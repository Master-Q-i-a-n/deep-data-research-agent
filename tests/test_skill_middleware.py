from types import SimpleNamespace

import pytest
from deepagents.backends.protocol import FileDownloadResponse, LsResult
from langgraph.store.memory import InMemoryStore

from deep_data_research_agent.agents.middleware.skills import (
    MongoSkillsRestoreMiddleware,
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
async def test_supervisor_lifecycle_ensures_and_exports(monkeypatch) -> None:
    manager = FakeManager()
    monkeypatch.setattr(sandbox_manager, "SANDBOX_MANAGER", manager)
    middleware = SandboxLifecycleMiddleware(
        component="supervisor",
        network_enabled=True,
    )

    await middleware.abefore_agent({}, _runtime())
    await middleware.aafter_agent({}, _runtime())

    assert manager.calls == [
        ("ensure", "thread-a", "supervisor", True, "user-a"),
        ("ensure", "thread-a", "supervisor", True, "user-a"),
        ("export", "thread-a", "supervisor"),
    ]


@pytest.mark.asyncio
async def test_public_and_user_skills_are_restored_to_isolated_roots(
    monkeypatch,
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
    middleware = MongoSkillsRestoreMiddleware(
        component="supervisor",
        agent_name="supervisor",
    )

    await middleware.abefore_agent({}, runtime)

    assert manager.calls == [
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
async def test_deleted_mongodb_skills_clear_sandbox_residue(monkeypatch) -> None:
    manager = FakeManager()
    monkeypatch.setattr(sandbox_manager, "SANDBOX_MANAGER", manager)
    middleware = MongoSkillsRestoreMiddleware(
        component="crawl-worker",
        agent_name="crawl-worker",
    )

    await middleware.abefore_agent({}, _runtime(InMemoryStore()))

    assert [call[3] for call in manager.calls] == [[], []]
    assert [call[2] for call in manager.calls] == [
        "/skills/public/crawl-worker/active",
        "/skills/user/crawl-worker/active",
    ]


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
