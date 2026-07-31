from types import SimpleNamespace

import pytest
from starlette.requests import Request

from deep_data_research_agent import auth as auth_module
from deep_data_research_agent import database


def _request(authorization: str | None = None) -> Request:
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_bearer_token_accepts_empty_and_valid_header() -> None:
    assert auth_module.bearer_token(None) is None
    assert auth_module.bearer_token("Bearer token-value") == "token-value"


def test_bearer_token_rejects_malformed_header() -> None:
    with pytest.raises(Exception, match="登录凭据格式无效"):
        auth_module.bearer_token("Basic abc")


@pytest.mark.asyncio
async def test_missing_token_uses_shared_default_user(monkeypatch) -> None:
    async def ensure_schema() -> None:
        return None

    monkeypatch.setattr(database, "ensure_schema", ensure_schema)
    user = await auth_module.authenticate_request(_request())

    assert user["identity"] == "local-user"
    assert user["is_authenticated"] is False


@pytest.mark.asyncio
async def test_valid_token_returns_registered_user(monkeypatch) -> None:
    async def resolve(_token: str) -> database.UserRecord:
        return database.UserRecord("user-a", "Alice", False)

    monkeypatch.setattr(database, "resolve_login_session", resolve)
    user = await auth_module.authenticate_request(_request("Bearer valid"))

    assert user["identity"] == "user-a"
    assert user["display_name"] == "Alice"
    assert user["is_authenticated"] is True


@pytest.mark.asyncio
async def test_thread_create_stamps_and_claims_owner(monkeypatch) -> None:
    claims: list[tuple[str, str]] = []

    async def claim(thread_id: str, user_id: str) -> None:
        claims.append((thread_id, user_id))

    monkeypatch.setattr(database, "claim_thread", claim)
    ctx = SimpleNamespace(user=SimpleNamespace(identity="user-a"))
    value = {"thread_id": "thread-a", "metadata": {}}

    await auth_module.create_thread(ctx, value)

    assert value["metadata"]["owner"] == "user-a"
    assert claims == [("thread-a", "user-a")]

