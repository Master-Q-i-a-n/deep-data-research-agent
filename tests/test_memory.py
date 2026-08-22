import json
from types import SimpleNamespace

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import ValidationError

from deep_data_research_agent import memory
from deep_data_research_agent.config import Settings


def test_memory_timeout_and_review_output_defaults_are_independent() -> None:
    settings = Settings(_env_file=None)

    assert settings.memory_model_timeout_seconds == 60
    assert settings.memory_job_timeout_seconds == 75
    assert settings.failure_review_max_output_tokens == 4096


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

    async def _put_user_memory_file(
        self,
        namespace,
        value,
        *,
        expected_generation,
    ):
        key = (namespace, memory.USER_MEMORY_KEY)
        current = self.documents.get(key)
        if memory._user_memory_generation(current) != expected_generation:
            return False
        self.documents[key] = {
            "namespace": list(namespace),
            "namespace_str": "/".join(namespace),
            "key": memory.USER_MEMORY_KEY,
            "generation": expected_generation,
            "value": value,
        }
        return True

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


def _review_messages(*, result="No matches found", final="没有查到结果"):
    user = HumanMessage(content="分析数据库", id="user-turn")
    tool_call = AIMessage(
        content="这段中间解释不应进入后台回顾",
        tool_calls=[
            {
                "name": "database_query_preview",
                "args": {"query": "select * from orders where email='a@example.com'"},
                "id": "query-a",
            }
        ],
    )
    tool_result = ToolMessage(
        content=result,
        tool_call_id="query-a",
        name="database_query_preview",
    )
    final_message = AIMessage(
        content=final,
        id="final-a",
        name="data-analyst",
    )
    return [user, tool_call, tool_result, final_message]


def test_failure_review_bundle_contains_only_completed_tool_evidence() -> None:
    bundle = memory._failure_review_bundle(
        _review_messages(),
        reviewable_tools=frozenset({"database_query_preview"}),
    )

    assert bundle is not None
    assert bundle.schema_version == 2
    assert bundle.turn_id == "user-turn"
    assert bundle.final_message_id == "final-a"
    assert bundle.task_goal == "分析数据库"
    assert bundle.final_response == "没有查到结果"
    assert bundle.final_status == "unknown"
    assert len(bundle.tool_events) == 1
    event = bundle.tool_events[0]
    assert event.tool_name == "database_query_preview"
    assert event.status == "empty"
    assert "a@example.com" not in event.arguments
    serialized = bundle.model_dump_json()
    assert "这段中间解释不应进入后台回顾" not in serialized
    assert "system_message" not in serialized
    assert "tool_schemas" not in serialized

    structured = memory._failure_review_bundle(
        _review_messages(final='{"status":"failed","summary":"查询失败"}'),
        reviewable_tools=frozenset({"database_query_preview"}),
    )
    assert structured is not None
    assert structured.final_status == "failed"

    no_tool = memory._failure_review_bundle(
        [HumanMessage(content="你好"), AIMessage(content="你好", id="final")],
        reviewable_tools=frozenset({"database_query_preview"}),
    )
    assert no_tool is None


def test_failure_review_uses_latest_visible_user_as_turn_boundary() -> None:
    messages: list = [
        HumanMessage(content="旧任务", id="old"),
        AIMessage(
            content="",
            tool_calls=[{"name": "execute", "args": {}, "id": "old-tool"}],
        ),
        ToolMessage(content="旧结果", tool_call_id="old-tool", name="execute"),
        HumanMessage(content="内部续跑", name="async-task-monitor"),
        HumanMessage(content="你好", id="new"),
        AIMessage(content="你好", id="final-new"),
    ]
    bundle = memory._failure_review_bundle(
        messages,
        reviewable_tools=frozenset({"execute"}),
    )

    assert bundle is None


@pytest.mark.asyncio
async def test_failure_review_middleware_enqueues_compact_bundle_once(monkeypatch) -> None:
    calls: list[dict] = []

    class Queue:
        async def get_memory_settings(self, _identity_hash):
            return memory.MemorySettings()

        async def enqueue(self, **kwargs):
            calls.append(kwargs)
            return True

        async def record_skipped_failure_review(self, **_kwargs):
            raise AssertionError("工具轨迹不应超限")

    monkeypatch.setattr(memory, "MEMORY_QUEUE", Queue())
    middleware = memory.FailureReviewMiddleware(
        agent_name="data-analyst",
        reviewable_tools={"database_query_preview"},
    )
    result = await middleware.aafter_agent(
        {"messages": _review_messages(result="空结果", final="分析完成")},
        _runtime(),
    )

    assert result is None
    assert len(calls) == 1
    assert calls[0]["kind"] == "failure_review"
    assert calls[0]["scope"] == "data-analyst"
    assert calls[0]["payload"]["bundle"]["tool_events"][0]["tool_name"] == "database_query_preview"
    assert calls[0]["source_user_hash"] == memory.user_hash(_runtime())
    assert calls[0]["settings_generation"] == 0
    assert len(calls[0]["idempotency_key"]) == 64


@pytest.mark.asyncio
async def test_failure_review_middleware_respects_disabled_setting(monkeypatch) -> None:
    class Queue:
        async def get_memory_settings(self, _identity_hash):
            return memory.MemorySettings(failure_lesson_saving_enabled=False, generation=2)

        async def enqueue(self, **_kwargs):
            raise AssertionError("关闭后不应入队")

    monkeypatch.setattr(memory, "MEMORY_QUEUE", Queue())
    middleware = memory.FailureReviewMiddleware(
        agent_name="data-analyst",
        reviewable_tools={"database_query_preview"},
    )

    assert await middleware.aafter_agent({"messages": _review_messages()}, _runtime()) is None


@pytest.mark.asyncio
async def test_failure_review_middleware_records_oversize_skip(monkeypatch) -> None:
    skipped: list[dict] = []

    class Queue:
        async def get_memory_settings(self, _identity_hash):
            return memory.MemorySettings()

        async def enqueue(self, **_kwargs):
            raise AssertionError("超限工具轨迹不应进入待处理队列")

        async def record_skipped_failure_review(self, **kwargs):
            skipped.append(kwargs)
            return True

    monkeypatch.setattr(memory, "MEMORY_QUEUE", Queue())
    monkeypatch.setattr(
        memory,
        "get_settings",
        lambda: SimpleNamespace(failure_review_bundle_max_bytes=1),
    )
    middleware = memory.FailureReviewMiddleware(
        agent_name="supervisor",
        reviewable_tools={"execute"},
    )
    messages = [
        HumanMessage(content="执行", id="turn-a"),
        AIMessage(content="", tool_calls=[{"name": "execute", "args": {}, "id": "exec-a"}]),
        ToolMessage(content="done", tool_call_id="exec-a", name="execute"),
        AIMessage(content="完成", id="final-a"),
    ]
    result = await middleware.aafter_agent(
        {"messages": messages},
        _runtime(),
    )

    assert result is None
    assert skipped[0]["scope"] == "supervisor"
    assert skipped[0]["bundle_bytes"] > 1


@pytest.mark.asyncio
async def test_failure_consolidator_uses_execution_evidence_scope(monkeypatch) -> None:
    captured: dict[str, str] = {}

    async def invoke(_schema, prompt):
        captured["prompt"] = prompt
        return memory.FailureDecision(action="discard")

    monkeypatch.setattr(memory, "_invoke_structured_memory_model", invoke)
    result = await memory._extract_failure_decision(
        agent_name="supervisor",
        content="重复委派造成同一报告被覆盖；改为单次委派后产物核验通过",
        evidence=[{"name": "task", "status": "success", "result": "产物核验通过"}],
        existing=[],
    )

    assert result.action == "discard"
    assert "工具、子任务、产物核验或其他确定性检查" in captured["prompt"]
    assert "只有执行证据能够确认" in captured["prompt"]
    assert "只有工具证据" not in captured["prompt"]


def test_failure_review_accepts_at_most_three_decisions() -> None:
    decisions = [
        memory.FailureDecision(action="add", lesson=_lesson(f"可复用失败经验 {index}"))
        for index in range(1, 4)
    ]
    result = memory.FailureReviewDecisions(decisions=decisions)
    assert len(result.decisions) == 3

    with pytest.raises(ValidationError):
        memory.FailureReviewDecisions(
            decisions=[
                *decisions,
                memory.FailureDecision(action="add", lesson=_lesson("第四条失败经验")),
            ]
        )


@pytest.mark.asyncio
async def test_failure_reviewer_uses_only_compact_bundle(monkeypatch) -> None:
    calls: list[dict] = []
    decisions = memory.FailureReviewDecisions(
        decisions=[memory.FailureDecision(action="add", lesson=_lesson())]
    )
    invocation_count = 0

    class Model:
        async def ainvoke(self, messages, config=None, **kwargs):
            nonlocal invocation_count
            invocation_count += 1
            calls.append(
                {"messages": list(messages), "config": config, "kwargs": kwargs}
            )
            return AIMessage(
                content=(
                    "{}"
                    if invocation_count == 1
                    else json.dumps(decisions.model_dump(mode="json"), ensure_ascii=False)
                ),
                usage_metadata={"input_tokens": 60, "output_tokens": 4, "total_tokens": 64},
            )

    monkeypatch.setattr(memory, "create_memory_model", lambda: Model())
    monkeypatch.setattr(
        memory,
        "get_settings",
        lambda: SimpleNamespace(
            memory_model="deepseek-memory",
            openai_model="deepseek-chat",
            failure_review_max_output_tokens=4096,
        ),
    )
    bundle = memory._failure_review_bundle(
        _review_messages(
            result="字段类型不匹配，读取结构后修复并验证成功",
            final="分析完成",
        ),
        reviewable_tools=frozenset({"database_query_preview"}),
    )
    assert bundle is not None

    result, stats = await memory._extract_failure_review_decisions(
        agent_name="data-analyst",
        bundle=bundle,
        existing=[],
    )

    assert [item.action for item in result.decisions] == ["add"]
    assert len(calls) == 2
    first_prompt = str(calls[0]["messages"][0].content)
    assert "本轮精简执行证据" in first_prompt
    assert "完整动态系统提示词" not in first_prompt
    assert "这段中间解释不应进入后台回顾" not in first_prompt
    assert calls[0]["config"]["callbacks"] == []
    assert calls[0]["kwargs"]["extra_body"] == {"max_tokens": 4096}
    assert stats["input_tokens"] == 60
    assert stats["output_tokens"] == 4
    assert stats["model"] == "deepseek-memory"
    assert "上一输出未通过 FailureReviewDecisions" in str(
        calls[1]["messages"][-1].content
    )


@pytest.mark.asyncio
async def test_failure_reviewer_rejects_tool_calls_and_never_executes_them(
    monkeypatch,
) -> None:
    decisions = memory.FailureReviewDecisions(
        decisions=[memory.FailureDecision(action="discard")]
    )
    responses = [
        AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "args": {"path": "/tmp/a"}, "id": "call-a"}],
        ),
        AIMessage(content=json.dumps(decisions.model_dump(mode="json"))),
    ]
    calls: list[list] = []

    class Model:
        async def ainvoke(self, messages, config=None, **_kwargs):
            calls.append(list(messages))
            return responses.pop(0)

    monkeypatch.setattr(memory, "create_memory_model", lambda: Model())
    monkeypatch.setattr(
        memory,
        "get_settings",
        lambda: SimpleNamespace(
            memory_model=None,
            openai_model="deepseek-chat",
            failure_review_max_output_tokens=4096,
        ),
    )
    bundle = memory._failure_review_bundle(
        [
            HumanMessage(content="读取文件", id="user-turn"),
            AIMessage(content="", tool_calls=[{"name": "read_file", "args": {}, "id": "query-a"}]),
            ToolMessage(content="已读取", tool_call_id="query-a", name="read_file"),
            AIMessage(content="完成", id="final-a"),
        ],
        reviewable_tools=frozenset({"read_file"}),
    )
    assert bundle is not None

    result, _stats = await memory._extract_failure_review_decisions(
        agent_name="supervisor",
        bundle=bundle,
        existing=[],
    )

    assert [item.action for item in result.decisions] == ["discard"]
    assert len(calls) == 2
    assert "严禁调用任何工具或返回 tool_calls" in str(calls[1][-1].content)


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
        backend=Backend(),
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
async def test_failure_review_job_adds_up_to_three_validated_public_lessons(monkeypatch) -> None:
    queue = InMemoryMemoryQueue()
    bundle = memory._failure_review_bundle(
        _review_messages(result="字段类型错误，读取结构后修复并验证成功", final="分析完成"),
        reviewable_tools=frozenset({"database_query_preview"}),
    )
    assert bundle is not None

    async def review(**_kwargs):
        lessons = [
            _lesson("查询前未确认字段类型"),
            _lesson("写入前未核验目标目录"),
            _lesson("生成后未检查产物完整性"),
        ]
        return (
            memory.FailureReviewDecisions(
                decisions=[
                    memory.FailureDecision(action="add", lesson=lesson)
                    for lesson in lessons
                ]
            ),
            {"actions": ["add", "add", "add"], "lesson_count": 3, "input_tokens": 100},
        )

    async def allowed(_job):
        return True

    monkeypatch.setattr(memory, "_extract_failure_review_decisions", review)
    monkeypatch.setattr(queue, "_failure_review_is_allowed", allowed)
    stats = await queue._process_failure_review(
        {
            "_id": "review-a",
            "scope": "data-analyst",
            "source_user_hash": "a" * 64,
            "settings_generation": 0,
            "payload": {"bundle": bundle.model_dump(mode="json")},
        }
    )

    records = await queue._active_failures("data-analyst")
    assert stats["actions"] == ["add", "add", "add"]
    assert len(records) == 3
    assert {record.lesson.title for record in records} == {
        "查询前未确认字段类型",
        "写入前未核验目标目录",
        "生成后未检查产物完整性",
    }


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
async def test_failure_review_enqueue_sets_temporary_payload_expiry(monkeypatch) -> None:
    updates: list[dict] = []

    class Jobs:
        async def update_one(self, _query, update, *, upsert):
            assert upsert is True
            updates.append(update)
            return SimpleNamespace(upserted_id="job-a")

    queue = memory.MemoryQueue()

    async def collections():
        return Jobs(), SimpleNamespace(), SimpleNamespace()

    monkeypatch.setattr(queue, "_collections", collections)
    monkeypatch.setattr(
        memory,
        "get_settings",
        lambda: SimpleNamespace(
            failure_review_payload_ttl_hours=24,
        ),
    )

    inserted = await queue.enqueue(
        kind="failure_review",
        scope="supervisor",
        idempotency_key="review-a",
        thread_digest="thread-a",
        payload={"bundle": {}},
        source_user_hash="a" * 64,
        settings_generation=3,
    )

    document = updates[0]["$setOnInsert"]
    assert inserted is True
    assert document["kind"] == "failure_review"
    assert document["payload"] == {"bundle": {}}
    assert document["available_at"] == document["created_at"]
    assert document["source_user_hash"] == "a" * 64
    assert document["settings_generation"] == 3
    assert "expires_at" in document


@pytest.mark.asyncio
async def test_memory_settings_default_enabled_and_disable_cancels_reviews(monkeypatch) -> None:
    memory_updates: list[tuple[dict, list]] = []
    job_updates: list[tuple[dict, dict]] = []

    class Memories:
        async def find_one(self, _query):
            return None

        async def find_one_and_update(
            self,
            query,
            update,
            *,
            upsert,
            return_document,
        ):
            assert upsert is True
            assert return_document is not None
            memory_updates.append((query, update))
            return {
                "failure_lesson_saving_enabled": False,
                "generation": 1,
            }

    class Jobs:
        async def update_many(self, query, update):
            job_updates.append((query, update))
            return SimpleNamespace(modified_count=2)

    queue = memory.MemoryQueue()

    async def collections():
        return Jobs(), SimpleNamespace(), Memories()

    monkeypatch.setattr(queue, "_collections", collections)
    identity_hash = "d" * 64

    assert await queue.get_memory_settings(identity_hash) == memory.MemorySettings()
    settings, cancelled = await queue.set_failure_lesson_saving(
        identity_hash,
        enabled=False,
    )

    assert settings.failure_lesson_saving_enabled is False
    assert settings.generation == 1
    assert cancelled == 2
    assert memory_updates[0][0]["namespace_str"] == f"{identity_hash}/memories/settings"
    assert memory_updates[0][0]["key"] == memory.MEMORY_SETTINGS_KEY
    job_query, job_update = job_updates[0]
    assert job_query["source_user_hash"] == identity_hash
    assert job_query["status"] == {"$in": ["pending", "retry", "processing"]}
    assert "payload" in job_update["$unset"]
    assert "source_user_hash" in job_update["$unset"]


@pytest.mark.asyncio
async def test_index_setup_cancels_legacy_full_context_reviews(monkeypatch) -> None:
    cleanup_updates: list[tuple[dict, dict]] = []

    class Collection:
        async def create_index(self, *_args, **_kwargs):
            return "index"

    class Jobs(Collection):
        async def update_many(self, query, update):
            cleanup_updates.append((query, update))
            return SimpleNamespace(modified_count=1)

    queue = memory.MemoryQueue()

    async def collections():
        return Jobs(), Collection(), Collection()

    monkeypatch.setattr(queue, "_collections", collections)
    await queue.ensure_indexes()

    query, update = cleanup_updates[0]
    assert query["payload.snapshot"] == {"$exists": True}
    assert query["status"] == {"$in": ["pending", "retry", "processing"]}
    assert update["$set"]["status"] == "cancelled"
    assert "payload" in update["$unset"]


@pytest.mark.asyncio
async def test_failure_review_permission_rejects_changed_generation(monkeypatch) -> None:
    queue = memory.MemoryQueue()

    async def settings(_identity_hash):
        return memory.MemorySettings(
            failure_lesson_saving_enabled=True,
            generation=4,
        )

    monkeypatch.setattr(queue, "get_memory_settings", settings)

    assert await queue._failure_review_is_allowed(
        {
            "_id": "job-a",
            "source_user_hash": "e" * 64,
            "settings_generation": 3,
        }
    ) is False


@pytest.mark.asyncio
async def test_user_memory_generation_discards_stale_job(monkeypatch) -> None:
    queue = InMemoryMemoryQueue()
    identity_hash = "b" * 64
    namespace = memory.user_memory_namespace_from_hash(identity_hash)
    queue.documents[(namespace, memory.USER_MEMORY_KEY)] = {
        "namespace": list(namespace),
        "namespace_str": "/".join(namespace),
        "key": memory.USER_MEMORY_KEY,
        "generation": 2,
        "value": {
            "content": memory._render_user_memory(memory.UserMemory()),
            "encoding": "utf-8",
        },
    }
    called = False

    async def extract(**_kwargs):
        nonlocal called
        called = True
        return memory.UserMemoryPatch(action="discard")

    monkeypatch.setattr(memory, "_extract_user_memory_patch", extract)
    await queue._process_user_memory(
        {
            "scope": identity_hash,
            "payload": {
                "memory_generation": 1,
                "user_message": "旧反馈",
            },
        }
    )

    assert called is False
    assert queue.documents[(namespace, memory.USER_MEMORY_KEY)]["generation"] == 2


@pytest.mark.asyncio
async def test_clear_user_memory_resets_content_and_cancels_only_user_jobs(
    monkeypatch,
) -> None:
    memory_updates: list[tuple[dict, dict]] = []
    job_updates: list[tuple[dict, dict]] = []

    class Memories:
        async def update_one(self, query, update, *, upsert):
            assert upsert is True
            memory_updates.append((query, update))
            return SimpleNamespace(upserted_id=None, matched_count=1)

    class Jobs:
        async def update_many(self, query, update):
            job_updates.append((query, update))
            return SimpleNamespace(modified_count=3)

    queue = memory.MemoryQueue()

    async def collections():
        return Jobs(), SimpleNamespace(), Memories()

    monkeypatch.setattr(queue, "_collections", collections)
    identity_hash = "c" * 64

    cancelled = await queue.clear_user_memory(identity_hash)

    assert cancelled == 3
    memory_query, memory_update = memory_updates[0]
    assert memory_query["namespace_str"] == f"{identity_hash}/memories/user"
    assert memory_update["$inc"] == {"generation": 1}
    restored = memory._parse_user_memory(memory_update["$set"]["value"]["content"])
    assert restored == memory.UserMemory()
    job_query, job_update = job_updates[0]
    assert job_query["scope"] == identity_hash
    assert job_query["kind"] == "user_memory"
    assert "payload" in job_update["$unset"]


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
async def test_memory_job_uses_the_independent_total_timeout(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class TimeoutContext:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return False

    def timeout(seconds):
        observed["timeout"] = seconds
        return TimeoutContext()

    queue = memory.MemoryQueue()

    async def process(_job):
        return {"actions": []}

    async def succeeded(_job, *, stats=None):
        observed["stats"] = stats

    monkeypatch.setattr(memory.asyncio, "timeout", timeout)
    monkeypatch.setattr(
        memory,
        "get_settings",
        lambda: SimpleNamespace(memory_job_timeout_seconds=75),
    )
    monkeypatch.setattr(queue, "_process_failure_review", process)
    monkeypatch.setattr(queue, "_mark_succeeded", succeeded)

    await queue._process_job({"kind": "failure_review"})

    assert observed == {"timeout": 75, "stats": {"actions": []}}


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
        {"_id": "job-a", "attempts": 1, "kind": "failure_review"},
        OutputParserException("bad json"),
        retryable=False,
    )

    update = updates[0][1]["$set"]
    assert update["status"] == "failed"
    assert "expires_at" in update
    assert "payload" in updates[0][1]["$unset"]


@pytest.mark.asyncio
async def test_successful_review_job_drops_payload_and_keeps_stats(monkeypatch) -> None:
    updates: list[tuple[dict, dict]] = []

    class Jobs:
        async def update_one(self, query, update):
            updates.append((query, update))

    queue = memory.MemoryQueue()

    async def collections():
        return Jobs(), SimpleNamespace(), SimpleNamespace()

    monkeypatch.setattr(queue, "_collections", collections)
    await queue._mark_succeeded(
        {"_id": "job-a", "kind": "failure_review"},
        stats={"action": "discard", "input_tokens": 100},
    )

    update = updates[0][1]
    assert update["$set"]["status"] == "succeeded"
    assert update["$set"]["review_stats"]["input_tokens"] == 100
    assert "payload" in update["$unset"]
    assert "source_user_hash" in update["$unset"]


def test_legacy_automatic_middlewares_are_removed() -> None:
    assert not hasattr(memory, "UserPreferenceUpdateMiddleware")
    assert not hasattr(memory, "AgentExperienceEnqueueMiddleware")
    assert not hasattr(memory, "PREFERENCE_UPDATE_GRAPH")
