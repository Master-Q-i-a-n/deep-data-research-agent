import json
from types import SimpleNamespace

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import ValidationError

from deep_data_research_agent import memory


def _runtime(messages=None, *, tool_call_id="call-memory"):
    return SimpleNamespace(
        state={"messages": list(messages or [])},
        config={
            "run_id": "run-a",
            "configurable": {
                "thread_id": "thread-a",
                "langgraph_auth_user_id": "user-a",
            },
        },
        tool_call_id=tool_call_id,
        server_info=None,
    )


class InMemoryMemoryQueue(memory.MemoryQueue):
    def __init__(self) -> None:
        super().__init__()
        self.documents: dict[tuple[tuple[str, ...], str], dict] = {}

    async def _memory_documents(self, namespace, *, key_prefix=None):
        return [
            document
            for (stored_namespace, key), document in self.documents.items()
            if stored_namespace == namespace
            and (key_prefix is None or key.startswith(key_prefix))
        ]

    async def _memory_document(self, namespace, key):
        return self.documents.get((namespace, key))

    async def _put_memory_file(self, namespace, key, value):
        self.documents[(namespace, key)] = {
            "namespace": list(namespace),
            "namespace_str": "/".join(namespace),
            "key": key,
            "value": value,
        }

    async def _delete_memory_file(self, namespace, key):
        self.documents.pop((namespace, key), None)


def _lesson(title: str = "查询前未确认字段类型") -> memory.FailureLesson:
    return memory.FailureLesson(
        title=title,
        applicability="数据库字段类型可能与查询条件不一致时",
        symptom="只读 SQL 查询因类型不匹配而执行失败",
        cause="查询前没有读取表结构，直接使用了不兼容的比较条件",
        remedy="先读取对象详情确认字段类型，再构造类型匹配的只读查询",
        verification="预览查询成功并返回符合预期的列",
        boundary="只适用于数据库字段类型尚未确认的场景",
        tags=["postgresql", "schema"],
    )


def _record(
    agent: memory.AgentName = "data-analyst",
    *,
    title: str = "查询前未确认字段类型",
    count: int = 1,
    timestamp: str = "2026-08-01T00:00:00+00:00",
) -> memory.StoredFailure:
    lesson = _lesson(title)
    return memory.StoredFailure(
        lesson_id=memory._failure_id(lesson),
        agent=agent,
        lesson=lesson,
        count=count,
        created_at=timestamp,
        last_seen=timestamp,
        source_fingerprints=[memory._failure_fingerprint(title)],
    )


def test_memory_namespaces_are_isolated() -> None:
    runtime = _runtime()

    user_namespace = memory.user_memory_namespace(runtime)
    assert user_namespace[1:] == ("memories", "user")
    assert memory.failure_memory_namespace("supervisor") == (
        "public",
        "memories",
        "supervisor",
    )
    assert memory.failure_memory_namespace("data-analyst") != (
        "public",
        "memories",
        "crawl-worker",
    )


def test_user_memory_round_trip_uses_null_defaults_and_new_fields() -> None:
    source = memory.UserMemory(
        updated_at="2026-08-13T00:00:00+00:00",
        preferences=memory.UserPreferences(preferred_currency="usd"),
        avoid_behaviors=["不要重复委派", "不要重复委派"],
        reinforce_behaviors=["继续生成 PDF"],
    )

    content = memory._render_user_memory(source)
    restored = memory._parse_user_memory(content)

    assert restored.preferences.preferred_currency == "USD"
    assert restored.preferences.preferred_output is None
    assert restored.avoid_behaviors == ["不要重复委派"]
    assert "recent_suppliers" not in content
    assert "recent_queries" not in content


def test_invalid_or_old_user_memory_is_treated_as_empty() -> None:
    old = "# 用户偏好\n```yaml\npreferred_currency: USD\nrecent_queries: [x]\n```"

    assert memory._parse_user_memory(old) == memory.UserMemory()


def test_user_patch_updates_clears_and_resolves_behavior_conflicts() -> None:
    current = memory.UserMemory(
        updated_at="old",
        preferences=memory.UserPreferences(
            preferred_output="chart",
            preferred_currency="USD",
        ),
        avoid_behaviors=["不要使用饼图", "不要重复委派"],
        reinforce_behaviors=["继续生成 PDF"],
    )
    patch = memory.UserMemoryPatch(
        action="update",
        preference_updates=[
            memory.PreferenceUpdate(field="preferred_output", value=None),
            memory.PreferenceUpdate(field="preferred_language", value="zh"),
        ],
        add_avoid=["不要生成冗长报告"],
        remove_avoid=["不要重复委派"],
        add_reinforce=["不要使用饼图", "继续提供 ZIP"],
        remove_reinforce=["继续生成 PDF"],
    )

    updated = memory._apply_user_patch(current, patch)

    assert updated.preferences.preferred_output is None
    assert updated.preferences.preferred_currency == "USD"
    assert updated.preferences.preferred_language == "zh"
    assert updated.avoid_behaviors == ["不要生成冗长报告"]
    assert updated.reinforce_behaviors == ["不要使用饼图", "继续提供 ZIP"]
    assert updated.updated_at != "old"


def test_user_patch_drops_behavior_text_that_still_contains_sensitive_data() -> None:
    updated = memory._apply_user_patch(
        memory.UserMemory(),
        memory.UserMemoryPatch(
            action="update",
            add_avoid=["不要访问 https://example.com", "不要重复委派"],
            add_reinforce=["继续读取 /workspace/private.csv"],
        ),
    )

    assert updated.avoid_behaviors == ["不要重复委派"]
    assert updated.reinforce_behaviors == []


def test_user_patch_rejects_unknown_and_invalid_fields() -> None:
    with pytest.raises(ValidationError):
        memory.UserMemoryPatch.model_validate({"action": "update", "unknown": 1})
    with pytest.raises(ValidationError):
        memory.PreferenceUpdate(field="preferred_currency", value="US")
    bounded = memory.UserMemory(avoid_behaviors=[str(index) for index in range(21)])
    assert len(bounded.avoid_behaviors) == 20
    with pytest.raises(ValidationError):
        memory.UserMemoryPatch(
            action="update",
            preference_updates=[
                memory.PreferenceUpdate(field="preferred_language", value="zh"),
                memory.PreferenceUpdate(field="preferred_language", value="en"),
            ],
        )


def test_capture_evidence_uses_visible_user_and_previous_assistant() -> None:
    messages = [
        HumanMessage(content="先前问题"),
        AIMessage(content="上一条实际回复"),
        HumanMessage(content="内部续跑", name="async-task-monitor"),
        AIMessage(content="内部状态"),
        HumanMessage(content="以后不要重复委派"),
        AIMessage(content="", tool_calls=[]),
    ]

    user_message, previous_assistant = memory._capture_user_evidence(
        {"messages": messages}
    )

    assert user_message == "以后不要重复委派"
    assert previous_assistant == "上一条实际回复"


@pytest.mark.asyncio
async def test_capture_user_memory_enqueues_without_model_wait(monkeypatch) -> None:
    calls: list[dict] = []

    class Queue:
        async def enqueue(self, **kwargs):
            calls.append(kwargs)
            return True

    monkeypatch.setattr(memory, "MEMORY_QUEUE", Queue())
    runtime = _runtime(
        [
            HumanMessage(content="生成了三个委派"),
            AIMessage(content="这是上一条回复"),
            HumanMessage(content="以后同一数据任务只委派一次"),
        ]
    )

    result = await memory.capture_user_memory.coroutine(runtime=runtime)

    assert result.status == "success"
    assert calls[0]["kind"] == "user_memory"
    assert len(calls[0]["scope"]) == 64
    assert calls[0]["payload"] == {
        "user_message": "以后同一数据任务只委派一次",
        "previous_assistant": "这是上一条回复",
    }
    assert len(calls[0]["idempotency_key"]) == 64


@pytest.mark.asyncio
async def test_failure_tool_requires_and_redacts_tool_evidence(monkeypatch) -> None:
    calls: list[dict] = []

    class Queue:
        async def enqueue(self, **kwargs):
            calls.append(kwargs)
            return True

    monkeypatch.setattr(memory, "MEMORY_QUEUE", Queue())
    no_evidence = await memory.DATA_ANALYST_FAILURE_TOOL.coroutine(
        content="查询失败是因为字段类型不匹配，读取结构后修复并验证成功",
        runtime=_runtime([HumanMessage(content="分析数据库")]),
    )
    assert no_evidence.status == "error"
    assert calls == []

    runtime = _runtime(
        [
            HumanMessage(content="分析数据库"),
            ToolMessage(
                content="https://example.com /workspace/a.sql token=secret123 查询类型错误",
                tool_call_id="query-a",
                name="database_query_preview",
                status="error",
            ),
        ]
    )
    result = await memory.DATA_ANALYST_FAILURE_TOOL.coroutine(
        content="查询失败是因为字段类型不匹配，读取结构后修复并验证成功",
        runtime=runtime,
    )

    assert result.status == "success"
    assert calls[0]["kind"] == "failure_lesson"
    assert calls[0]["scope"] == "data-analyst"
    assert "user-a" not in str(calls[0])
    assert "example.com" not in str(calls[0]["payload"])
    assert "/workspace/" not in str(calls[0]["payload"])
    assert "secret123" not in str(calls[0]["payload"])


@pytest.mark.asyncio
async def test_memory_refresh_replaces_checkpoint_cached_contents() -> None:
    class Backend:
        async def adownload_files(self, paths):
            return [
                SimpleNamespace(error=None, content=f"fresh:{path}".encode())
                for path in paths
            ]

    sources = [memory.USER_MEMORY_PATH, memory.agent_memory_path("supervisor")]
    middleware = memory.MemoryRefreshMiddleware(
        backend_factory=lambda _runtime: Backend(),
        sources=sources,
    )

    update = await middleware.abefore_agent(
        {"memory_contents": {sources[0]: "stale"}},
        SimpleNamespace(),
        {},
    )

    assert update == {
        "memory_contents": {
            sources[0]: f"fresh:{sources[0]}",
            sources[1]: f"fresh:{sources[1]}",
        }
    }


@pytest.mark.asyncio
async def test_async_bridge_does_not_copy_user_memory_into_task() -> None:
    request = SimpleNamespace(
        tool=SimpleNamespace(name="start_async_task"),
        tool_call={
            "name": "start_async_task",
            "args": {"subagent_type": "crawl-worker", "description": "采集文档"},
        },
        state={"memory_contents": {memory.USER_MEMORY_PATH: "secret preference"}},
    )
    seen: list[object] = []

    async def handler(value):
        seen.append(value)
        return "ok"

    result = await memory.AsyncTaskBridgeMiddleware().awrap_tool_call(request, handler)

    assert result == "ok"
    assert seen == [request]
    assert seen[0].tool_call["args"]["description"] == "采集文档"


def test_async_bridge_normalizes_completed_child_json() -> None:
    outer = {
        "status": "success",
        "result": json.dumps(
            {
                "status": "success",
                "summary": "done",
                "artifacts": [],
                "sources": [],
                "warnings": [],
            }
        ),
    }
    response = Command(
        update={
            "messages": [
                ToolMessage(
                    content=json.dumps(outer),
                    tool_call_id="check-a",
                    name="check_async_task",
                )
            ]
        }
    )

    normalized = memory.AsyncTaskBridgeMiddleware._normalize_check_response(response)
    content = json.loads(normalized.update["messages"][0].content)
    assert content["result"]["summary"] == "done"


@pytest.mark.asyncio
async def test_structured_model_repairs_once_and_detaches_callbacks(monkeypatch) -> None:
    calls: list[dict] = []

    class StructuredModel:
        async def ainvoke(self, prompt, config=None):
            calls.append({"prompt": prompt, "config": config})
            if len(calls) == 1:
                raise OutputParserException("bad json")
            return memory.UserMemoryPatch(action="discard")

    class Model:
        def with_structured_output(self, schema, *, method):
            assert schema is memory.UserMemoryPatch
            assert method == "json_mode"
            return StructuredModel()

    monkeypatch.setattr(
        memory,
        "create_memory_model",
        lambda: Model(),
    )

    result = await memory._extract_user_memory_patch(
        current=memory.UserMemory(),
        user_message="本轮只导出 CSV",
        previous_assistant="",
    )

    assert result.action == "discard"
    assert len(calls) == 2
    assert calls[0]["config"]["callbacks"] == []
    assert "memory-internal" in calls[0]["config"]["tags"]
    assert '"action": "update"' in calls[0]["prompt"]
    assert '"action": "discard"' in calls[0]["prompt"]
    assert '"field": "preferred_currency", "value": "CNY"' in calls[0]["prompt"]
    assert "禁止返回 schema_version" in calls[0]["prompt"]
    assert "上一输出未通过" in calls[1]["prompt"]


@pytest.mark.asyncio
async def test_user_memory_job_writes_only_when_patch_changes(monkeypatch) -> None:
    queue = InMemoryMemoryQueue()
    identity_hash = "a" * 64

    async def extract(**_kwargs):
        return memory.UserMemoryPatch(
            action="update",
            preference_updates=[
                memory.PreferenceUpdate(field="preferred_language", value="zh")
            ],
            add_avoid=["不要重复委派"],
        )

    monkeypatch.setattr(memory, "_extract_user_memory_patch", extract)
    await queue._process_user_memory(
        {
            "scope": identity_hash,
            "payload": {"user_message": "以后不要重复委派"},
        }
    )

    namespace = memory.user_memory_namespace_from_hash(identity_hash)
    document = queue.documents[(namespace, "/MEMORY.md")]
    restored = memory._parse_user_memory(document["value"]["content"])
    assert restored.preferences.preferred_language == "zh"
    assert restored.avoid_behaviors == ["不要重复委派"]


@pytest.mark.asyncio
async def test_failure_job_adds_then_exactly_deduplicates(monkeypatch) -> None:
    queue = InMemoryMemoryQueue()
    calls = 0

    async def decide(**_kwargs):
        nonlocal calls
        calls += 1
        return memory.FailureDecision(action="add", lesson=_lesson())

    monkeypatch.setattr(memory, "_extract_failure_decision", decide)
    job = {
        "scope": "data-analyst",
        "payload": {
            "content": "字段类型不匹配；读取表结构后改正查询并验证成功",
            "evidence": [{"name": "database_query_preview", "status": "error"}],
        },
    }

    await queue._process_failure_lesson(job)
    await queue._process_failure_lesson(job)

    records = await queue._active_failures("data-analyst")
    assert calls == 1
    assert len(records) == 1
    assert records[0].count == 2
    namespace = memory.failure_memory_namespace("data-analyst")
    index = queue.documents[(namespace, "/MEMORY.md")]["value"]["content"]
    assert records[0].lesson_id in index
    assert f"/pitfalls/{records[0].lesson_id}.md" in index
    detail = queue.documents[
        (namespace, f"/pitfalls/{records[0].lesson_id}.md")
    ]["value"]["content"]
    assert "## 已确认原因" in detail
    assert "## 验证方式" in detail


@pytest.mark.asyncio
async def test_failure_job_semantically_merges_existing_target(monkeypatch) -> None:
    queue = InMemoryMemoryQueue()
    existing = _record(count=2)
    await queue._put_failure(existing)

    merged_lesson = _lesson("查询前必须读取字段类型")

    async def decide(**_kwargs):
        return memory.FailureDecision(
            action="merge",
            merge_target_id=existing.lesson_id,
            lesson=merged_lesson,
        )

    monkeypatch.setattr(memory, "_extract_failure_decision", decide)
    await queue._process_failure_lesson(
        {
            "scope": "data-analyst",
            "payload": {
                "content": "另一次类型不匹配，读取 schema 后修复并验证",
                "evidence": [{"name": "database_get_object_details", "status": "success"}],
            },
        }
    )

    records = await queue._active_failures("data-analyst")
    assert len(records) == 1
    assert records[0].lesson_id == existing.lesson_id
    assert records[0].lesson.title == "查询前必须读取字段类型"
    assert records[0].count == 3


@pytest.mark.asyncio
async def test_failure_job_discards_model_output_with_sensitive_text(monkeypatch) -> None:
    queue = InMemoryMemoryQueue()
    unsafe = _lesson().model_copy(
        update={"remedy": "读取 /workspace/private.sql 后重试并验证查询结果"}
    )

    async def decide(**_kwargs):
        return memory.FailureDecision(action="add", lesson=unsafe)

    monkeypatch.setattr(memory, "_extract_failure_decision", decide)
    await queue._process_failure_lesson(
        {
            "scope": "data-analyst",
            "payload": {
                "content": "字段类型不匹配，读取结构后修复",
                "evidence": [{"name": "database_query_preview", "status": "error"}],
            },
        }
    )

    assert await queue._active_failures("data-analyst") == []


@pytest.mark.asyncio
async def test_capacity_archives_low_frequency_oldest(monkeypatch) -> None:
    queue = InMemoryMemoryQueue()
    old = _record(title="旧的低频失败经验", timestamp="2026-01-01T00:00:00+00:00")
    recent = _record(
        title="最近发生的失败经验",
        timestamp="2026-08-01T00:00:00+00:00",
    )
    await queue._put_failure(old)
    await queue._put_failure(recent)
    monkeypatch.setattr(memory, "MAX_ACTIVE_FAILURES", 1)

    await queue._enforce_failure_capacity("data-analyst")
    await queue._rebuild_failure_index("data-analyst")

    namespace = memory.failure_memory_namespace("data-analyst")
    assert (namespace, f"/pitfalls/{old.lesson_id}.md") not in queue.documents
    assert (namespace, f"/archive/{old.lesson_id}.md") in queue.documents
    index = queue.documents[(namespace, "/MEMORY.md")]["value"]["content"]
    assert old.lesson_id not in index
    assert recent.lesson_id in index


def test_memory_error_retry_classification() -> None:
    assert memory._is_retryable_memory_error(TimeoutError()) is True
    assert memory._is_retryable_memory_error(OutputParserException("bad")) is False
    assert memory._is_retryable_memory_error(ValueError("bad")) is False


@pytest.mark.asyncio
async def test_non_retryable_job_failure_preserves_terminal_state(monkeypatch) -> None:
    updates: list[tuple[dict, dict]] = []

    class Jobs:
        async def update_one(self, query, update):
            updates.append((query, update))

    queue = memory.MemoryQueue()

    async def collections():
        return Jobs(), SimpleNamespace(), SimpleNamespace()

    monkeypatch.setattr(queue, "_collections", collections)
    await queue._mark_failed(
        {"_id": "job-a", "attempts": 1},
        OutputParserException("bad json"),
        retryable=False,
    )

    update = updates[0][1]["$set"]
    assert update["status"] == "failed"
    assert "expires_at" in update


def test_legacy_automatic_middlewares_are_removed() -> None:
    assert not hasattr(memory, "UserPreferenceUpdateMiddleware")
    assert not hasattr(memory, "AgentExperienceEnqueueMiddleware")
    assert not hasattr(memory, "PREFERENCE_UPDATE_GRAPH")
