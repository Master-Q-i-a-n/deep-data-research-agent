from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from deep_data_research_agent.database import repository as database
from deep_data_research_agent.infrastructure.postgres.checkpointer import (
    UserOwnedPostgresCheckpointer,
    configured_user_id,
)
from deep_data_research_agent.infrastructure.sandbox import manager as sandbox_manager


class FakePostgresSaver:
    """Small delegate used to test ownership behavior without PostgreSQL."""

    def __init__(self) -> None:
        self.serde = JsonPlusSerializer()
        self.deleted_threads: list[str] = []
        self.copied_threads: list[tuple[str, str]] = []
        self.pruned_threads: list[tuple[list[str], str]] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted_threads.append(thread_id)

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        self.copied_threads.append((source_thread_id, target_thread_id))

    async def aprune(self, thread_ids: list[str], *, strategy: str) -> None:
        self.pruned_threads.append((thread_ids, strategy))

    async def aget_tuple(self, _config: Any) -> None:
        return None


def test_configured_user_id_supports_agent_server_fields() -> None:
    assert configured_user_id(
        {"configurable": {"langgraph_auth_user_id": "user-a"}}
    ) == "user-a"
    assert configured_user_id(
        {"configurable": {"langgraph_auth_user": {"identity": "user-b"}}}
    ) == "user-b"


@pytest.mark.asyncio
async def test_config_user_claims_thread_before_checkpoint_write(monkeypatch) -> None:
    claims: list[tuple[str, str]] = []

    async def claim(thread_id: str, user_id: str) -> None:
        claims.append((thread_id, user_id))

    monkeypatch.setattr(database, "claim_thread", claim)
    checkpointer = UserOwnedPostgresCheckpointer(FakePostgresSaver())  # type: ignore[arg-type]
    user_id = await checkpointer._user_for_config(
        {
            "configurable": {
                "thread_id": "thread-a",
                "langgraph_auth_user_id": "user-a",
            }
        },
        claim=True,
    )

    assert user_id == "user-a"
    assert claims == [("thread-a", "user-a")]


@pytest.mark.asyncio
async def test_read_rejects_another_users_thread(monkeypatch) -> None:
    async def get_owner(_thread_id: str) -> str:
        return "user-b"

    monkeypatch.setattr(database, "get_thread_owner", get_owner)
    checkpointer = UserOwnedPostgresCheckpointer(FakePostgresSaver())  # type: ignore[arg-type]

    with pytest.raises(database.ThreadOwnershipError, match="不属于当前用户"):
        await checkpointer.aget_tuple(
            {
                "configurable": {
                    "thread_id": "thread-a",
                    "langgraph_auth_user_id": "user-a",
                }
            }
        )


@pytest.mark.asyncio
async def test_delete_thread_removes_checkpoint_and_owner_claim(monkeypatch) -> None:
    deleted_claims: list[tuple[str, str]] = []
    deleted_resources: list[tuple[str, str]] = []

    async def get_owner(_thread_id: str) -> str:
        return "user-a"

    async def delete_claim(thread_id: str, user_id: str) -> None:
        deleted_claims.append((thread_id, user_id))

    async def delete_resources(thread_id: str, *, user_id: str) -> None:
        deleted_resources.append((thread_id, user_id))

    monkeypatch.setattr(database, "get_thread_owner", get_owner)
    monkeypatch.setattr(database, "delete_thread_claim", delete_claim)
    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER,
        "delete_thread_resources",
        delete_resources,
    )
    saver = FakePostgresSaver()
    checkpointer = UserOwnedPostgresCheckpointer(saver)  # type: ignore[arg-type]
    await checkpointer.adelete_thread("thread-a")

    assert saver.deleted_threads == ["thread-a"]
    assert deleted_claims == [("thread-a", "user-a")]
    assert deleted_resources == [("thread-a", "user-a")]


@pytest.mark.asyncio
async def test_delete_thread_continues_when_external_resource_cleanup_fails(
    monkeypatch,
) -> None:
    """An unavailable sandbox service must not make conversations undeletable."""

    deleted_claims: list[tuple[str, str]] = []

    async def get_owner(_thread_id: str) -> str:
        return "user-a"

    async def delete_claim(thread_id: str, user_id: str) -> None:
        deleted_claims.append((thread_id, user_id))

    async def fail_delete_resources(_thread_id: str, *, user_id: str) -> None:
        del user_id
        raise ConnectionError("sandbox unavailable")

    monkeypatch.setattr(database, "get_thread_owner", get_owner)
    monkeypatch.setattr(database, "delete_thread_claim", delete_claim)
    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER,
        "delete_thread_resources",
        fail_delete_resources,
    )
    saver = FakePostgresSaver()
    checkpointer = UserOwnedPostgresCheckpointer(saver)  # type: ignore[arg-type]

    await checkpointer.adelete_thread("thread-a")

    assert saver.deleted_threads == ["thread-a"]
    assert deleted_claims == [("thread-a", "user-a")]


@pytest.mark.asyncio
async def test_copy_failure_rolls_back_target_claim(monkeypatch) -> None:
    deleted_claims: list[tuple[str, str]] = []

    async def get_owner(thread_id: str) -> str | None:
        return "user-a" if thread_id == "source" else None

    async def claim(_thread_id: str, _user_id: str) -> None:
        return None

    async def delete_claim(thread_id: str, user_id: str) -> None:
        deleted_claims.append((thread_id, user_id))

    saver = FakePostgresSaver()

    async def fail_copy(_source: str, _target: str) -> None:
        raise RuntimeError("copy failed")

    saver.acopy_thread = fail_copy  # type: ignore[method-assign]
    monkeypatch.setattr(database, "get_thread_owner", get_owner)
    monkeypatch.setattr(database, "claim_thread", claim)
    monkeypatch.setattr(database, "delete_thread_claim", delete_claim)
    checkpointer = UserOwnedPostgresCheckpointer(saver)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="copy failed"):
        await checkpointer.acopy_thread("source", "target")
    assert deleted_claims == [("target", "user-a")]


@pytest.mark.asyncio
async def test_copy_failure_keeps_preexisting_target_claim(monkeypatch) -> None:
    deleted_claims: list[tuple[str, str]] = []

    async def get_owner(_thread_id: str) -> str:
        return "user-a"

    async def claim(_thread_id: str, _user_id: str) -> None:
        return None

    async def delete_claim(thread_id: str, user_id: str) -> None:
        deleted_claims.append((thread_id, user_id))

    saver = FakePostgresSaver()

    async def fail_copy(_source: str, _target: str) -> None:
        raise RuntimeError("copy failed")

    saver.acopy_thread = fail_copy  # type: ignore[method-assign]
    monkeypatch.setattr(database, "get_thread_owner", get_owner)
    monkeypatch.setattr(database, "claim_thread", claim)
    monkeypatch.setattr(database, "delete_thread_claim", delete_claim)
    checkpointer = UserOwnedPostgresCheckpointer(saver)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="copy failed"):
        await checkpointer.acopy_thread("source", "target")
    assert deleted_claims == []
