from __future__ import annotations

import ipaddress
import json
from contextlib import asynccontextmanager
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


def _resolved_provider(
    base_url: str,
    model_name: str,
    *,
    user_id: str = "user-a",
    api_key: str = "test-key",
) -> service.ResolvedProvider:
    normalized = service.normalize_provider_url(base_url)
    normalized, provider_name, provider_type, capabilities = (
        service.resolve_provider_metadata(normalized, model_name)
    )
    return service.ResolvedProvider(
        user_id=user_id,
        provider_name=provider_name,
        provider_type=provider_type,
        base_url=normalized,
        model_name=model_name,
        api_key=api_key,
        api_key_hint=api_key[-4:],
        version=1,
        capabilities=capabilities,
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
        "provider_name": "openai-compatible",
        "provider_type": "chat_completions",
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
        assert stored.provider_type == "chat_completions"
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
async def test_provider_url_allows_private_https_but_http_requires_allowlist(
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

    assert (
        await service.validate_provider_url("https://localhost:9000/v1")
        == "https://localhost:9000/v1"
    )
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
    ("base_url", "model_name", "provider_name", "provider_type"),
    [
        ("https://api.openai.com/v1", "gpt-5.6", "openai", "responses"),
        (
            "https://API.OPENAI.COM./v1",
            "unknown-gpt",
            "openai",
            "chat_completions",
        ),
        ("https://api.deepseek.com", "deepseek-v4-pro", "deepseek", "responses"),
        (
            "https://api.deepseek.com",
            "unknown-deepseek",
            "deepseek",
            "chat_completions",
        ),
        (
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "qwen-plus",
            "dashscope",
            "chat_completions",
        ),
        ("https://api.anthropic.com", "claude-sonnet", "anthropic", "anthropic"),
        (
            "https://models.example.com/v1",
            "custom-model",
            "openai-compatible",
            "chat_completions",
        ),
    ],
)
def test_provider_registry_resolves_exact_host_and_model_override(
    base_url: str,
    model_name: str,
    provider_name: str,
    provider_type: str,
) -> None:
    resolved = model_profiles.resolve_model_provider(base_url, model_name)

    assert resolved.provider_name == provider_name
    assert resolved.provider_type == provider_type
    assert resolved.capabilities.supports_tools is True


def test_provider_registry_does_not_fuzzy_match_hostname() -> None:
    resolved = model_profiles.resolve_model_provider(
        "https://proxy.api.openai.com/v1", "gpt-5.6"
    )

    assert resolved.provider_name == "openai-compatible"
    assert resolved.provider_type == "chat_completions"


def test_registered_responses_capabilities_are_model_specific() -> None:
    openai = model_profiles.resolve_model_provider(
        "https://api.openai.com/v1", "GPT-5.4-mini"
    )
    deepseek = model_profiles.resolve_model_provider(
        "https://api.deepseek.com", "deepseek-v4-flash"
    )

    assert openai.capabilities.max_input_tokens == 400_000
    assert openai.capabilities.supports_web_search is True
    assert openai.capabilities.responses_include == (
        "reasoning.encrypted_content",
        "web_search_call.action.sources",
    )
    assert deepseek.capabilities.max_input_tokens == 1_000_000
    assert deepseek.capabilities.supports_web_search is True
    assert deepseek.capabilities.responses_include == ()


def test_web_search_configuration_requires_responses_protocol(tmp_path: Path) -> None:
    profile_path = tmp_path / "profiles.yaml"
    profile_path.write_text(
        "fallback:\n"
        "  provider_name: generic\n"
        "  protocol: chat_completions\n"
        "  capabilities:\n"
        "    supports_web_search: true\n"
        "providers: {}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Responses 协议"):
        model_profiles._load_provider_registry(profile_path)


def test_anthropic_native_rejects_proxy_and_non_root_url() -> None:
    with pytest.raises(service.ProviderConfigurationError, match="官方根地址"):
        service.resolve_provider_metadata(
            "https://api.anthropic.com/v1", "claude-sonnet"
        )

    proxy = service.resolve_provider_metadata(
        "https://anthropic-proxy.example.com", "claude-sonnet"
    )
    assert proxy[2] == "chat_completions"


def test_models_without_tool_support_are_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "resolve_model_provider",
        lambda _url, _model: model_profiles.ResolvedModelProvider(
            "disabled",
            "chat_completions",
            model_profiles.ModelProviderCapabilities(supports_tools=False),
        ),
    )
    with pytest.raises(service.ProviderConfigurationError, match="工具调用"):
        service.resolve_provider_metadata("https://models.example.com", "model")


def test_responses_include_requires_responses_protocol(tmp_path: Path) -> None:
    profile_path = tmp_path / "profiles.yaml"
    profile_path.write_text(
        "fallback:\n"
        "  provider_name: generic\n"
        "  protocol: chat_completions\n"
        "  capabilities:\n"
        "    responses_include: [reasoning.encrypted_content]\n"
        "providers: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="responses_include"):
        model_profiles._load_provider_registry(profile_path)


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
    provider = _resolved_provider("https://api.openai.com/v1", "gpt-5.4")

    built = provider_models._build_model(provider, role)
    model = built.runtime.model
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
        assert built.sync_client is not None
        built.sync_client.close()
        # Construction is synchronous; close the async client in the test loop below.
        import asyncio

        assert built.async_client is not None
        asyncio.run(built.async_client.aclose())


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
    provider = _resolved_provider(
        "https://api.deepseek.com", "deepseek-v4-flash"
    )

    built = provider_models._build_model(
        provider,
        "supervisor",
    )
    model = built.runtime.model
    try:
        assert model.use_responses_api is True
        assert model.include is None
        assert model.extra_body is None
    finally:
        assert built.sync_client is not None
        built.sync_client.close()
        import asyncio

        assert built.async_client is not None
        asyncio.run(built.async_client.aclose())


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
    provider = _resolved_provider(
        "https://api.deepseek.com", "deepseek-v4-flash"
    )

    message = await provider_models.test_provider_model(provider)

    assert message.text == "OK"
    assert route.called
    payload = json.loads(route.calls[0].request.content)
    assert payload["store"] is False
    assert "include" not in payload
    assert "tools" not in payload


def test_only_supervisor_receives_builtin_web_search() -> None:
    runtime = Runtime(
        server_info=SimpleNamespace(user=SimpleNamespace(identity="user-a"))
    )
    capabilities = model_profiles.resolve_model_provider(
        "https://api.openai.com/v1", "gpt-5.4"
    ).capabilities
    resolved = provider_models.RuntimeModel(
        model=SimpleNamespace(model_name="gpt-5.4"),
        provider_name="openai",
        provider_type="responses",
        capabilities=capabilities,
        provider_version=1,
    )

    def captured_tools(role: provider_models.ModelRole):
        request = provider_models.ModelRequest(
            model=SimpleNamespace(model_name="placeholder"),
            messages=[HumanMessage(content="hello")],
            runtime=runtime,
            tools=[],
        )
        return provider_models._runtime_tools(request, resolved, role)

    assert captured_tools("supervisor") == [{"type": "web_search"}]
    assert captured_tools("data-analyst") == []
    assert captured_tools("crawl-worker") == []


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
async def test_provider_save_resolves_and_persists_without_model_call(monkeypatch) -> None:
    saved: dict[str, object] = {}

    async def authenticated(_authorization: str | None) -> str:
        return "user-a"

    @asynccontextmanager
    async def admission_lock(_user_id: str):
        yield

    async def not_busy(_request, _authorization):
        return []

    async def save(**kwargs):
        saved.update(kwargs)
        return SimpleNamespace(version=2)

    async def public(_user_id: str):
        return {
            "provider_name": "openai",
            "provider_type": "responses",
            "base_url": "https://api.openai.com/v1",
            "model_name": "gpt-5.4",
            "has_api_key": True,
            "api_key_hint": "cret",
            "version": 2,
            "updated_at": "2026-09-02T00:00:00",
        }

    async def no_model_call(_provider):
        raise AssertionError("保存接口不应调用上游模型")

    async def clear_cache(_user_id: str):
        return None

    monkeypatch.setattr(webapp, "_authenticated_user_id", authenticated)
    monkeypatch.setattr(webapp.redis_limits, "admission_lock", admission_lock)
    monkeypatch.setattr(webapp, "_busy_provider_thread_ids", not_busy)
    monkeypatch.setattr(webapp, "save_provider", save)
    monkeypatch.setattr(webapp, "get_public_provider", public)
    monkeypatch.setattr(webapp, "clear_model_cache", clear_cache)
    monkeypatch.setattr(webapp, "test_provider_model", no_model_call)
    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/model-provider",
            "headers": [],
            "client": ("127.0.0.1", 50000),
        }
    )

    response = await webapp.update_model_provider(
        webapp.ModelProviderRequest(
            base_url="https://api.openai.com/v1",
            model_name="gpt-5.4",
            api_key="sk-save-secret",
        ),
        request,
        None,
    )

    assert response.status_code == 200
    assert saved == {
        "user_id": "user-a",
        "base_url": "https://api.openai.com/v1",
        "model_name": "gpt-5.4",
        "api_key": "sk-save-secret",
    }


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
    capabilities = model_profiles.FALLBACK_PROVIDER.capabilities

    async def runtime_model(_user_id: str, _role: str):
        return provider_models.RuntimeModel(
            model=SimpleNamespace(model_name="actual-user-model"),
            provider_name="openai-compatible",
            provider_type="chat_completions",
            capabilities=capabilities,
            provider_version=1,
        )

    class PassThroughSummarizer:
        async def awrap_model_call(self, request, handler):
            return await handler(request)

    async def reserve(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(call_id="call-a", reserved_tokens=10)

    async def settle(_reservation, **_kwargs):
        return None

    monkeypatch.setattr(provider_models, "get_runtime_model", runtime_model)
    monkeypatch.setattr(
        provider_models.ProviderSummarizationMiddleware,
        "_delegate",
        lambda _self, _request: PassThroughSummarizer(),
    )
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

    await provider_models.ProviderSummarizationMiddleware(
        "supervisor", StateBackend()
    ).awrap_model_call(
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
        return _resolved_provider(
            "https://models.example.com/v1",
            "model-one",
            user_id=user_id,
            api_key=f"secret-{user_id}",
        )

    def built(provider: service.ResolvedProvider, _role: str):
        return provider_models._BuiltModel(
            runtime=provider_models.RuntimeModel(
                model=SimpleNamespace(model_name=provider.model_name),
                provider_name=provider.provider_name,
                provider_type=provider.provider_type,
                capabilities=provider.capabilities,
                provider_version=provider.version,
            ),
            sync_client=SyncClient(provider.user_id),
            async_client=AsyncClient(provider.user_id),
        )

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
    provider = _resolved_provider(
        "https://models.example.com/v1",
        "mock-model",
        api_key=secret,
    )

    message = await provider_models.test_provider_model(provider)

    assert message.content == "OK"
    assert route.called
    assert route.calls[0].request.headers["authorization"] == f"Bearer {secret}"
    # LangSmith serializes model metadata, not the upstream HTTP request. The
    # model's secret field must remain masked in both representations.
    built = provider_models._build_model(provider, "test")
    model = built.runtime.model
    try:
        assert secret not in repr(model)
        assert secret not in json.dumps(model.to_json(), default=str)
    finally:
        assert built.sync_client is not None
        built.sync_client.close()
        assert built.async_client is not None
        await built.async_client.aclose()


@pytest.mark.asyncio
async def test_anthropic_native_calls_official_messages_endpoint(
    monkeypatch,
    respx_mock,
) -> None:
    route = respx_mock.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg-test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "OK"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 2, "output_tokens": 1},
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
    secret = "sk-ant-test-secret"
    provider = _resolved_provider(
        "https://api.anthropic.com",
        "claude-sonnet-4-5",
        api_key=secret,
    )

    message = await provider_models.test_provider_model(provider)

    assert message.text == "OK"
    assert route.called
    request = route.calls[0].request
    assert request.headers["x-api-key"] == secret
    assert request.headers["anthropic-version"]
    assert "/responses" not in request.url.path
    assert "/chat/completions" not in request.url.path


@pytest.mark.parametrize(
    ("role", "harness"),
    [
        ("supervisor", "openai"),
        ("data-analyst", "deep-data-worker"),
        ("crawl-worker", "deep-data-worker"),
        ("analysis-reviewer", "deep-data-reviewer"),
    ],
)
def test_anthropic_roles_keep_application_harness(
    role: provider_models.ModelRole,
    harness: str,
) -> None:
    model_class = provider_models._anthropic_model_class(role)
    model = model_class(
        model="claude-sonnet-4-5",
        api_key="sk-test",
        base_url="https://api.anthropic.com",
    )

    assert model._get_ls_params()["ls_provider"] == harness
