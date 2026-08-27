import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from langgraph.runtime import ExecutionInfo, Runtime
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from deep_data_research_agent.admissions import redis_limits, token_usage
from deep_data_research_agent.api import app as webapp
from deep_data_research_agent.api import auth as auth_module
from deep_data_research_agent.core.config import Settings
from deep_data_research_agent.database import repository as database
from deep_data_research_agent.database.models import Base


@pytest.mark.asyncio
async def test_token_usage_middleware_supports_runtime_without_config(monkeypatch) -> None:
    """Modern LangGraph Runtime exposes execution info but no config attribute."""

    captured: dict[str, object] = {}
    reservation = SimpleNamespace(call_id="call-a", reserved_tokens=100)

    async def reserve(**kwargs):
        captured.update(kwargs)
        return reservation

    async def settle(_reservation, **kwargs):
        captured["settled"] = kwargs

    monkeypatch.setattr(token_usage, "_reserve", reserve)
    monkeypatch.setattr(token_usage, "_settle", settle)
    monkeypatch.setattr(
        token_usage,
        "get_config",
        lambda: {
            "configurable": {"thread_id": "thread-a"},
            "metadata": {"token_budget_session_id": "submission-a"},
        },
    )
    runtime = Runtime(
        server_info=SimpleNamespace(user=SimpleNamespace(identity="user-a")),
        execution_info=ExecutionInfo(
            checkpoint_id="checkpoint-a",
            checkpoint_ns="",
            task_id="task-a",
            thread_id="thread-a",
            run_id="run-a",
        ),
    )
    request = token_usage.ModelRequest(
        model=SimpleNamespace(model_name="test-model"),
        messages=[token_usage.AIMessage(content="hello")],
        runtime=runtime,
    )

    async def handler(_request):
        return token_usage.ModelResponse(
            result=[
                token_usage.AIMessage(
                    content="ok",
                    usage_metadata={
                        "input_tokens": 3,
                        "output_tokens": 2,
                        "total_tokens": 5,
                    },
                )
            ]
        )

    response = await token_usage.TokenUsageMiddleware(
        agent_name="supervisor"
    ).awrap_model_call(request, handler)

    assert response.result[0].content == "ok"
    assert captured["user_id"] == "user-a"
    assert captured["root_run_id"] == "submission-a"
    assert captured["thread_id"] == "thread-a"
    assert captured["settled"]["total_tokens"] == 5  # type: ignore[index]


@pytest.mark.asyncio
async def test_token_usage_middleware_settles_cancelled_call(monkeypatch) -> None:
    """Server shutdown must not leave a reserved model call pending forever."""

    captured: dict[str, object] = {}
    reservation = SimpleNamespace(call_id="call-cancelled", reserved_tokens=321)

    async def reserve(**_kwargs):
        return reservation

    async def settle(_reservation, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(token_usage, "_reserve", reserve)
    monkeypatch.setattr(token_usage, "_settle", settle)
    monkeypatch.setattr(token_usage, "get_config", dict)
    request = token_usage.ModelRequest(
        model=SimpleNamespace(model_name="test-model"),
        messages=[token_usage.AIMessage(content="hello")],
        runtime=Runtime(
            server_info=SimpleNamespace(
                user=SimpleNamespace(identity=database.DEFAULT_USER_ID)
            )
        ),
    )

    async def handler(_request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await token_usage.TokenUsageMiddleware(
            agent_name="supervisor"
        ).awrap_model_call(request, handler)

    assert captured["total_tokens"] == 321
    assert captured["usage_source"] == "reserved"
    assert captured["status"] == "failed"


async def _noop_schema_check(**_kwargs) -> None:
    return None


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
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_session_factory", factory)
    monkeypatch.setattr(database, "_initialized", False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(database, "_validate_deployed_schema", _noop_schema_check)
    await database.ensure_schema()
    try:
        yield factory
    finally:
        await database.close_database()


@pytest_asyncio.fixture
async def redis_service(monkeypatch):
    """Use real Redis/Lua with an isolated prefix and never call FLUSHDB."""

    await redis_limits.close_redis()
    monkeypatch.setattr(redis_limits, "_KEY_PREFIX", f"ddra:test:{uuid4()}")
    try:
        client = await redis_limits.initialize_redis()
    except (OSError, RedisError, RuntimeError) as exc:
        pytest.skip(f"测试 Redis 不可用：{exc}")
    try:
        yield client
    finally:
        await redis_limits.close_redis()


async def _delete_rate_key(client, scope: str, raw_key: str) -> None:
    key = f"{redis_limits._KEY_PREFIX}:rl:{scope}:{redis_limits._digest(scope, raw_key)}"
    await client.delete(key)


async def _delete_user_keys(client, user_id: str, submission_ids: list[str]) -> None:
    base = f"{redis_limits._KEY_PREFIX}:{{{redis_limits._user_tag(user_id)}}}"
    keys = [
        f"{base}:questions",
        f"{base}:reservations",
        f"{base}:admission-lock",
        f"{base}:token-bucket",
    ]
    for submission_id in submission_ids:
        keys.extend([f"{base}:permit:{submission_id}", f"{base}:used:{submission_id}"])
    await client.delete(*keys)


async def _seed_token_bucket(user_id: str, balance: int = 100_000_000) -> None:
    await redis_limits.sync_token_bucket(
        user_id,
        balance_tokens=balance,
        last_refill_hour=int(time.time() // 3600),
        version=1,
    )


def test_production_requires_stable_rate_limit_secret() -> None:
    with pytest.raises(ValueError, match="至少需要 32 个字符"):
        Settings(app_env="production", rate_limit_key_secret="short")

    settings = Settings(
        app_env="production",
        rate_limit_key_secret="x" * 32,
        workspace_storage_backend="oss",
        oss_bucket_name="test-bucket",
    )
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
async def test_sliding_window_boundary_retry_ttl_and_idempotency(redis_service) -> None:
    raw_key = f"ip:{uuid4()}"
    try:
        first = await redis_limits.consume_sliding_window(
            "login_ip", raw_key, limit=2, window_seconds=60, request_id="request-1"
        )
        duplicate = await redis_limits.consume_sliding_window(
            "login_ip", raw_key, limit=2, window_seconds=60, request_id="request-1"
        )
        second = await redis_limits.consume_sliding_window(
            "login_ip", raw_key, limit=2, window_seconds=60, request_id="request-2"
        )
        denied = await redis_limits.consume_sliding_window(
            "login_ip", raw_key, limit=2, window_seconds=60, request_id="request-3"
        )

        assert (first.allowed, first.count) == (True, 1)
        assert duplicate.allowed is True and duplicate.duplicate is True
        assert (second.allowed, second.count) == (True, 2)
        assert denied.allowed is False
        assert 1 <= denied.retry_after_seconds <= 60
        key = (
            f"{redis_limits._KEY_PREFIX}:rl:login_ip:"
            f"{redis_limits._digest('login_ip', raw_key)}"
        )
        assert 0 < await redis_service.pttl(key) <= 61_000
    finally:
        await _delete_rate_key(redis_service, "login_ip", raw_key)


@pytest.mark.asyncio
async def test_concurrent_sliding_window_allows_exactly_twenty(redis_service) -> None:
    raw_key = f"user:{uuid4()}"
    try:
        decisions = await asyncio.gather(*[
            redis_limits.consume_sliding_window(
                "questions",
                raw_key,
                limit=20,
                window_seconds=60,
                request_id=f"request-{index}",
            )
            for index in range(21)
        ])
        assert sum(decision.allowed for decision in decisions) == 20
        assert sum(not decision.allowed for decision in decisions) == 1
    finally:
        await _delete_rate_key(redis_service, "questions", raw_key)


@pytest.mark.asyncio
async def test_token_bucket_database_refill_reserve_and_idempotent_settlement(
    isolated_database,
) -> None:
    bucket = await database.get_token_bucket(database.DEFAULT_USER_ID)
    assert bucket.balance_tokens == 100_000_000

    call_id = str(uuid4())
    reservation = await database.reserve_model_tokens(
        call_id=call_id,
        user_id=database.DEFAULT_USER_ID,
        root_run_id="run-a",
        thread_id="thread-a",
        agent_name="supervisor",
        model_name="test-model",
        reserved_tokens=10_000,
    )
    assert reservation.bucket.balance_tokens == 99_990_000
    settled = await database.settle_model_tokens(
        call_id=call_id,
        input_tokens=3_000,
        output_tokens=1_000,
        total_tokens=4_000,
        usage_source="provider",
    )
    assert settled.balance_tokens == 99_996_000
    duplicate = await database.settle_model_tokens(
        call_id=call_id,
        input_tokens=9_000,
        output_tokens=9_000,
        total_tokens=18_000,
        usage_source="provider",
    )
    assert duplicate.balance_tokens == 99_996_000

    async with isolated_database() as session:
        state = await session.get(database.UserTokenBucket, database.DEFAULT_USER_ID)
        assert state is not None
        state.balance_tokens = -15_000_000
        state.last_refill_hour = int(time.time() // 3600) - 2
        await session.commit()
    refilled = await database.get_token_bucket(database.DEFAULT_USER_ID)
    assert refilled.balance_tokens == 5_000_000


@pytest.mark.asyncio
async def test_metered_direct_model_call_uses_provider_usage(
    isolated_database,
    monkeypatch,
) -> None:
    class Model:
        model_name = "test-model"

        def get_num_tokens_from_messages(self, _messages):
            return 100

        async def ainvoke(self, _input, config=None, **_kwargs):
            assert config == {"tags": ["test"]}
            return token_usage.AIMessage(
                content="ok",
                usage_metadata={
                    "input_tokens": 70,
                    "output_tokens": 30,
                    "total_tokens": 100,
                },
            )

    async def ignore_sync(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(token_usage, "_sync_bucket", ignore_sync)
    result = await token_usage.metered_model_ainvoke(
        Model(),
        "hello",
        user_id=database.DEFAULT_USER_ID,
        agent_name="memory-user",
        config={"tags": ["test"]},
    )
    assert result.content == "ok"
    bucket = await database.get_token_bucket(database.DEFAULT_USER_ID)
    assert bucket.balance_tokens == 99_999_900

    async with isolated_database() as session:
        rows = (await session.execute(database.select(database.ModelTokenUsage))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "settled"
    assert rows[0].usage_source == "provider"
    assert rows[0].total_tokens == 100


@pytest.mark.asyncio
async def test_token_bucket_blocks_zero_and_calculates_debt_retry(redis_service) -> None:
    user_id = f"token-user-{uuid4()}"
    submissions = [str(uuid4()), str(uuid4())]
    try:
        await _seed_token_bucket(user_id, 0)
        zero = await redis_limits.admit_run(user_id, submissions[0], None, [])
        assert zero.code == "TOKEN_BUDGET_EXHAUSTED"
        assert zero.token_balance == 0
        assert 1 <= zero.retry_after_seconds <= 3600

        await redis_limits.sync_token_bucket(
            user_id,
            balance_tokens=-10_000_000,
            last_refill_hour=int(time.time() // 3600),
            version=2,
        )
        debt = await redis_limits.admit_run(user_id, submissions[1], None, [])
        assert debt.code == "TOKEN_BUDGET_EXHAUSTED"
        assert 3600 < debt.retry_after_seconds <= 7200
    finally:
        await _delete_user_keys(redis_service, user_id, submissions)


@pytest.mark.asyncio
async def test_run_admission_thread_limit_same_thread_and_permit_guards(redis_service) -> None:
    user_id = f"user-{uuid4()}"
    submissions = [f"submission-{index}-{uuid4()}" for index in range(6)]
    try:
        await _seed_token_bucket(user_id)
        for index in range(3):
            decision = await redis_limits.admit_run(
                user_id, submissions[index], f"thread-{index}", []
            )
            assert decision.allowed is True

        duplicate = await redis_limits.admit_run(
            user_id, submissions[0], "thread-0", []
        )
        assert duplicate.allowed is True
        assert await redis_limits.consume_run_permit(
            f"other-{user_id}", submissions[0], "thread-0"
        ) == "MISSING_OR_EXPIRED"

        denied = await redis_limits.admit_run(user_id, submissions[3], "thread-3", [])
        assert denied.code == "THREAD_CONCURRENCY_LIMIT"
        assert set(denied.active_thread_ids) == {"thread-0", "thread-1", "thread-2"}

        same_thread = await redis_limits.admit_run(
            user_id, submissions[4], "thread-0", []
        )
        assert same_thread.allowed is True
        assert await redis_limits.consume_run_permit(
            user_id, submissions[4], "thread-other"
        ) == "THREAD_MISMATCH"
        assert await redis_limits.consume_run_permit(
            user_id, submissions[4], "thread-0"
        ) == "CONSUMED"
        assert await redis_limits.consume_run_permit(
            user_id, submissions[4], "thread-0"
        ) == "ALREADY_USED"
        assert await redis_limits.consume_run_permit(
            user_id, submissions[0], "thread-0"
        ) == "CONSUMED"
        used = await redis_limits.admit_run(user_id, submissions[0], "thread-0", [])
        assert used.code == "RUN_ADMISSION_ALREADY_USED"

        new_thread = await redis_limits.admit_run(user_id, submissions[5], None, [])
        assert new_thread.allowed is False
        assert new_thread.code == "THREAD_CONCURRENCY_LIMIT"
    finally:
        await _delete_user_keys(redis_service, user_id, submissions)


@pytest.mark.asyncio
async def test_expired_reservations_release_thread_slot_and_permit(redis_service, monkeypatch) -> None:
    settings = redis_limits.get_settings().model_copy(
        update={"run_permit_ttl_seconds": 1, "run_reservation_ttl_seconds": 1}
    )
    monkeypatch.setattr(redis_limits, "get_settings", lambda: settings)
    user_id = f"expiring-user-{uuid4()}"
    submissions = [f"expiring-{index}-{uuid4()}" for index in range(4)]
    try:
        await _seed_token_bucket(user_id)
        for index in range(3):
            assert (
                await redis_limits.admit_run(
                    user_id, submissions[index], f"thread-{index}", []
                )
            ).allowed is True
        await asyncio.sleep(1.05)
        assert await redis_limits.consume_run_permit(
            user_id, submissions[0], "thread-0"
        ) == "MISSING_OR_EXPIRED"
        assert (
            await redis_limits.admit_run(user_id, submissions[3], "thread-3", [])
        ).allowed is True
    finally:
        await _delete_user_keys(redis_service, user_id, submissions)


@pytest.mark.asyncio
async def test_run_admission_endpoint_returns_structured_active_threads(
    redis_service,
    monkeypatch,
) -> None:
    user_id = f"endpoint-user-{uuid4()}"
    submission_id = str(uuid4())
    active_ids = [str(uuid4()) for _ in range(3)]

    class Threads:
        async def search(self, **_kwargs):
            return [{"thread_id": thread_id, "status": "busy"} for thread_id in active_ids]

    async def authenticated(_authorization: str | None) -> str:
        return user_id

    request = _request(path="/run-admissions")
    request.scope["app"] = SimpleNamespace(
        state=SimpleNamespace(agent_client=SimpleNamespace(threads=Threads()))
    )
    monkeypatch.setattr(webapp, "_authenticated_user_id", authenticated)
    async def token_bucket(_user_id: str) -> database.TokenBucketRecord:
        return database.TokenBucketRecord(
            _user_id, 100_000_000, int(time.time() // 3600), 1
        )

    monkeypatch.setattr(database, "get_token_bucket", token_bucket)
    try:
        with pytest.raises(Exception) as caught:
            await webapp.create_run_admission(
                webapp.RunAdmissionRequest(submission_id=submission_id),
                request,
                None,
            )
        assert caught.value.status_code == 409
        assert caught.value.detail == {
            "code": "THREAD_CONCURRENCY_LIMIT",
            "message": "最多同时运行 3 个会话，请等待或停止其中一个",
            "limit": 3,
            "retry_after_seconds": 0,
            "active_thread_ids": active_ids,
        }
    finally:
        await _delete_user_keys(redis_service, user_id, [submission_id])


@pytest.mark.asyncio
async def test_run_admission_endpoint_returns_token_budget_details(
    redis_service,
    monkeypatch,
) -> None:
    user_id = f"token-endpoint-user-{uuid4()}"
    submission_id = str(uuid4())
    current_hour = int(time.time() // 3600)

    class Threads:
        async def search(self, **_kwargs):
            return []

    async def authenticated(_authorization: str | None) -> str:
        return user_id

    async def token_bucket(_user_id: str) -> database.TokenBucketRecord:
        return database.TokenBucketRecord(_user_id, -10_000_000, current_hour, 1)

    request = _request(path="/run-admissions")
    request.scope["app"] = SimpleNamespace(
        state=SimpleNamespace(agent_client=SimpleNamespace(threads=Threads()))
    )
    monkeypatch.setattr(webapp, "_authenticated_user_id", authenticated)
    monkeypatch.setattr(database, "get_token_bucket", token_bucket)
    try:
        with pytest.raises(Exception) as caught:
            await webapp.create_run_admission(
                webapp.RunAdmissionRequest(submission_id=submission_id),
                request,
                None,
            )
        assert caught.value.status_code == 429
        assert caught.value.detail["code"] == "TOKEN_BUDGET_EXHAUSTED"
        assert caught.value.detail["balance_tokens"] == -10_000_000
        assert caught.value.detail["capacity_tokens"] == 100_000_000
        assert 3600 < caught.value.detail["retry_after_seconds"] <= 7200
        assert caught.value.headers["Retry-After"] == str(
            caught.value.detail["retry_after_seconds"]
        )
    finally:
        await _delete_user_keys(redis_service, user_id, [submission_id])


@pytest.mark.asyncio
async def test_internal_child_marker_is_signed_and_single_use(redis_service) -> None:
    user_id = f"user-{uuid4()}"
    marker = redis_limits.issue_internal_run_marker(
        user_id=user_id,
        graph_id="crawl-worker",
        parent_thread_id="parent-thread",
        token_budget_session_id="root-run-a",
    )
    key = (
        f"{redis_limits._KEY_PREFIX}:internal-used:"
        f"{redis_limits._user_tag(user_id)}:{marker['nonce']}"
    )
    try:
        assert await redis_limits.consume_internal_run_marker(
            marker, user_id=user_id, graph_id="crawl-worker"
        ) is True
        assert await redis_limits.consume_internal_run_marker(
            marker, user_id=user_id, graph_id="crawl-worker"
        ) is False
        forged = {**marker, "nonce": "forged"}
        assert await redis_limits.consume_internal_run_marker(
            forged, user_id=user_id, graph_id="crawl-worker"
        ) is False
        forged_session = {**marker, "token_budget_session_id": "other-run"}
        assert await redis_limits.consume_internal_run_marker(
            forged_session, user_id=user_id, graph_id="crawl-worker"
        ) is False
    finally:
        await redis_service.delete(key)


@pytest.mark.asyncio
async def test_login_checks_ip_then_fallback_user_bucket(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def allow(scope: str, raw_key: str, **_kwargs) -> redis_limits.RateLimitDecision:
        calls.append((scope, raw_key))
        return redis_limits.RateLimitDecision(True, 1, 10, 0)

    async def missing_user(_username: str):
        return None

    monkeypatch.setattr(redis_limits, "consume_sliding_window", allow)
    monkeypatch.setattr(database, "get_user_by_username", missing_user)
    payload = webapp.LoginRequest(username="MissingUser", password="wrong-password")
    with pytest.raises(Exception) as caught:
        await webapp.login(payload, _request(client_host="198.51.100.10"))
    assert caught.value.status_code == 401
    assert calls == [
        ("login_ip", "198.51.100.10"),
        ("login_user", "username:missinguser"),
    ]


@pytest.mark.asyncio
async def test_successful_login_also_keeps_both_rate_limit_counts(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    user = database.UserRecord(
        id="user-a",
        username="Alice",
        is_system=False,
        password_hash="test-password-hash",
    )

    async def allow(scope: str, raw_key: str, **_kwargs) -> redis_limits.RateLimitDecision:
        calls.append((scope, raw_key))
        return redis_limits.RateLimitDecision(True, len(calls), 10, 0)

    async def find_user(_username: str) -> database.UserRecord:
        return user

    async def issue_token(_user: database.UserRecord) -> dict[str, object]:
        return {"token": "token-a", "user": {"id": "user-a"}}

    monkeypatch.setattr(redis_limits, "consume_sliding_window", allow)
    monkeypatch.setattr(database, "get_user_by_username", find_user)
    monkeypatch.setattr(webapp, "_issue_token", issue_token)
    monkeypatch.setattr(
        webapp,
        "_PASSWORD_HASHER",
        SimpleNamespace(verify=lambda *_args: True),
    )
    payload = webapp.LoginRequest(username="Alice", password="correct-password")
    await webapp.login(payload, _request(client_host="198.51.100.11"))
    await webapp.login(payload, _request(client_host="198.51.100.11"))
    assert calls == [
        ("login_ip", "198.51.100.11"),
        ("login_user", "user-a"),
        ("login_ip", "198.51.100.11"),
        ("login_user", "user-a"),
    ]


@pytest.mark.asyncio
async def test_register_uses_redis_before_business_validation(monkeypatch) -> None:
    calls: list[str] = []

    async def allow(scope: str, *_args, **_kwargs) -> redis_limits.RateLimitDecision:
        calls.append(scope)
        return redis_limits.RateLimitDecision(True, 1, 3, 0)

    monkeypatch.setattr(redis_limits, "consume_sliding_window", allow)
    payload = webapp.RegisterRequest(
        username="Alice",
        password="password-a",
        confirm_password="password-b",
    )
    with pytest.raises(Exception) as caught:
        await webapp.register(payload, _request(client_host="198.51.100.20"))
    assert caught.value.status_code == 422
    assert calls == ["auth_register"]


@pytest.mark.asyncio
async def test_create_run_requires_and_consumes_permit_before_claim(monkeypatch) -> None:
    claims: list[tuple[str, str]] = []

    async def consume(*_args, **_kwargs) -> str:
        return "CONSUMED"

    async def claim(thread_id: str, user_id: str) -> None:
        claims.append((thread_id, user_id))

    monkeypatch.setattr(redis_limits, "consume_run_permit", consume)
    monkeypatch.setattr(database, "claim_thread", claim)
    ctx = SimpleNamespace(user=SimpleNamespace(identity="user-a"))
    value = {
        "thread_id": "thread-a",
        "metadata": {"deep_data_ui": {"submission_id": "submission-a"}},
    }
    await auth_module.create_run(ctx, value)
    assert claims == [("thread-a", "user-a")]
    assert value["metadata"]["owner"] == "user-a"
    assert value["metadata"]["token_budget_session_id"] == "submission-a"

    with pytest.raises(Exception) as missing:
        await auth_module.create_run(ctx, {"thread_id": "thread-b", "metadata": {}})
    assert missing.value.status_code == 403
    assert missing.value.headers["X-Error-Code"] == "RUN_ADMISSION_REQUIRED"


@pytest.mark.asyncio
async def test_redis_failure_is_fail_closed_without_postgres_fallback(monkeypatch) -> None:
    async def fail(*_args, **_kwargs):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(redis_limits, "consume_sliding_window", fail)
    with pytest.raises(Exception) as caught:
        await webapp._enforce_rate_limit(
            scope="login_ip",
            raw_key="127.0.0.1",
            limit=10,
            window_seconds=60,
            detail="登录尝试过于频繁",
            error_code="LOGIN_RATE_LIMITED",
            request_id="request-a",
        )
    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "RATE_LIMIT_SERVICE_UNAVAILABLE"
    assert not hasattr(database, "consume_rate_limit")


def test_client_ip_uses_asgi_peer_and_ignores_forwarded_header() -> None:
    request = _request(client_host="::ffff:127.0.0.1", forwarded_for="203.0.113.8")
    assert webapp._client_ip(request) == "127.0.0.1"
