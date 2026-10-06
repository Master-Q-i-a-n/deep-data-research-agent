"""Opt-in real MongoDB checks, isolated in a disposable test database."""

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from pymongo import AsyncMongoClient

from deep_data_research_agent.memory import service as memory


@pytest_asyncio.fixture
async def mongo_store(monkeypatch):
    uri = os.environ.get("TEST_MONGODB_URI")
    if not uri:
        pytest.skip("Set TEST_MONGODB_URI to run real MongoDB memory checks")
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=3000)
    database_name = f"ddra_memory_test_{uuid.uuid4().hex}"
    database = client[database_name]
    queue = memory.MemoryQueue()

    async def collections():
        return database.jobs, database.leases, database.memories

    monkeypatch.setattr(queue, "_collections", collections)
    created = False
    try:
        await client.admin.command("ping")
        await database.create_collection("memories")
        created = True
        yield queue, database
    finally:
        # Only our unique test database is removed, never the configured application DB.
        assert database_name.startswith("ddra_memory_test_")
        try:
            if created:
                await client.drop_database(database_name)
        finally:
            await client.close()


@pytest.mark.integration
@pytest.mark.parametrize("legacy_document", [False, True])
@pytest.mark.asyncio
async def test_mongodb_concurrent_updates_and_first_insert(
    mongo_store, monkeypatch, legacy_document
):
    queue, database = mongo_store
    identity = "a" * 64
    query = {
        "namespace_str": f"{identity}/memories/user",
        "key": memory.USER_MEMORY_KEY,
    }
    if legacy_document:
        await database.memories.insert_one(
            {
                **query,
                "value": {"content": memory._render_user_memory(memory.UserMemory())},
            }
        )
    original_write = queue._put_user_memory_file
    ready = asyncio.Event()
    arrivals = 0

    async def overlapping_write(*args, **kwargs):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        if arrivals <= 2:
            await ready.wait()
        return await original_write(*args, **kwargs)

    monkeypatch.setattr(queue, "_put_user_memory_file", overlapping_write)
    results = await asyncio.gather(
        queue.update_user_memory(
            identity, memory.UserMemoryEdit(field="preferred_currency", value="CNY")
        ),
        queue.update_user_memory(
            identity, memory.UserMemoryEdit(field="preferred_language", value="zh")
        ),
    )
    assert all(result["status"] == "updated" for result in results)
    document = await database.memories.find_one(query)
    assert await database.memories.count_documents(query) == 1
    assert document["version"] == 2
    parsed = memory._parse_user_memory(document["value"]["content"], strict=True)
    assert parsed.preferences == memory.UserPreferences(
        preferred_currency="CNY", preferred_language="zh"
    )
    assert parsed.model_dump(mode="json") == document["value"]["memory"]
    assert arrivals == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mongodb_clear_prevents_stale_update(mongo_store, monkeypatch):
    queue, database = mongo_store
    identity = "b" * 64
    await queue.update_user_memory(
        identity, memory.UserMemoryEdit(field="preferred_language", value="en")
    )
    original_write = queue._put_user_memory_file

    async def clear_then_write(*args, **kwargs):
        monkeypatch.setattr(queue, "_put_user_memory_file", original_write)
        await queue.clear_user_memory(identity)
        return await original_write(*args, **kwargs)

    monkeypatch.setattr(queue, "_put_user_memory_file", clear_then_write)
    with pytest.raises(ValueError, match="已被清空"):
        await queue.update_user_memory(
            identity, memory.UserMemoryEdit(field="preferred_currency", value="CNY")
        )
    document = await database.memories.find_one(
        {"namespace_str": f"{identity}/memories/user"}
    )
    assert document["version"] == 2
    assert document["generation"] == 1
    assert (
        memory._parse_user_memory(document["value"]["content"]) == memory.UserMemory()
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mongodb_cancels_legacy_user_jobs_only(mongo_store):
    queue, database = mongo_store
    await database.jobs.insert_many(
        [
            {
                "kind": "user_memory",
                "status": "pending",
                "payload": {"user_message": "旧反馈"},
            },
            {
                "kind": "failure_lesson",
                "status": "pending",
                "payload": {"content": "失败经验"},
            },
        ]
    )
    await queue.ensure_indexes()
    user_job = await database.jobs.find_one({"kind": "user_memory"})
    failure_job = await database.jobs.find_one({"kind": "failure_lesson"})
    assert user_job["status"] == "cancelled"
    assert "payload" not in user_job
    assert failure_job["status"] == "pending"
    assert failure_job["payload"]["content"] == "失败经验"
