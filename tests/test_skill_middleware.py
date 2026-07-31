from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from deep_data_research_agent import identity, sandbox_manager
from deep_data_research_agent.skill_middleware import (
    SandboxLifecycleMiddleware,
    SkillsSyncMiddleware,
    UserSkillsRestoreMiddleware,
)


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
        server_info=None,
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
        ("ensure", "thread-a", "supervisor", True, "local-user"),
        ("export", "thread-a", "supervisor"),
    ]


@pytest.mark.asyncio
async def test_builtin_skills_are_copied_to_physical_sandbox(
    monkeypatch,
) -> None:
    manager = FakeManager()
    monkeypatch.setattr(sandbox_manager, "SANDBOX_MANAGER", manager)
    middleware = SkillsSyncMiddleware(
        component="supervisor",
        scope="supervisor",
    )

    await middleware.abefore_agent({}, _runtime())

    _, _, root, files, component = manager.calls[0]
    assert root == "/skills"
    assert component == "supervisor"
    assert any(
        path == "supervisor/evidence-reporting/SKILL.md"
        for path, _content in files
    )


@pytest.mark.asyncio
async def test_active_user_skills_are_restored_from_store(monkeypatch) -> None:
    manager = FakeManager()
    monkeypatch.setattr(sandbox_manager, "SANDBOX_MANAGER", manager)
    store = InMemoryStore()
    runtime = _runtime(store)
    namespace = (
        identity.user_hash(runtime),
        "skills",
        "assigned",
        "supervisor",
    )
    await store.aput(
        namespace,
        "/active/demo-skill/SKILL.md",
        {
            "content": "---\nname: demo-skill\ndescription: demo\n---\n",
            "encoding": "utf-8",
        },
    )
    await store.aput(
        namespace,
        "/manifests/demo-skill.json",
        {"content": "{}", "encoding": "utf-8"},
    )
    middleware = UserSkillsRestoreMiddleware(
        component="supervisor",
        agent_name="supervisor",
    )

    await middleware.abefore_agent({}, runtime)

    _, _, root, files, component = manager.calls[0]
    assert root == "/persisted-skills"
    assert component == "supervisor"
    assert files == [
        (
            "active/demo-skill/SKILL.md",
            b"---\nname: demo-skill\ndescription: demo\n---\n",
        )
    ]
