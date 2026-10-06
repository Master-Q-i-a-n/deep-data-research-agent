"""Exercise synchronous edits through the actual MongoDB conditional-write path."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from pymongo.errors import DuplicateKeyError

from deep_data_research_agent.memory import service as memory


class MemoryCollection:
    """Minimal atomic Mongo semantics, including unique-key upsert conflicts."""

    def __init__(self):
        self.documents = {}
        self.writes = []
        self.before_write = None

    async def create_index(self, *_args, **_kwargs):
        return "index"

    @staticmethod
    def matches(document, query):
        for name, expected in query.items():
            if name == "$and":
                if not all(
                    MemoryCollection.matches(document, item) for item in expected
                ):
                    return False
            elif name == "$or":
                if not any(
                    MemoryCollection.matches(document, item) for item in expected
                ):
                    return False
            elif isinstance(expected, dict) and "$exists" in expected:
                if (name in document) != expected["$exists"]:
                    return False
            elif document.get(name) != expected:
                return False
        return True

    async def find_one(self, query):
        document = self.documents.get((query["namespace_str"], query["key"]))
        return copy.deepcopy(document) if document else None

    async def update_one(self, query, update, *, upsert):
        self.writes.append(copy.deepcopy((query, update, upsert)))
        if self.before_write:
            await self.before_write(query, update)
        key = (query["namespace_str"], query["key"])
        current = self.documents.get(key)
        matched = current is not None and self.matches(current, query)
        if not matched:
            if not upsert:
                return SimpleNamespace(matched_count=0, upserted_id=None)
            if current is not None:
                raise DuplicateKeyError("unique namespace/key")
            current = {
                "namespace_str": key[0],
                "key": key[1],
                **update.get("$setOnInsert", {}),
            }
        current = copy.deepcopy(current)
        current.update(update.get("$set", {}))
        for field, increment in update.get("$inc", {}).items():
            current[field] = current.get(field, 0) + increment
        self.documents[key] = current
        return SimpleNamespace(
            matched_count=int(matched), upserted_id=None if matched else "new"
        )


class JobsCollection:
    def __init__(self):
        self.updates = []

    async def create_index(self, *_args, **_kwargs):
        return "index"

    async def update_many(self, query, update):
        self.updates.append((query, update))
        return SimpleNamespace(modified_count=0)


@pytest.fixture
def store(monkeypatch):
    queue = memory.MemoryQueue()
    collection = MemoryCollection()
    jobs = JobsCollection()

    async def collections():
        return jobs, None, collection

    monkeypatch.setattr(queue, "_collections", collections)
    monkeypatch.setattr(memory, "MEMORY_QUEUE", queue)
    return queue, collection, jobs


def document_key(identity="a" * 64):
    return (f"{identity}/memories/user", memory.USER_MEMORY_KEY)


def put_document(collection, current, *, version=None, generation=0, identity="a" * 64):
    key = document_key(identity)
    collection.documents[key] = {
        "namespace_str": key[0],
        "key": key[1],
        "generation": generation,
        "value": {
            "content": memory._render_user_memory(current),
            "memory": current.model_dump(mode="json"),
        },
    }
    if version is not None:
        collection.documents[key]["version"] = version


def restored(collection, identity="a" * 64):
    value = collection.documents[document_key(identity)]["value"]
    parsed = memory._parse_user_memory(value["content"], strict=True)
    assert parsed.model_dump(mode="json") == value["memory"]
    return parsed


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("preferred_output", "TABLE", "table"),
        ("preferred_chart_type", "LINE", "line"),
        ("preferred_currency", " cny ", "CNY"),
        ("preferred_language", "ZH", "zh"),
    ],
)
@pytest.mark.asyncio
async def test_scalar_update_preserves_other_fields_and_legacy_version(
    store, field, value, expected
):
    queue, collection, _jobs = store
    current = memory.UserMemory(
        avoid_behaviors=["不要重复委派"], reinforce_behaviors=["继续提供依据"]
    )
    put_document(collection, current)
    result = await queue.update_user_memory(
        "a" * 64, memory.UserMemoryEdit(field=field, value=value)
    )
    assert result["status"] == "updated"
    assert result["value"] == expected
    assert result["version"] == 1
    after = restored(collection)
    assert getattr(after.preferences, field) == expected
    assert after.avoid_behaviors == current.avoid_behaviors
    assert after.reinforce_behaviors == current.reinforce_behaviors


@pytest.mark.asyncio
async def test_clear_scalar_and_noop_do_not_rewrite(store):
    queue, collection, _jobs = store
    put_document(
        collection,
        memory.UserMemory(preferences=memory.UserPreferences(preferred_currency="USD")),
        version=4,
    )
    edit = memory.UserMemoryEdit(field="preferred_currency", value=None)
    assert (await queue.update_user_memory("a" * 64, edit))["version"] == 5
    saved = copy.deepcopy(collection.documents)
    assert (await queue.update_user_memory("a" * 64, edit))["status"] == "unchanged"
    assert collection.documents == saved
    assert len(collection.writes) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"field": "unknown", "value": "x"},
        {"field": "preferred_language"},
        {"field": "preferred_language", "value": "bad language"},
        {"field": "preferred_currency", "value": "US"},
        {"field": "preferred_output", "value": "pdf"},
        {"field": "preferred_language", "action": "remove"},
        {"field": "avoid_behaviors", "value": "整表替换"},
        {"field": "avoid_behaviors", "action": "replace", "value": "新内容"},
        {
            "field": "avoid_behaviors",
            "action": "remove",
            "old_text": "原内容",
            "value": "新内容",
        },
        {"field": "avoid_behaviors", "action": "add", "value": " "},
        {"field": "avoid_behaviors", "action": "add", "value": "长" * 181},
        {
            "field": "avoid_behaviors",
            "action": "add",
            "value": "不要访问 https://private.example",
        },
        {"field": "preferred_language", "value": "zh", "user_id": "other-user"},
    ],
)
def test_edit_schema_rejects_invalid_operations(kwargs):
    with pytest.raises(ValueError):
        memory.UserMemoryEdit.model_validate(kwargs)


@pytest.mark.asyncio
async def test_behavior_add_replace_remove_and_duplicate(store):
    queue, collection, _jobs = store
    put_document(
        collection, memory.UserMemory(avoid_behaviors=["不要冗长报告", "不要重复委派"])
    )
    duplicate = memory.UserMemoryEdit(
        field="avoid_behaviors", action="add", value="不要重复委派"
    )
    assert (await queue.update_user_memory("a" * 64, duplicate))[
        "status"
    ] == "unchanged"
    replacement = memory.UserMemoryEdit(
        field="avoid_behaviors",
        action="replace",
        old_text="不要冗长报告",
        value="不要省略关键依据",
    )
    await queue.update_user_memory("a" * 64, replacement)
    assert restored(collection).avoid_behaviors == ["不要省略关键依据", "不要重复委派"]
    await queue.update_user_memory(
        "a" * 64,
        memory.UserMemoryEdit(
            field="avoid_behaviors", action="remove", old_text="不要重复委派"
        ),
    )
    assert restored(collection).avoid_behaviors == ["不要省略关键依据"]
    await queue.update_user_memory(
        "a" * 64,
        memory.UserMemoryEdit(
            field="reinforce_behaviors", action="add", value="不要省略关键依据"
        ),
    )
    assert restored(collection).avoid_behaviors == []
    assert restored(collection).reinforce_behaviors == ["不要省略关键依据"]


@pytest.mark.asyncio
async def test_capacity_and_unmatched_entry_do_not_change_memory(store):
    queue, collection, _jobs = store
    current = memory.UserMemory(avoid_behaviors=[f"行为{index}" for index in range(20)])
    put_document(collection, current)
    with pytest.raises(ValueError, match="已满"):
        await queue.update_user_memory(
            "a" * 64,
            memory.UserMemoryEdit(
                field="avoid_behaviors", action="add", value="新行为"
            ),
        )
    with pytest.raises(memory.UserMemoryEditConflict) as error:
        await queue.update_user_memory(
            "a" * 64,
            memory.UserMemoryEdit(
                field="avoid_behaviors", action="remove", old_text="不存在"
            ),
        )
    assert error.value.entries == current.avoid_behaviors
    assert restored(collection) == current
    assert collection.writes == []


@pytest.mark.parametrize("initial_exists", [False, True])
@pytest.mark.asyncio
async def test_concurrent_writes_preserve_both_fields_even_on_first_insert(
    store, initial_exists
):
    queue, collection, _jobs = store
    if initial_exists:
        put_document(collection, memory.UserMemory())
    # Force both calls to reach their first write with the same read snapshot.
    ready = asyncio.Event()
    arrivals = 0

    async def barrier(_query, _update):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        if arrivals <= 2:
            await ready.wait()

    collection.before_write = barrier
    results = await asyncio.wait_for(
        asyncio.gather(
            queue.update_user_memory(
                "a" * 64, memory.UserMemoryEdit(field="preferred_currency", value="CNY")
            ),
            queue.update_user_memory(
                "a" * 64, memory.UserMemoryEdit(field="preferred_language", value="zh")
            ),
        ),
        timeout=2,
    )
    assert all(result["status"] == "updated" for result in results)
    assert restored(collection).preferences == memory.UserPreferences(
        preferred_currency="CNY", preferred_language="zh"
    )
    assert collection.documents[document_key()]["version"] == 2
    assert len(collection.writes) == 3
    assert (
        collection.writes[-1][2] is False
    )  # Existing version must never be recreated by upsert.


@pytest.mark.asyncio
async def test_same_field_reapplies_requested_value_after_conflict(store):
    queue, collection, _jobs = store
    put_document(collection, memory.UserMemory(), version=2)

    async def other_writer(_query, _update):
        collection.before_write = None
        put_document(
            collection,
            memory.UserMemory(
                preferences=memory.UserPreferences(preferred_currency="USD")
            ),
            version=3,
        )

    collection.before_write = other_writer
    await queue.update_user_memory(
        "a" * 64, memory.UserMemoryEdit(field="preferred_currency", value="CNY")
    )
    assert restored(collection).preferences.preferred_currency == "CNY"
    assert collection.documents[document_key()]["version"] == 4


@pytest.mark.asyncio
async def test_changed_behavior_returns_conflict_instead_of_overwriting(store):
    queue, collection, _jobs = store
    put_document(collection, memory.UserMemory(avoid_behaviors=["旧条目"]), version=1)

    async def other_writer(_query, _update):
        collection.before_write = None
        put_document(
            collection,
            memory.UserMemory(avoid_behaviors=["另一个会话的新条目"]),
            version=2,
        )

    collection.before_write = other_writer
    with pytest.raises(memory.UserMemoryEditConflict):
        await queue.update_user_memory(
            "a" * 64,
            memory.UserMemoryEdit(
                field="avoid_behaviors",
                action="replace",
                old_text="旧条目",
                value="本次新条目",
            ),
        )
    assert restored(collection).avoid_behaviors == ["另一个会话的新条目"]


@pytest.mark.asyncio
async def test_clear_invalidates_inflight_update(store):
    queue, collection, _jobs = store
    put_document(collection, memory.UserMemory(), version=1)

    async def clear_before_write(_query, _update):
        collection.before_write = None
        await queue.clear_user_memory("a" * 64)

    collection.before_write = clear_before_write
    with pytest.raises(ValueError, match="已被清空"):
        await queue.update_user_memory(
            "a" * 64, memory.UserMemoryEdit(field="preferred_currency", value="CNY")
        )
    assert restored(collection) == memory.UserMemory()
    assert collection.documents[document_key()]["version"] == 2
    assert collection.documents[document_key()]["generation"] == 1


@pytest.mark.asyncio
async def test_conflicts_retry_only_three_times(store):
    queue, collection, _jobs = store
    put_document(collection, memory.UserMemory(), version=1)

    async def competing_write(_query, _update):
        collection.documents[document_key()]["version"] += 1

    collection.before_write = competing_write
    with pytest.raises(ValueError, match="连续冲突"):
        await queue.update_user_memory(
            "a" * 64, memory.UserMemoryEdit(field="preferred_currency", value="CNY")
        )
    assert len(collection.writes) == 3
    assert restored(collection).preferences.preferred_currency is None


@pytest.mark.parametrize(
    "content",
    [None, "broken", "---\n- invalid\n---\n", "---\navoid_behaviors: invalid\n---\n"],
)
@pytest.mark.asyncio
async def test_corrupt_existing_memory_is_not_overwritten(store, content):
    queue, collection, _jobs = store
    collection.documents[document_key()] = {"value": {"content": content}}
    with pytest.raises(ValueError, match="拒绝覆盖"):
        await queue.update_user_memory(
            "a" * 64, memory.UserMemoryEdit(field="preferred_language", value="zh")
        )
    assert collection.writes == []


@pytest.mark.asyncio
async def test_write_ack_timeout_is_uncertain_and_not_retried(store):
    queue, collection, _jobs = store
    put_document(collection, memory.UserMemory(), version=1)

    async def ack_lost(_query, _update):
        raise TimeoutError("acknowledgement unavailable")

    collection.before_write = ack_lost
    with pytest.raises(memory.MemoryWriteUncertain):
        await queue.update_user_memory(
            "a" * 64, memory.UserMemoryEdit(field="preferred_language", value="zh")
        )
    assert len(collection.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,success",
    [
        ({"field": "preferred_currency", "value": "cny"}, True),
        ({"field": "preferred_currency"}, False),
        ({"field": "unknown", "value": "cny"}, False),
        (
            {"field": "preferred_currency", "value": "cny", "user_id": "other-user"},
            False,
        ),
    ],
)
async def test_toolnode_injects_runtime_and_returns_actual_write_result(
    store, arguments, success
):
    _queue, collection, _jobs = store
    call = {
        "name": "capture_user_memory",
        "id": "memory-call",
        "type": "tool_call",
        "args": arguments,
    }
    builder = StateGraph(memory.AgentState)
    builder.add_node("tools", ToolNode([memory.capture_user_memory]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    output = await builder.compile().ainvoke(
        {
            "messages": [
                HumanMessage(content="以后用人民币"),
                AIMessage(content="", tool_calls=[call]),
            ]
        },
        config={
            "configurable": {
                "langgraph_auth_user_id": "user-a",
                "thread_id": "thread-a",
            }
        },
    )
    message = output["messages"][-1]
    assert message.tool_call_id == "memory-call"
    if not success:
        assert message.status == "error"
        assert collection.documents == {}
        return
    assert message.status == "success", message.content
    assert json.loads(message.content)["value"] == "CNY"
    key = next(iter(collection.documents))
    assert (
        key[0]
        == f"{memory.user_hash(SimpleNamespace(config={'configurable': {'langgraph_auth_user_id': 'user-a'}}, server_info=None))}/memories/user"
    )


@pytest.mark.asyncio
async def test_tool_reports_uncertain_and_requires_visible_user(store):
    _queue, collection, _jobs = store
    runtime = SimpleNamespace(
        state={"messages": []},
        tool_call_id="memory-call",
        server_info=None,
        config={"configurable": {"langgraph_auth_user_id": "user-a"}},
    )
    missing_user = await memory.capture_user_memory.coroutine(
        field="preferred_language", value="zh", runtime=runtime
    )
    assert missing_user.status == "error"
    assert json.loads(missing_user.content)["status"] == "failed"
    assert collection.writes == []

    runtime.state["messages"] = [HumanMessage(content="以后用中文")]

    async def ack_lost(_query, _update):
        raise TimeoutError("ack unavailable")

    collection.before_write = ack_lost
    uncertain = await memory.capture_user_memory.coroutine(
        field="preferred_language", value="zh", runtime=runtime
    )
    assert uncertain.status == "error"
    assert json.loads(uncertain.content)["status"] == "uncertain"
    assert len(collection.writes) == 1


@pytest.mark.asyncio
async def test_index_setup_cancels_old_user_jobs_without_touching_memories(store):
    queue, collection, jobs = store
    await queue.ensure_indexes()
    query, update = jobs.updates[-1]
    assert query == {
        "kind": "user_memory",
        "status": {"$in": ["pending", "retry", "processing"]},
    }
    assert update["$set"]["status"] == "cancelled"
    assert update["$set"]["skip_reason"] == "user_memory_now_synchronous"
    assert "payload" in update["$unset"]
    assert collection.documents == {}


@pytest.mark.asyncio
async def test_legacy_job_cannot_call_model_or_write(store, monkeypatch):
    queue, collection, _jobs = store
    cancelled = []

    async def mark_cancelled(_job, reason):
        cancelled.append(reason)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Legacy user jobs must not invoke an LLM")

    monkeypatch.setattr(queue, "_mark_cancelled", mark_cancelled)
    monkeypatch.setattr(memory, "get_runtime_model", forbidden)
    assert (
        await queue._process_job(
            {"kind": "user_memory", "payload": {"user_message": "旧反馈"}}
        )
        is None
    )
    assert cancelled == ["user_memory_now_synchronous"]
    assert collection.documents == {}
    with pytest.raises(ValueError, match="同步工具"):
        await queue.enqueue(
            kind="user_memory",
            scope="a" * 64,
            idempotency_key="old",
            thread_digest="thread",
            payload={},
        )
