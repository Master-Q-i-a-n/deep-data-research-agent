import json
from types import SimpleNamespace

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from pydantic import ValidationError

from deep_data_research_agent import memory


def _runtime(store: InMemoryStore, user_id: str = "user-a") -> SimpleNamespace:
    return SimpleNamespace(
        store=store,
        server_info=SimpleNamespace(
            user=SimpleNamespace(identity=user_id),
        ),
        execution_info=SimpleNamespace(thread_id="thread-a"),
    )


@pytest.mark.asyncio
async def test_new_user_gets_isolated_default_preferences() -> None:
    store = InMemoryStore()
    user_a = _runtime(store, "user-a")
    user_b = _runtime(store, "user-b")

    preferences = await memory.load_or_initialize_preferences(user_a)

    assert preferences == memory.UserPreferences()
    assert await store.aget(memory.user_preferences_namespace(user_a), "/preferences.md")
    assert await store.aget(memory.user_preferences_namespace(user_b), "/preferences.md") is None


@pytest.mark.asyncio
async def test_preference_update_merges_only_latest_patch(monkeypatch) -> None:
    store = InMemoryStore()
    runtime = _runtime(store)

    async def extract_patch(**_kwargs) -> memory.PreferencePatch:
        return memory.PreferencePatch(
            preferred_currency="usd",
            recent_suppliers=["供应商 A"],
            recent_queries=["服务器采购比价"],
        )

    monkeypatch.setattr(memory, "_extract_preference_patch", extract_patch)
    middleware = memory.UserPreferenceUpdateMiddleware()
    await middleware.aafter_agent(
        {
            "messages": [
                HumanMessage(content="以后用美元，比较供应商 A 的服务器报价"),
                AIMessage(content="已按美元整理。"),
            ]
        },
        runtime,
    )

    updated = await memory.load_or_initialize_preferences(runtime)
    assert updated.preferred_currency == "USD"
    assert updated.recent_suppliers == ["供应商 A"]
    assert updated.recent_queries == ["服务器采购比价"]
    assert updated.preferred_chart_type == "bar"


@pytest.mark.asyncio
async def test_preference_workflow_keeps_existing_data_when_extraction_fails(
    monkeypatch,
) -> None:
    store = InMemoryStore()
    runtime = _runtime(store)
    original = memory.UserPreferences(preferred_currency="USD")
    await memory.save_preferences(runtime, original)

    async def invalid_extraction(**_kwargs):
        raise ValidationError.from_exception_data("PreferencePatch", [])

    monkeypatch.setattr(memory, "_extract_preference_patch", invalid_extraction)
    await memory.UserPreferenceUpdateMiddleware().aafter_agent(
        {
            "messages": [
                HumanMessage(content="以后都用欧元"),
                AIMessage(content="好的。"),
            ]
        },
        runtime,
    )

    assert await memory.load_or_initialize_preferences(runtime) == original


@pytest.mark.asyncio
async def test_preference_extractor_detaches_internal_model_callbacks(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StructuredModel:
        async def ainvoke(self, _prompt, config=None):
            captured.update(config or {})
            return memory.PreferenceExtraction(
                should_update=False,
                changes=memory.PreferencePatch(),
            )

    class MemoryModel:
        def with_structured_output(self, schema, *, method):
            assert schema is memory.PreferenceExtraction
            assert method == "json_mode"
            return StructuredModel()

    monkeypatch.setattr(memory, "create_memory_model", lambda: MemoryModel())
    patch = await memory._extract_preference_patch(
        user_message="你好",
        final_answer="你好。",
        current=memory.UserPreferences(),
    )

    assert patch == memory.PreferencePatch()
    assert captured["callbacks"] == []
    assert "memory-internal" in captured["tags"]


def test_preference_patch_rejects_unknown_or_invalid_fields() -> None:
    with pytest.raises(ValidationError):
        memory.PreferencePatch.model_validate({"unknown": "value"})
    with pytest.raises(ValidationError):
        memory.PreferencePatch(preferred_currency="人民币")


def test_preference_workflow_has_explicit_validation_branches() -> None:
    nodes = set(memory.PREFERENCE_UPDATE_GRAPH.get_graph().nodes)

    assert {"load", "extract", "merge", "save"} <= nodes


@pytest.mark.asyncio
async def test_memory_refresh_overwrites_checkpoint_cached_contents() -> None:
    class Backend:
        async def adownload_files(self, paths):
            return [
                SimpleNamespace(
                    error=None,
                    content=f"fresh:{path}".encode(),
                )
                for path in paths
            ]

    middleware = memory.MemoryRefreshMiddleware(
        backend_factory=lambda _runtime: Backend(),
        sources=[memory.AGENT_MEMORY_PATHS["supervisor"]],
    )
    update = await middleware.abefore_agent(
        {"memory_contents": {"old": "stale"}},
        SimpleNamespace(),
        {},
    )

    assert update == {
        "memory_contents": {
            memory.AGENT_MEMORY_PATHS["supervisor"]: (
                "fresh:/memories/agent/supervisor.md"
            )
        }
    }
    assert "不得调用 write_file 或 edit_file" in middleware.system_prompt


@pytest.mark.asyncio
async def test_async_task_forwarding_adds_preferences_once() -> None:
    class Request:
        def __init__(self, tool_call, state):
            self.tool = SimpleNamespace(name=tool_call["name"])
            self.tool_call = tool_call
            self.state = state

        def override(self, *, tool_call):
            return Request(tool_call, self.state)

    state = {
        "memory_contents": {
            memory.USER_PREFERENCES_PATH: "preferred_currency: CNY",
        }
    }
    request = Request(
        {
            "name": "start_async_task",
            "args": {
                "description": "搜索服务器报价",
                "subagent_type": "crawl-worker",
            },
        },
        state,
    )
    seen: list[str] = []

    async def handler(modified):
        seen.append(modified.tool_call["args"]["description"])
        return "ok"

    middleware = memory.AsyncTaskBridgeMiddleware()
    assert await middleware.awrap_tool_call(request, handler) == "ok"
    assert seen[0].count("<user_preferences>") == 1
    assert "preferred_currency: CNY" in seen[0]


def test_async_task_bridge_parses_structured_child_result() -> None:
    child_result = {
        "status": "success",
        "summary": "采集完成",
        "artifacts": [],
        "sources": [],
        "warnings": [],
    }
    outer = {
        "status": "success",
        "thread_id": "child-thread",
        "result": json.dumps(child_result, ensure_ascii=False),
    }
    response = Command(
        update={
            "messages": [
                ToolMessage(
                    content=json.dumps(outer, ensure_ascii=False),
                    tool_call_id="call-check",
                )
            ],
            "async_tasks": {},
        }
    )

    normalized = memory.AsyncTaskBridgeMiddleware._normalize_check_response(
        response
    )
    payload = json.loads(normalized.update["messages"][0].content)

    assert payload["result"] == child_result


def test_experience_payload_is_bounded_and_redacted() -> None:
    payload = memory._experience_payload(
        {
            "messages": [
                HumanMessage(content="访问 https://example.com，密钥 sk-secret123456"),
                ToolMessage(
                    content="Bearer abc.secret.token 请求失败 https://example.com/a",
                    tool_call_id="call-a",
                    name="tavily_search",
                    status="error",
                ),
                AIMessage(content="网页搜索失败，稍后重试。"),
            ]
        }
    )

    encoded = str(payload)
    assert payload is not None
    assert "example.com" not in encoded
    assert "sk-secret" not in encoded
    assert "Bearer" not in encoded
    assert payload["tools"][0]["status"] == "error"


def test_read_only_file_tools_do_not_enqueue_experience() -> None:
    payload = memory._experience_payload(
        {
            "messages": [
                HumanMessage(content="我的偏好是什么"),
                ToolMessage(
                    content="preferred_currency: USD",
                    tool_call_id="call-read",
                    name="read_file",
                    status="success",
                ),
                ToolMessage(
                    content="/memories/user/preferences.md",
                    tool_call_id="call-list",
                    name="ls",
                    status="success",
                ),
                AIMessage(content="你偏好使用美元。"),
            ]
        }
    )

    assert payload is None


@pytest.mark.asyncio
async def test_experience_extractor_spells_out_fixed_json_fields(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StructuredModel:
        async def ainvoke(self, prompt):
            captured["prompt"] = prompt
            return memory.ExperiencePatch(entries=[])

    class MemoryModel:
        def with_structured_output(self, schema, *, method):
            assert schema is memory.ExperiencePatch
            assert method == "json_mode"
            return StructuredModel()

    def create_model(*, background=False):
        captured["background"] = background
        return MemoryModel()

    monkeypatch.setattr(memory, "create_memory_model", create_model)
    result = await memory._extract_experience_patch(
        agent_name="supervisor",
        payload={"user_signal": "test", "final_summary": "test", "tools": []},
    )

    assert result == memory.ExperiencePatch(entries=[])
    assert captured["background"] is True
    prompt = str(captured["prompt"])
    assert '"kind": "success"' in prompt
    assert '"lesson"' in prompt
    assert '"action"' in prompt
    assert "禁止使用 type、content" in prompt


def test_memory_error_retry_classification_and_text() -> None:
    assert memory._is_retryable_memory_error(TimeoutError()) is True
    assert memory._is_retryable_memory_error(OutputParserException("bad json")) is False
    assert memory._is_retryable_memory_error(ValueError("bad schema")) is False
    assert memory._memory_error_text(TimeoutError()) == "TimeoutError"


@pytest.mark.asyncio
async def test_parser_failure_is_marked_non_retryable(monkeypatch) -> None:
    queue = memory.MemoryQueue()
    captured: dict[str, object] = {}

    async def invalid_patch(**_kwargs):
        raise OutputParserException("bad json")

    async def mark_failed(job, error, *, retryable):
        captured.update({"job": job, "error": error, "retryable": retryable})

    monkeypatch.setattr(memory, "_extract_experience_patch", invalid_patch)
    monkeypatch.setattr(queue, "_mark_failed", mark_failed)
    monkeypatch.setattr(
        memory,
        "get_settings",
        lambda: SimpleNamespace(memory_experience_timeout_seconds=30),
    )
    job = {"_id": "job-a", "agent_name": "supervisor", "payload": {}}

    await queue._process_job(job)

    assert captured["job"] == job
    assert isinstance(captured["error"], OutputParserException)
    assert captured["retryable"] is False


@pytest.mark.asyncio
async def test_non_retryable_failure_skips_retry_state(monkeypatch) -> None:
    updates: list[tuple[dict, dict]] = []

    class Jobs:
        async def update_one(self, query, update):
            updates.append((query, update))

    queue = memory.MemoryQueue()

    async def collections():
        return Jobs(), SimpleNamespace()

    monkeypatch.setattr(queue, "_collections", collections)
    await queue._mark_failed(
        {"_id": "job-a", "attempts": 1},
        OutputParserException("bad json"),
        retryable=False,
    )

    update = updates[0][1]["$set"]
    assert update["status"] == "failed"
    assert update["last_error"].startswith("OutputParserException:")
    assert "expires_at" in update


@pytest.mark.asyncio
async def test_shared_experience_is_deduplicated_and_persisted(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(memory, "AGENT_MEMORY_ROOT", tmp_path)
    entry = memory.ExperienceEntry(
        kind="pitfall",
        lesson="同步 SDK 调用会阻塞异步请求事件循环",
        action="使用 asyncio.to_thread 包装阻塞调用并设置超时",
    )

    assert await memory.persist_experience_entries("supervisor", [entry]) == 1
    assert await memory.persist_experience_entries("supervisor", [entry]) == 1

    content = (tmp_path / "supervisor.md").read_text("utf-8")
    assert "出现 2 次" in content
    assert "同步 SDK 调用" in content
    assert "memory-data" in content
