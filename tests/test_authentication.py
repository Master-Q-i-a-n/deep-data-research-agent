from types import SimpleNamespace

import pytest
from starlette.requests import Request

from deep_data_research_agent import auth as auth_module
from deep_data_research_agent import database, webapp


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


@pytest.mark.asyncio
async def test_async_task_status_queries_child_run_without_model(monkeypatch) -> None:
    async def get_owner(_thread_id: str) -> str:
        return database.DEFAULT_USER_ID

    class Threads:
        async def get(self, *, thread_id: str) -> dict[str, object]:
            assert thread_id == "parent-thread"
            return {
                "values": {
                    "async_tasks": {
                        "child-thread": {
                            "task_id": "child-thread",
                            "thread_id": "child-thread",
                            "run_id": "run-1",
                            "agent_name": "crawl-worker",
                            "status": "running",
                        }
                    }
                }
            }

    class Runs:
        async def get(self, *, thread_id: str, run_id: str) -> dict[str, str]:
            assert (thread_id, run_id) == ("child-thread", "run-1")
            return {"status": "success"}

    monkeypatch.setattr(database, "get_thread_owner", get_owner)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(agent_client=SimpleNamespace(threads=Threads(), runs=Runs()))
        )
    )

    result = await webapp.async_task_status(
        webapp.AsyncTaskStatusRequest(thread_id="parent-thread"),
        request,
        None,
    )

    assert result["tasks"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_async_task_status_hides_other_users_thread(monkeypatch) -> None:
    async def get_owner(_thread_id: str) -> str:
        return "another-user"

    monkeypatch.setattr(database, "get_thread_owner", get_owner)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(Exception, match="会话不存在"):
        await webapp.async_task_status(
            webapp.AsyncTaskStatusRequest(thread_id="private-thread"),
            request,
            None,
        )
