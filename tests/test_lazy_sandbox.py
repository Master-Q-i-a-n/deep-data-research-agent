from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.store.memory import InMemoryStore
from test_sandbox_manager import _handle, _settings

from deep_data_research_agent.agents.backends import create_backend
from deep_data_research_agent.agents.middleware.skills import (
    ReloadableSkillsMiddleware,
    SandboxLifecycleMiddleware,
)
from deep_data_research_agent.infrastructure.sandbox import manager as sandbox_manager


@pytest.mark.asyncio
async def test_model_setup_and_unused_run_never_allocate_sandbox(monkeypatch):
    manager = SimpleNamespace(
        ensure=AsyncMock(side_effect=AssertionError("sandbox on model path")),
        export_existing_workspace=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(sandbox_manager, "SANDBOX_MANAGER", manager)
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="lazy-thread"),
        server_info=SimpleNamespace(user=SimpleNamespace(identity="user-a")),
    )
    middleware = SandboxLifecycleMiddleware(component="supervisor")
    assert type(middleware).abefore_agent is AgentMiddleware.abefore_agent
    await middleware.aafter_agent({}, runtime)
    manager.ensure.assert_not_awaited()
    manager.export_existing_workspace.assert_awaited_once_with(
        "lazy-thread", component="supervisor", user_id="user-a"
    )


@pytest.mark.asyncio
async def test_real_deepagent_can_respond_without_sandbox_preparation(monkeypatch):
    class ChatModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    manager = SimpleNamespace(
        prepare=AsyncMock(side_effect=AssertionError("model waited for sandbox")),
        export_existing_workspace=AsyncMock(return_value=[]),
        get_backend=Mock(
            side_effect=sandbox_manager.SandboxNotInitializedError("unused")
        ),
    )
    monkeypatch.setattr(sandbox_manager, "SANDBOX_MANAGER", manager)
    backend = create_backend()
    agent = create_deep_agent(
        model=ChatModel(responses=[AIMessage(content="ready")]),
        backend=backend,
        store=InMemoryStore(),
        middleware=[
            ReloadableSkillsMiddleware(
                backend=backend, sources=["/skills/public/supervisor/active/"]
            ),
            SandboxLifecycleMiddleware(component="supervisor"),
        ],
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "hello"}]},
        config={
            "configurable": {
                "thread_id": "pure-chat",
                "langgraph_auth_user_id": "user-a",
            }
        },
    )
    assert result["messages"][-1].content == "ready"
    manager.prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_inputs_are_refreshed_and_unchanged_inputs_are_not_downloaded(
    tmp_path, monkeypatch
):
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    handle = _handle("inputs")
    handle.component = "supervisor"
    manager._handles[manager._key("thread-a", "supervisor", "user-a")] = handle
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-a"),
        server_info=SimpleNamespace(user=SimpleNamespace(identity="user-a")),
    )
    scope = manager.workspace_scope("thread-a", "supervisor")
    await manager.workspace_store.write_many(scope, [("input/orders.csv", b"old")])
    read = AsyncMock(wraps=manager.workspace_store.read)
    monkeypatch.setattr(manager.workspace_store, "read", read)
    await manager.prepare(runtime, component="supervisor")
    assert handle.backend.files["/workspace/input/orders.csv"] == b"old"
    await manager.prepare(runtime, component="supervisor")
    assert read.await_count == 1
    await manager.workspace_store.write_many(scope, [("input/orders.csv", b"new")])
    await manager.prepare(runtime, component="supervisor")
    assert handle.backend.files["/workspace/input/orders.csv"] == b"new"
    await manager.workspace_store.delete_file(scope, "input/orders.csv")
    execute = AsyncMock(wraps=handle.backend.aexecute)
    monkeypatch.setattr(handle.backend, "aexecute", execute)
    await manager.prepare(runtime, component="supervisor")
    assert any("keep=set([])" in call.args[0] for call in execute.await_args_list)
    # 模拟容器里仍残留旧副本：结束导出也不得复活已删除附件。
    handle.backend.files["/workspace/result.md"] = b"report"
    result = await manager.export_workspace("thread-a", component="supervisor")
    assert [item["path"] for item in result] == ["/workspace/result.md"]
    assert not await manager.workspace_store.list(scope, prefix="input")
