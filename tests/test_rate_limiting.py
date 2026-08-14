from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from deep_data_research_agent import auth as auth_module
from deep_data_research_agent import database, webapp
from deep_data_research_agent.config import Settings


def _request(
    authorization: str | None = None,
    *,
    path: str = "/",
    client_host: str = "127.0.0.1",
    forwarded_for: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": headers,
            "client": (client_host, 50000),
        }
    )


@pytest_asyncio.fixture
async def isolated_database(monkeypatch):
    """Use SQLite to exercise the same atomic upsert used by PostgreSQL."""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_session_factory", factory)
    monkeypatch.setattr(database, "_initialized", False)
    await database.ensure_schema()
    try:
        yield factory
    finally:
        await database.close_database()


def test_production_requires_stable_rate_limit_secret() -> None:
    with pytest.raises(ValueError, match="至少需要 32 个字符"):
        Settings(app_env="production", rate_limit_key_secret="short")

    settings = Settings(app_env="production", rate_limit_key_secret="x" * 32)
    assert settings.app_env == "production"


@pytest.mark.asyncio
async def test_production_rejects_anonymous_langgraph_request(monkeypatch) -> None:
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )

    with pytest.raises(Exception) as caught:
        await auth_module.authenticate_request(_request())

    assert caught.value.status_code == 401
    assert caught.value.detail == "请先登录"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/auth/register", "/auth/login", "/auth/logout"])
async def test_production_keeps_auth_entry_points_public(monkeypatch, path: str) -> None:
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )

    user = await auth_module.authenticate_request(_request(path=path))

    assert user["identity"] == "public-auth"
    assert user["is_authenticated"] is False


@pytest.mark.asyncio
async def test_production_auth_me_rejects_anonymous_request(monkeypatch) -> None:
    monkeypatch.setattr(
        webapp,
        "get_settings",
        lambda: SimpleNamespace(app_env="production"),
    )

    with pytest.raises(Exception) as caught:
        await webapp.current_user(None)

    assert caught.value.status_code == 401
    assert caught.value.detail == "请先登录"


@pytest.mark.asyncio
async def test_fixed_window_hashes_keys_and_resets_after_expiry(
    isolated_database,
) -> None:
    now = datetime(2026, 8, 14, 12, 0, 10, tzinfo=UTC)
    decisions = [
        await database.consume_rate_limit(
            "auth_login",
            "127.0.0.1\0alice",
            limit=2,
            window_seconds=60,
            now=now,
        )
        for _ in range(3)
    ]

    assert [decision.allowed for decision in decisions] == [True, True, False]
    assert decisions[-1].count == 3
    assert decisions[-1].retry_after_seconds == 50

    async with isolated_database() as session:
        bucket = (await session.execute(select(database.RateLimitBucket))).scalar_one()
        assert bucket.key_hash != "127.0.0.1\0alice"
        assert len(bucket.key_hash) == 64

    next_window = await database.consume_rate_limit(
        "auth_login",
        "127.0.0.1\0alice",
        limit=2,
        window_seconds=60,
        now=now + timedelta(seconds=60),
    )
    assert next_window.allowed is True
    assert next_window.count == 1


@pytest.mark.asyncio
async def test_successful_login_clear_removes_current_bucket(isolated_database) -> None:
    await database.consume_rate_limit(
        "auth_login",
        "127.0.0.1\0alice",
        limit=1,
        window_seconds=900,
    )
    await database.clear_rate_limit("auth_login", "127.0.0.1\0alice")

    decision = await database.consume_rate_limit(
        "auth_login",
        "127.0.0.1\0alice",
        limit=1,
        window_seconds=900,
    )
    assert decision.allowed is True
    assert decision.count == 1


@pytest.mark.asyncio
async def test_failed_login_sixth_attempt_is_limited(isolated_database, monkeypatch) -> None:
    monkeypatch.setattr(
        webapp,
        "get_settings",
        lambda: SimpleNamespace(
            auth_login_failure_limit=5,
            auth_login_window_seconds=900,
        ),
    )
    payload = webapp.LoginRequest(username="Alice", password="wrong-password")
    request = _request(client_host="198.51.100.10")

    for _ in range(5):
        with pytest.raises(Exception) as caught:
            await webapp.login(payload, request)
        assert caught.value.status_code == 401
        assert caught.value.detail == "用户名或密码错误"

    with pytest.raises(Exception) as limited:
        await webapp.login(payload, request)
    assert limited.value.status_code == 429
    assert int(limited.value.headers["Retry-After"]) > 0


@pytest.mark.asyncio
async def test_register_counts_validated_requests_before_business_checks(
    isolated_database,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        webapp,
        "get_settings",
        lambda: SimpleNamespace(
            auth_register_limit=3,
            auth_register_window_seconds=3600,
        ),
    )
    payload = webapp.RegisterRequest(
        username="Alice",
        password="password-a",
        confirm_password="password-b",
    )
    request = _request(client_host="198.51.100.20")

    for _ in range(3):
        with pytest.raises(Exception) as caught:
            await webapp.register(payload, request)
        assert caught.value.status_code == 422
        assert caught.value.detail == "两次输入的密码不一致"

    with pytest.raises(Exception) as limited:
        await webapp.register(payload, request)
    assert limited.value.status_code == 429
    assert int(limited.value.headers["Retry-After"]) > 0


@pytest.mark.asyncio
async def test_create_run_limits_before_thread_claim(monkeypatch) -> None:
    claims: list[tuple[str, str]] = []

    async def deny(*_args, **_kwargs) -> database.RateLimitDecision:
        return database.RateLimitDecision(False, 11, 10, 27)

    async def claim(thread_id: str, user_id: str) -> None:
        claims.append((thread_id, user_id))

    monkeypatch.setattr(database, "consume_rate_limit", deny)
    monkeypatch.setattr(database, "claim_thread", claim)
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: SimpleNamespace(agent_run_limit=10, agent_run_window_seconds=60),
    )
    ctx = SimpleNamespace(user=SimpleNamespace(identity="user-a"))
    value = {"thread_id": "thread-a", "metadata": {}}

    with pytest.raises(Exception) as caught:
        await auth_module.create_run(ctx, value)

    assert caught.value.status_code == 429
    assert caught.value.headers["Retry-After"] == "27"
    assert claims == []
    assert value["metadata"] == {}


@pytest.mark.asyncio
async def test_rate_limit_storage_failure_closes_agent_run(monkeypatch) -> None:
    async def fail(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(database, "consume_rate_limit", fail)
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: SimpleNamespace(agent_run_limit=10, agent_run_window_seconds=60),
    )
    ctx = SimpleNamespace(user=SimpleNamespace(identity="user-a"))

    with pytest.raises(Exception) as caught:
        await auth_module.create_run(ctx, {"thread_id": "thread-a"})

    assert caught.value.status_code == 503


def test_client_ip_uses_asgi_peer_and_ignores_forwarded_header() -> None:
    request = _request(client_host="::ffff:127.0.0.1", forwarded_for="203.0.113.8")
    assert webapp._client_ip(request) == "127.0.0.1"
