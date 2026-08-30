from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from deep_data_research_agent.admissions import token_usage
from deep_data_research_agent.api import app as webapp
from deep_data_research_agent.database import repository as database
from deep_data_research_agent.database.models import Base, User, UserModelProvider
from deep_data_research_agent.providers import model_profiles, service
from deep_data_research_agent.providers import models as provider_models
from deep_data_research_agent.providers import responses as provider_responses


@pytest_asyncio.fixture
async def provider_database(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def schema_ready() -> None:
        return None

    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_session_factory", factory)
    monkeypatch.setattr(database, "ensure_schema", schema_ready)
    async with factory() as session, session.begin():
        session.add_all(
            [
                User(
                    id="user-a",
                    username="user-a",
                    username_normalized="user-a",
                    is_system=False,
                ),
                User(
                    id="user-b",
                    username="user-b",
                    username_normalized="user-b",
                    is_system=False,
                ),
            ]
        )
    try:
        yield factory
    finally:
        await engine.dispose()


def _provider_settings(key_file: Path, allowlist: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        app_env="development",
        model_provider_encryption_key_file=key_file,
        model_provider_host_allowlist=allowlist,
    )


def test_production_readiness_requires_valid_encryption_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.key"
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(
            app_env="production",
            model_provider_encryption_key_file=missing,
        ),
    )
    with pytest.raises(service.ProviderConfigurationError, match="密钥不可用"):
        service.check_encryption_ready()

    missing.write_text("not-a-fernet-key", encoding="ascii")
    with pytest.raises(service.ProviderConfigurationError, match="格式无效"):
        service.check_encryption_ready()


@pytest.mark.asyncio
async def test_provider_is_encrypted_scoped_and_can_retain_key(
    provider_database,
    monkeypatch,
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "provider.key"
    key_file.write_bytes(Fernet.generate_key())
    monkeypatch.setattr(service, "get_settings", lambda: _provider_settings(key_file))
    monkeypatch.setattr(
        service,
        "_resolve_addresses",
        lambda _host, _port: {ipaddress.ip_address("8.8.8.8")},
    )

    secret = "sk-this-must-never-be-stored-as-plaintext"
    first = await service.save_provider(
        user_id="user-a",
        base_url="https://models.example.com/v1/",
        model_name="model-one",
        api_key=secret,
    )
    async with provider_database() as session:
        stored = (
            await session.execute(
                select(UserModelProvider).where(UserModelProvider.user_id == "user-a")
            )
        ).scalar_one()
        first_ciphertext = stored.api_key_ciphertext
        assert first_ciphertext != secret
        assert secret not in first_ciphertext

    assert await database.get_model_provider("user-b") is None
    public = await service.get_public_provider("user-a")
    assert public == {
        "base_url": "https://models.example.com/v1",
        "model_name": "model-one",
        "has_api_key": True,
        "api_key_hint": "text",
        "version": 1,
        "updated_at": first.updated_at.isoformat(),
    }
    assert "api_key" not in public

    updated = await service.save_provider(
        user_id="user-a",
        base_url="https://models.example.com/v1",
        model_name="deepseek-v4-flash",
        api_key=None,
    )
    async with provider_database() as session:
        stored = await session.get(UserModelProvider, "user-a")
        assert stored is not None
        assert stored.api_key_ciphertext == first_ciphertext
        assert stored.provider_type == "openai_compatible"
        assert stored.version == 2
    assert updated.version == 2
    assert (await service.resolve_provider("user-a")).api_key == secret

    assert await service.delete_provider("user-a") is True
    assert await database.get_model_provider("user-a") is None


@pytest.mark.parametrize(
    "url",
    [
        "ftp://models.example.com/v1",
        "https://user:password@models.example.com/v1",
        "https://models.example.com/v1?token=secret",
        "https://models.example.com/v1#fragment",
    ],
)
def test_provider_url_rejects_unsafe_components(url: str) -> None:
    with pytest.raises(service.ProviderConfigurationError):
        service.normalize_provider_url(url)


@pytest.mark.asyncio
async def test_provider_url_requires_public_https_or_allowlist(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _provider_settings(tmp_path / "unused")
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        service,
        "_resolve_addresses",
        lambda _host, _port: {ipaddress.ip_address("127.0.0.1")},
    )

    with pytest.raises(service.ProviderConfigurationError, match="白名单"):
        await service.validate_provider_url("https://localhost:9000/v1")
    with pytest.raises(service.ProviderConfigurationError, match="HTTP"):
        await service.validate_provider_url("http://localhost:9000/v1")

    settings.model_provider_host_allowlist = "localhost"
    assert (
        await service.validate_provider_url("http://LOCALHOST:9000/v1/")
        == "http://localhost:9000/v1"
    )

    settings.model_provider_host_allowlist = "127.0.0.0/8"
    assert await service.validate_provider_url("http://localhost:9000/v1")


@pytest.mark.parametrize(
    "model_name",
    ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"],
)
def test_deepseek_v4_profiles_use_responses_without_openai_include(
    model_name: str,
) -> None:
    profile = model_profiles.model_profile(model_name)

    assert profile == {"max_input_tokens": 1_000_000}
    capabilities = model_profiles.model_capabilities(model_name)
    assert capabilities.supports_responses_api is True
    assert capabilities.supports_web_search is True
    assert capabilities.responses_include == ()
    assert model_profiles.model_profile("unknown-model") is None


@pytest.mark.parametrize(
    ("model_name", "max_input_tokens"),
    [("gpt-5.6", 1_050_000), ("GPT-5.4-mini", 400_000)],
)
def test_official_gpt_profiles_enable_responses_and_web_search(
    model_name: str,
    max_input_tokens: int,
) -> None:
    assert model_profiles.model_profile(model_name) == {
        "max_input_tokens": max_input_tokens
    }
    capabilities = model_profiles.model_capabilities(model_name)
    assert capabilities.supports_responses_api is True
    assert capabilities.supports_web_search is True
    assert capabilities.responses_include == (
        "reasoning.encrypted_content",
        "web_search_call.action.sources",
    )

    unknown = model_profiles.model_capabilities("gpt-unknown-compatible")
    assert unknown.supports_responses_api is False
    assert unknown.supports_web_search is False
    assert unknown.responses_include == ()


def test_web_search_profile_requires_responses_api(tmp_path: Path) -> None:
    profile_path = tmp_path / "profiles.yaml"
    profile_path.write_text(
        "custom-model:\n"
        "  max_input_tokens: 1000\n"
        "  supports_web_search: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="必须同时启用 Responses API"):
        model_profiles._load_model_profiles(profile_path)

    profile_path.write_text(
        "custom-model:\n"
        "  max_input_tokens: 1000\n"
        "  responses_include: [reasoning.encrypted_content]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="responses_include"):
        model_profiles._load_model_profiles(profile_path)


@pytest.mark.parametrize(
    "role",
    ["supervisor", "data-analyst", "analysis-reviewer", "crawl-worker", "memory", "test"],
)
def test_responses_capability_configures_every_online_role(
    role: provider_models.ModelRole,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        provider_models,
        "get_settings",
        lambda: SimpleNamespace(
            model_provider_test_timeout_seconds=5,
            model_provider_timeout_seconds=20,
            model_provider_streaming=True,
        ),
    )
    provider = service.ResolvedProvider(
        user_id="user-a",
        base_url="https://api.openai.com/v1",
        model_name="gpt-5.4",
        api_key="test-key",
        api_key_hint="-key",
        version=1,
    )

    model, sync_client, async_client = provider_models._build_model(provider, role)
    try:
        assert model.use_responses_api is True
        assert model.output_version == "responses/v1"
        assert model.store is False
        assert model.streaming is (role == "supervisor")
        assert "reasoning.encrypted_content" in (model.include or [])
        if role == "supervisor":
            assert "web_search_call.action.sources" in (model.include or [])
        else:
            assert "web_search_call.action.sources" not in (model.include or [])
    finally:
        sync_client.close()
        # Construction is synchronous; close the async client in the test loop below.
        import asyncio

        asyncio.run(async_client.aclose())


def test_deepseek_responses_does_not_send_unsupported_include(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_models,
        "get_settings",
        lambda: SimpleNamespace(
            model_provider_test_timeout_seconds=5,
            model_provider_timeout_seconds=20,
            model_provider_streaming=False,
        ),
    )
    provider = service.ResolvedProvider(
        user_id="user-a",
        base_url="https://api.deepseek.com",
        model_name="deepseek-v4-flash",
        api_key="test-key",
        api_key_hint="-key",
        version=1,
    )

    model, sync_client, async_client = provider_models._build_model(
        provider,
        "supervisor",
    )
    try:
        assert model.use_responses_api is True
        assert model.include is None
        assert model.extra_body is None
    finally:
        sync_client.close()
        import asyncio

        asyncio.run(async_client.aclose())


@pytest.mark.asyncio
async def test_deepseek_provider_test_calls_responses_without_include(
    monkeypatch,
    respx_mock,
) -> None:
    route = respx_mock.post("https://api.deepseek.com/responses").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp-deepseek-test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "deepseek-v4-flash",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "rs-1",
                        "status": "completed",
                        "content": [
                            {"type": "reasoning_text", "text": "Check request"}
                        ],
                    },
                    {
                        "type": "message",
                        "id": "msg-1",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "OK",
                                "annotations": [],
                            }
                        ],
                    },
                ],
                "usage": {
                    "input_tokens": 2,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 2,
                    "output_tokens_details": {"reasoning_tokens": 1},
                    "total_tokens": 4,
                },
            },
        )
    )
    monkeypatch.setattr(
        provider_models,
        "get_settings",
        lambda: SimpleNamespace(
            model_provider_test_timeout_seconds=5,
            model_provider_timeout_seconds=20,
            model_provider_streaming=False,
        ),
    )
    provider = service.ResolvedProvider(
        user_id="user-a",
        base_url="https://api.deepseek.com",
        model_name="deepseek-v4-flash",
        api_key="test-key",
        api_key_hint="-key",
        version=1,
    )

    message = await provider_models.test_provider_model(provider)

    assert message.text == "OK"
    assert route.called
    payload = json.loads(route.calls[0].request.content)
    assert payload["store"] is False
    assert "include" not in payload
    assert "tools" not in payload


@pytest.mark.asyncio
async def test_only_supervisor_receives_builtin_web_search(monkeypatch) -> None:
    async def runtime_model(_user_id: str, _role: str):
        return SimpleNamespace(model_name="gpt-5.4")

    monkeypatch.setattr(provider_models, "get_runtime_model", runtime_model)
    runtime = Runtime(
        server_info=SimpleNamespace(user=SimpleNamespace(identity="user-a"))
    )

    async def captured_tools(role: provider_models.ModelRole):
        request = provider_models.ModelRequest(
            model=SimpleNamespace(model_name="placeholder"),
            messages=[HumanMessage(content="hello")],
            runtime=runtime,
            tools=[],
        )

        async def handler(resolved_request):
            return resolved_request.tools

        return await provider_models.ProviderModelMiddleware(role).awrap_model_call(
            request,
            handler,
        )

    assert await captured_tools("supervisor") == [{"type": "web_search"}]
    assert await captured_tools("data-analyst") == []
    assert await captured_tools("crawl-worker") == []


def test_web_search_events_are_normalized_and_sources_are_safe() -> None:
    lifecycle = provider_responses.normalize_web_search_event(
        SimpleNamespace(
            type="response.web_search_call.searching",
            item_id="ws-1",
            output_index=2,
            sequence_number=4,
        )
    )
    assert lifecycle == {
        "type": "web_search_progress",
        "phase": "searching",
        "item_id": "ws-1",
        "output_index": 2,
        "sequence_number": 4,
    }

    item = {
        "type": "web_search_call",
        "id": "ws-1",
        "status": "completed",
        "action": {
            "type": "search",
            "query": "Responses API",
            "sources": [
                {"type": "url", "url": "https://example.com/a"},
                {"type": "url", "url": "javascript:alert(1)"},
                {"type": "url", "url": "https://example.com/a"},
            ],
        },
    }
    completed = provider_responses.normalize_web_search_event(
        SimpleNamespace(
            type="response.output_item.done",
            item=item,
            output_index=2,
            sequence_number=5,
        )
    )
    assert completed is not None
    assert completed["action"] == {"type": "search", "query": "Responses API"}
    assert completed["sources"] == [
        {"title": "example.com", "url": "https://example.com/a"}
    ]


@pytest.mark.asyncio
async def test_supervisor_responses_stream_preserves_langchain_chunks(
    monkeypatch,
) -> None:
    raw_events = [
        SimpleNamespace(
            type="response.web_search_call.searching",
            item_id="ws-stream",
            output_index=0,
            sequence_number=2,
        ),
        SimpleNamespace(
            type="response.output_text.delta",
            output_index=1,
            content_index=0,
            delta="streamed answer",
        ),
    ]
    custom_events: list[dict[str, object]] = []
    captured_payload: dict[str, object] = {}

    class FakeResponseStream:
        def __init__(self) -> None:
            self._events = iter(raw_events)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    class FakeContextManager:
        async def __aenter__(self):
            return FakeResponseStream()

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    class FakeResponses:
        async def create(self, **payload):
            captured_payload.update(payload)
            return FakeContextManager()

    model = provider_responses.SupervisorResponsesChatOpenAI(
        model="gpt-5.4",
        api_key="test-key",
        use_responses_api=True,
        output_version="responses/v1",
        store=False,
        include=[
            "reasoning.encrypted_content",
            "web_search_call.action.sources",
        ],
        stream_chunk_timeout=None,
    )
    object.__setattr__(
        model,
        "root_async_client",
        SimpleNamespace(responses=FakeResponses()),
    )
    monkeypatch.setattr(
        provider_responses,
        "_stream_writer",
        lambda: custom_events.append,
    )

    chunks = [
        chunk
        async for chunk in model._astream_responses(
            [HumanMessage(content="search")]
        )
    ]

    assert chunks[0].text == "streamed answer"
    assert custom_events == [
        {
            "type": "web_search_progress",
            "phase": "searching",
            "item_id": "ws-stream",
            "output_index": 0,
            "sequence_number": 2,
        }
    ]
    assert captured_payload["store"] is False
    assert "web_search_call.action.sources" in captured_payload["include"]


def test_provider_summarizer_uses_runtime_model_profile() -> None:
    runtime_model = ChatOpenAI(
        model="deepseek-v4-flash",
        api_key="test-key",
        base_url="https://models.example.com/v1",
        profile={"max_input_tokens": 1_000_000},
    )
    request = provider_models.ModelRequest(
        model=runtime_model,
        messages=[HumanMessage(content="hello")],
        runtime=Runtime(
            server_info=SimpleNamespace(user=SimpleNamespace(identity="user-a"))
        ),
    )
    middleware = provider_models.ProviderSummarizationMiddleware(
        "supervisor",
        StateBackend(),
    )

    delegate = middleware._delegate(request)

    assert delegate._lc_helper.trigger == ("fraction", 0.85)
    assert delegate._lc_helper.keep == ("fraction", 0.10)


@pytest.mark.asyncio
async def test_provider_get_is_no_store_and_never_returns_ciphertext(monkeypatch) -> None:
    async def authenticated(_authorization: str | None) -> str:
        return "user-a"

    async def public(_user_id: str) -> dict[str, object]:
        return {
            "base_url": "https://models.example.com/v1",
            "model_name": "model-one",
            "has_api_key": True,
            "api_key_hint": "1234",
            "version": 3,
            "updated_at": "2026-08-28T10:00:00",
        }

    monkeypatch.setattr(webapp, "_authenticated_user_id", authenticated)
    monkeypatch.setattr(webapp, "get_public_provider", public)
    response = await webapp.read_model_provider(None)
    body = json.loads(response.body)

    assert response.headers["cache-control"] == "no-store"
    assert body["provider"]["has_api_key"] is True
    assert "api_key" not in body["provider"]
    assert "ciphertext" not in response.body.decode("utf-8")


@pytest.mark.asyncio
async def test_run_admission_rejects_missing_provider_before_redis(monkeypatch) -> None:
    async def authenticated(_authorization: str | None) -> str:
        return "user-a"

    async def missing(_user_id: str):
        raise service.ProviderNotConfiguredError("missing")

    monkeypatch.setattr(webapp, "_authenticated_user_id", authenticated)
    monkeypatch.setattr(webapp, "resolve_provider", missing)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/run-admissions",
            "headers": [],
            "client": ("127.0.0.1", 50000),
        }
    )

    with pytest.raises(Exception) as caught:
        await webapp.create_run_admission(
            webapp.RunAdmissionRequest(submission_id=uuid4()),
            request,
            None,
        )
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "MODEL_PROVIDER_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_provider_middleware_precedes_token_metering(monkeypatch) -> None:
    """The metering layer must observe the resolved model, not the placeholder."""

    captured: dict[str, object] = {}

    async def runtime_model(_user_id: str, _role: str):
        return SimpleNamespace(model_name="actual-user-model")

    async def reserve(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(call_id="call-a", reserved_tokens=10)

    async def settle(_reservation, **_kwargs):
        return None

    monkeypatch.setattr(provider_models, "get_runtime_model", runtime_model)
    monkeypatch.setattr(token_usage, "_reserve", reserve)
    monkeypatch.setattr(token_usage, "_settle", settle)
    monkeypatch.setattr(token_usage, "get_config", dict)
    request = token_usage.ModelRequest(
        model=SimpleNamespace(model_name="provider-placeholder"),
        messages=[AIMessage(content="hello")],
        runtime=Runtime(
            server_info=SimpleNamespace(user=SimpleNamespace(identity="user-a"))
        ),
    )

    async def final_handler(_request):
        return token_usage.ModelResponse(
            result=[
                AIMessage(
                    content="ok",
                    usage_metadata={
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    },
                )
            ]
        )

    async def metered_handler(resolved_request):
        return await token_usage.TokenUsageMiddleware(
            agent_name="supervisor"
        ).awrap_model_call(resolved_request, final_handler)

    await provider_models.ProviderModelMiddleware("supervisor").awrap_model_call(
        request,
        metered_handler,
    )
    actual_model = captured["model"]
    assert isinstance(actual_model, SimpleNamespace)
    assert actual_model.model_name == "actual-user-model"


@pytest.mark.asyncio
async def test_runtime_model_cache_is_bounded_and_user_evictable(monkeypatch) -> None:
    closed: list[str] = []

    class SyncClient:
        def __init__(self, user_id: str) -> None:
            self.user_id = user_id

        def close(self) -> None:
            closed.append(f"sync:{self.user_id}")

    class AsyncClient:
        def __init__(self, user_id: str) -> None:
            self.user_id = user_id

        async def aclose(self) -> None:
            closed.append(f"async:{self.user_id}")

    async def resolved(user_id: str) -> service.ResolvedProvider:
        return service.ResolvedProvider(
            user_id=user_id,
            base_url="https://models.example.com/v1",
            model_name="model-one",
            api_key=f"secret-{user_id}",
            api_key_hint=user_id[-4:],
            version=1,
        )

    def built(provider: service.ResolvedProvider, _role: str):
        return SimpleNamespace(model_name=provider.model_name), SyncClient(
            provider.user_id
        ), AsyncClient(provider.user_id)

    monkeypatch.setattr(provider_models, "resolve_provider", resolved)
    monkeypatch.setattr(provider_models, "_build_model", built)
    monkeypatch.setattr(
        provider_models,
        "get_settings",
        lambda: SimpleNamespace(
            model_provider_cache_ttl_seconds=900,
            model_provider_cache_size=1,
        ),
    )
    provider_models._MODEL_CACHE.clear()

    await provider_models.get_runtime_model("user-a", "supervisor")
    await provider_models.get_runtime_model("user-b", "supervisor")
    assert len(provider_models._MODEL_CACHE) == 1
    assert closed == ["sync:user-a", "async:user-a"]

    await provider_models.clear_model_cache("user-b")
    assert provider_models._MODEL_CACHE == {}
    assert closed[-2:] == ["sync:user-b", "async:user-b"]


@pytest.mark.asyncio
async def test_provider_adapters_make_minimal_mock_call_without_serializing_key(
    monkeypatch,
    respx_mock,
) -> None:
    route = respx_mock.post("https://models.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "mock-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "OK",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            },
        )
    )
    monkeypatch.setattr(
        provider_models,
        "get_settings",
        lambda: SimpleNamespace(
            model_provider_test_timeout_seconds=5,
            model_provider_timeout_seconds=20,
            model_provider_streaming=False,
        ),
    )
    secret = "sk-mock-provider-secret"
    provider = service.ResolvedProvider(
        user_id="user-a",
        base_url="https://models.example.com/v1",
        model_name="mock-model",
        api_key=secret,
        api_key_hint="cret",
        version=1,
    )

    message = await provider_models.test_provider_model(provider)

    assert message.content == "OK"
    assert route.called
    assert route.calls[0].request.headers["authorization"] == f"Bearer {secret}"
    # LangSmith serializes model metadata, not the upstream HTTP request. The
    # model's secret field must remain masked in both representations.
    model, sync_client, async_client = provider_models._build_model(provider, "test")
    try:
        assert secret not in repr(model)
        assert secret not in json.dumps(model.to_json(), default=str)
    finally:
        sync_client.close()
        await async_client.aclose()
