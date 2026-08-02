import asyncio

import pytest

from deep_data_research_agent import database
from deep_data_research_agent.user_checkpointer import (
    UserScopedSqliteCheckpointer,
    configured_user_id,
    storage_user_id,
)


def test_configured_user_id_supports_agent_server_fields() -> None:
    assert configured_user_id(
        {"configurable": {"langgraph_auth_user_id": "user-a"}}
    ) == "user-a"
    assert configured_user_id(
        {"configurable": {"langgraph_auth_user": {"identity": "user-b"}}}
    ) == "user-b"


def test_unsafe_storage_identity_is_hashed() -> None:
    assert storage_user_id("local-user") == "local-user"
    assert "/" not in storage_user_id("tenant/user")


@pytest.mark.asyncio
async def test_users_get_separate_lazy_sqlite_files(tmp_path) -> None:
    checkpointer = UserScopedSqliteCheckpointer(tmp_path)
    try:
        first, duplicate, second = await asyncio.gather(
            checkpointer._saver("user-a"),
            checkpointer._saver("user-a"),
            checkpointer._saver("user-b"),
        )
        assert first is duplicate
        assert first is not second
        assert (tmp_path / "user-a" / "checkpoints.sqlite").is_file()
        assert (tmp_path / "user-b" / "checkpoints.sqlite").is_file()
    finally:
        await checkpointer.aclose()


@pytest.mark.asyncio
async def test_config_user_claims_thread_before_checkpoint_write(monkeypatch, tmp_path) -> None:
    claims: list[tuple[str, str]] = []

    async def claim(thread_id: str, user_id: str) -> None:
        claims.append((thread_id, user_id))

    monkeypatch.setattr(database, "claim_thread", claim)
    checkpointer = UserScopedSqliteCheckpointer(tmp_path)
    try:
        user_id = await checkpointer._user_for_config(
            {
                "configurable": {
                    "thread_id": "thread-a",
                    "langgraph_auth_user_id": "user-a",
                }
            },
            claim=True,
        )
    finally:
        await checkpointer.aclose()

    assert user_id == "user-a"
    assert claims == [("thread-a", "user-a")]


@pytest.mark.asyncio
async def test_delete_thread_removes_checkpoint_and_owner_claim(monkeypatch, tmp_path) -> None:
    deleted_claims: list[tuple[str, str]] = []

    async def get_owner(_thread_id: str) -> str:
        return "user-a"

    async def delete_claim(thread_id: str, user_id: str) -> None:
        deleted_claims.append((thread_id, user_id))

    monkeypatch.setattr(database, "get_thread_owner", get_owner)
    monkeypatch.setattr(database, "delete_thread_claim", delete_claim)
    checkpointer = UserScopedSqliteCheckpointer(tmp_path)
    try:
        await checkpointer.adelete_thread("thread-a")
    finally:
        await checkpointer.aclose()

    assert deleted_claims == [("thread-a", "user-a")]
