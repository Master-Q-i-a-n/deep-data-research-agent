from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from deepagents.backends.protocol import FileDownloadResponse, LsResult
from langgraph.store.memory import InMemoryStore

from deep_data_research_agent.agents.middleware.skills import (
    ReloadableSkillsMiddleware,
    SandboxLifecycleMiddleware,
)
from deep_data_research_agent.core import identity
from deep_data_research_agent.infrastructure.sandbox import manager as sandbox_manager
from deep_data_research_agent.skill_system.storage import file_store_value


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def ensure(
        self,
        thread_id: str,
        *,
        component: str,
        network_enabled: bool,
        user_id: str,
    ) -> None:
        self.calls.append(("ensure", thread_id, component, network_enabled, user_id))

    async def export_existing_workspace(self, thread_id, *, component, user_id):
        self.calls.append(("export_existing", thread_id, component, user_id))

    async def export_workspace(
        self,
        thread_id: str,
        *,
        component: str,
    ) -> None:
        self.calls.append(("export", thread_id, component))

    async def replace_directory_files(
        self,
        thread_id: str,
        root: str,
        files: list[tuple[str, bytes]],
        *,
        component: str,
    ) -> None:
        self.calls.append(("replace", thread_id, root, files, component))


def _runtime(store=None):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-a"),
        store=store,
        server_info=SimpleNamespace(user=SimpleNamespace(identity="user-a")),
    )


@pytest.mark.asyncio
async def test_supervisor_lifecycle_only_exports_existing(monkeypatch) -> None:
    manager = FakeManager()
    monkeypatch.setattr(sandbox_manager, "SANDBOX_MANAGER", manager)
    middleware = SandboxLifecycleMiddleware(
        component="supervisor",
    )

    await middleware.aafter_agent({}, _runtime())

    assert manager.calls == [
        ("export_existing", "thread-a", "supervisor", "user-a"),
    ]


@pytest.mark.asyncio
async def test_public_and_user_skills_are_restored_to_isolated_roots(
    monkeypatch, tmp_path,
) -> None:
    manager = FakeManager()
    monkeypatch.setattr(sandbox_manager, "SANDBOX_MANAGER", manager)
    store = InMemoryStore()
    runtime = _runtime(store)
    public_namespace = ("public", "skills", "supervisor")
    user_namespace = (identity.user_hash(runtime), "skills", "supervisor")
    await store.aput(
        public_namespace,
        "/active/demo-skill/SKILL.md",
        file_store_value(b"public"),
    )
    await store.aput(
        user_namespace,
        "/active/demo-skill/SKILL.md",
        file_store_value(b"user"),
    )
    await store.aput(
        user_namespace,
        "/manifests/demo-skill.json",
        file_store_value(b"{}"),
    )
    from test_sandbox_manager import _handle, _settings

    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    handle = _handle("lazy-skills")
    handle.component = "supervisor"
    manager._handles[manager._key("thread-a", "supervisor", "user-a")] = handle
    calls = []
    original_replace = manager.replace_directory_files

    async def replace(thread_id, root, files, *, component):
        calls.append(("replace", thread_id, root, files, component))
        return await original_replace(thread_id, root, files, component=component)

    monkeypatch.setattr(manager, "replace_directory_files", replace)
    await manager.prepare(runtime, component="supervisor")

    assert calls[:2] == [
        (
            "replace",
            "thread-a",
            "/skills/public/supervisor/active",
            [("demo-skill/SKILL.md", b"public")],
            "supervisor",
        ),
        (
            "replace",
            "thread-a",
            "/skills/user/supervisor/active",
            [("demo-skill/SKILL.md", b"user")],
            "supervisor",
        ),
    ]


@pytest.mark.asyncio
async def test_deleted_mongodb_skills_clear_sandbox_residue(monkeypatch, tmp_path) -> None:
    from test_sandbox_manager import _handle, _settings

    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    handle = _handle("skill-delete")
    manager._handles[manager._key("thread-a", "crawl-worker", "user-a")] = handle
    store = InMemoryStore()
    namespace = ("public", "skills", "crawl-worker")
    await store.aput(namespace, "/active/demo/SKILL.md", file_store_value(b"demo"))
    replace = AsyncMock(wraps=manager.replace_directory_files)
    monkeypatch.setattr(manager, "replace_directory_files", replace)
    await manager.prepare(_runtime(store), component="crawl-worker")
    assert replace.await_count == 2
    await manager.prepare(_runtime(store), component="crawl-worker")
    assert replace.await_count == 2  # 内容未变化不重复清理/上传。
    await store.adelete(namespace, "/active/demo/SKILL.md")
    await manager.prepare(_runtime(store), component="crawl-worker")
    assert replace.await_count == 3
    assert replace.call_args.args[2] == []


@pytest.mark.asyncio
async def test_user_skill_metadata_overrides_same_named_public_skill() -> None:
    public_root = "/skills/public/supervisor/active/"
    user_root = "/skills/user/supervisor/active/"

    class MetadataBackend:
        async def als(self, path):
            return LsResult(
                entries=[{"path": f"{path}demo", "is_dir": True}]
            )

        async def adownload_files(self, paths):
            responses = []
            for path in paths:
                description = "用户版本" if path.startswith(user_root) else "公共版本"
                content = (
                    "---\nname: demo\ndescription: "
                    f"{description}\n---\n"
                ).encode()
                responses.append(FileDownloadResponse(path=path, content=content))
            return responses

    middleware = ReloadableSkillsMiddleware(
        backend=MetadataBackend(),
        sources=[(public_root, "公共"), (user_root, "用户")],
    )

    update = await middleware.abefore_agent({}, None, {})

    assert update is not None
    assert update["skills_metadata"] == [
        {
            "path": "/skills/user/supervisor/active/demo/SKILL.md",
            "name": "demo",
            "description": "用户版本",
            "metadata": {},
            "license": None,
            "compatibility": None,
            "allowed_tools": [],
        }
    ]
