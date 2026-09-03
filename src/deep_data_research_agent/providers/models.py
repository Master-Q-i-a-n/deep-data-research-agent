"""Runtime model routing for authenticated per-user Provider settings."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from deepagents.middleware.summarization import (
    SummarizationState,
    create_summarization_middleware,
)
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from deep_data_research_agent.core.config import HarnessChatOpenAI, get_settings
from deep_data_research_agent.core.identity import user_identity
from deep_data_research_agent.core.model_execution import ModelExecutionProfile
from deep_data_research_agent.providers.context_usage import ContextTokenCounter
from deep_data_research_agent.providers.model_profiles import (
    ModelProviderCapabilities,
    ProviderType,
)
from deep_data_research_agent.providers.responses import ResponsesWebSearchChatOpenAI
from deep_data_research_agent.providers.service import (
    ResolvedProvider,
    resolve_provider,
)


@dataclass(frozen=True, slots=True)
class RuntimeModel:
    """One resolved model plus non-standard application capabilities."""

    model: BaseChatModel
    provider_name: str
    provider_type: ProviderType
    capabilities: ModelProviderCapabilities
    provider_version: int


@dataclass(slots=True)
class _BuiltModel:
    runtime: RuntimeModel
    sync_client: httpx.Client | None = None
    async_client: httpx.AsyncClient | None = None


@dataclass(slots=True)
class _CacheEntry:
    built: _BuiltModel
    expires_at: float


_MODEL_CACHE: OrderedDict[
    tuple[str, int, ModelExecutionProfile], _CacheEntry
] = OrderedDict()
_MODEL_CACHE_LOCK = asyncio.Lock()


class _HarnessChatAnthropic(ChatAnthropic):
    """Select an opaque DeepAgents harness for Anthropic Native."""

    harness_provider: str = "openai"

    def _get_ls_params(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        params = super()._get_ls_params(*args, **kwargs)
        params["ls_provider"] = self.harness_provider
        return params


class _HarnessResponsesWebSearchChatOpenAI(ResponsesWebSearchChatOpenAI):
    """Add an opaque DeepAgents harness to the Responses search adapter."""

    harness_provider: str = "openai"

    def _get_ls_params(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        params = super()._get_ls_params(*args, **kwargs)
        params["ls_provider"] = self.harness_provider
        return params


def _openai_model_class(profile: ModelExecutionProfile) -> type[ChatOpenAI]:
    """Select only the protocol extension required by the execution profile."""

    if profile.enable_hosted_web_search:
        return _HarnessResponsesWebSearchChatOpenAI
    return HarnessChatOpenAI


def _attach_runtime_metadata(model: BaseChatModel, provider: ResolvedProvider) -> None:
    """Attach non-secret metadata used by metering and context checkpoints."""

    model.profile = {
        **(model.profile or {}),
        **provider.capabilities.as_model_profile(),
    }
    object.__setattr__(model, "_deep_data_provider_version", provider.version)
    object.__setattr__(model, "_deep_data_provider_type", provider.provider_type)
    object.__setattr__(model, "_deep_data_provider_name", provider.provider_name)


def _build_openai_model(
    provider: ResolvedProvider,
    profile: ModelExecutionProfile,
    *,
    timeout: float,
    test_timeout: bool,
) -> _BuiltModel:
    # Explicit clients keep redirects disabled for user-supplied compatible URLs.
    sync_client = httpx.Client(follow_redirects=False, timeout=timeout)
    async_client = httpx.AsyncClient(follow_redirects=False, timeout=timeout)
    streaming = (
        profile.enable_streaming
        and get_settings().model_provider_streaming
        and provider.capabilities.supports_streaming
    )
    options: dict[str, Any] = {
        "use_responses_api": provider.provider_type == "responses",
    }
    if provider.provider_type == "responses":
        include = [
            value
            for value in provider.capabilities.responses_include
            if value != "web_search_call.action.sources"
            or profile.enable_hosted_web_search
        ]
        options.update(output_version="responses/v1", store=False)
        if include:
            options["include"] = include

    model = _openai_model_class(profile)(
        model=provider.model_name,
        api_key=provider.api_key,
        base_url=provider.base_url,
        temperature=0,
        timeout=timeout,
        max_retries=0 if test_timeout else profile.max_retries,
        streaming=streaming,
        http_client=sync_client,
        http_async_client=async_client,
        harness_provider=profile.harness_provider,
        **options,
    )
    _attach_runtime_metadata(model, provider)
    return _BuiltModel(
        runtime=RuntimeModel(
            model=model,
            provider_name=provider.provider_name,
            provider_type=provider.provider_type,
            capabilities=provider.capabilities,
            provider_version=provider.version,
        ),
        sync_client=sync_client,
        async_client=async_client,
    )


def _build_anthropic_model(
    provider: ResolvedProvider,
    profile: ModelExecutionProfile,
    *,
    timeout: float,
    test_timeout: bool,
) -> _BuiltModel:
    streaming = (
        profile.enable_streaming
        and get_settings().model_provider_streaming
        and provider.capabilities.supports_streaming
    )
    model = _HarnessChatAnthropic(
        model=provider.model_name,
        api_key=provider.api_key,
        # Anthropic Native is restricted to the official endpoint in service.py.
        base_url=provider.base_url,
        temperature=0,
        timeout=timeout,
        max_retries=0 if test_timeout else profile.max_retries,
        streaming=streaming,
        harness_provider=profile.harness_provider,
    )
    _attach_runtime_metadata(model, provider)
    return _BuiltModel(
        runtime=RuntimeModel(
            model=model,
            provider_name=provider.provider_name,
            provider_type=provider.provider_type,
            capabilities=provider.capabilities,
            provider_version=provider.version,
        )
    )


def _build_model(
    provider: ResolvedProvider,
    profile: ModelExecutionProfile,
    *,
    test_timeout: bool = False,
) -> _BuiltModel:
    settings = get_settings()
    timeout = (
        settings.model_provider_test_timeout_seconds
        if test_timeout
        else settings.model_provider_timeout_seconds
    )
    if provider.provider_type == "anthropic":
        return _build_anthropic_model(
            provider, profile, timeout=timeout, test_timeout=test_timeout
        )
    return _build_openai_model(
        provider, profile, timeout=timeout, test_timeout=test_timeout
    )


async def _close_entries(entries: list[_CacheEntry]) -> None:
    for entry in entries:
        if entry.built.sync_client is not None:
            entry.built.sync_client.close()
        if entry.built.async_client is not None:
            await entry.built.async_client.aclose()


async def get_runtime_model(
    user_id: str,
    profile: ModelExecutionProfile,
) -> RuntimeModel:
    """Resolve and cache one execution-specific model without caching by API Key."""

    provider = await resolve_provider(user_id)
    key = (provider.user_id, provider.version, profile)
    now = time.monotonic()
    evicted: list[_CacheEntry] = []
    async with _MODEL_CACHE_LOCK:
        for cached_key, entry in list(_MODEL_CACHE.items()):
            if entry.expires_at <= now:
                evicted.append(_MODEL_CACHE.pop(cached_key))
        entry = _MODEL_CACHE.get(key)
        if entry is not None:
            _MODEL_CACHE.move_to_end(key)
            runtime = entry.built.runtime
        else:
            built = _build_model(provider, profile)
            runtime = built.runtime
            _MODEL_CACHE[key] = _CacheEntry(
                built=built,
                expires_at=now + get_settings().model_provider_cache_ttl_seconds,
            )
            while len(_MODEL_CACHE) > get_settings().model_provider_cache_size:
                _, removed = _MODEL_CACHE.popitem(last=False)
                evicted.append(removed)
    await _close_entries(evicted)
    return runtime


async def clear_model_cache(user_id: str) -> None:
    """Evict decrypted model clients after a Provider update or deletion."""

    evicted: list[_CacheEntry] = []
    async with _MODEL_CACHE_LOCK:
        for key in list(_MODEL_CACHE):
            if key[0] == str(user_id):
                evicted.append(_MODEL_CACHE.pop(key))
    await _close_entries(evicted)


async def test_provider_model(provider: ResolvedProvider) -> AIMessage:
    """Perform one minimal call through only the configured protocol."""

    built = _build_model(
        provider,
        ModelExecutionProfile(name="provider-test", max_retries=0),
        test_timeout=True,
    )
    try:
        return await built.runtime.model.ainvoke("Return exactly: OK")
    finally:
        if built.sync_client is not None:
            built.sync_client.close()
        if built.async_client is not None:
            await built.async_client.aclose()


def _runtime_tools(
    request: ModelRequest,
    runtime: RuntimeModel,
    profile: ModelExecutionProfile,
) -> list[Any]:
    tools = list(request.tools)
    if (
        profile.enable_hosted_web_search
        and runtime.provider_type == "responses"
        and runtime.capabilities.supports_web_search
        and not any(
            isinstance(tool, dict) and tool.get("type") == "web_search"
            for tool in tools
        )
    ):
        tools.append({"type": "web_search"})
    return tools


class ProviderSummarizationMiddleware(AgentMiddleware):
    """Inject the user model before request-aware DeepAgents summarization."""

    state_schema = SummarizationState

    def __init__(self, profile: ModelExecutionProfile, backend: Any) -> None:
        self.profile = profile
        self.backend = backend

    @property
    def name(self) -> str:
        """Replace DeepAgents' built-in middleware by its public name."""

        return "SummarizationMiddleware"

    def _delegate(self, request: ModelRequest) -> AgentMiddleware:
        token_counter = ContextTokenCounter(request)
        delegate = create_summarization_middleware(
            request.model,
            self.backend,
            token_counter=token_counter,
        )
        # Historical usage anchors apply only to this exact effective request.
        token_counter.bind(delegate._get_effective_messages(request))
        return delegate

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        del request, handler
        raise RuntimeError("Provider 模型注入仅支持异步 Agent 调用")

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        runtime = await get_runtime_model(
            user_identity(request.runtime),
            self.profile,
        )
        effective_request = request.override(
            model=runtime.model,
            tools=_runtime_tools(request, runtime, self.profile),
        )
        return await self._delegate(effective_request).awrap_model_call(
            effective_request, handler
        )

def structured_output_model(
    runtime: RuntimeModel,
    schema: type[Any],
    *,
    include_raw: bool = True,
) -> BaseChatModel:
    """Apply the configured structured-output strategy outside an Agent."""

    method = runtime.capabilities.structured_output_method
    if method == "none":
        raise RuntimeError("当前模型未配置结构化输出能力")
    return runtime.model.with_structured_output(
        schema,
        method=method,
        include_raw=include_raw,
    )


def direct_output_limit_kwargs(
    runtime: RuntimeModel,
    max_output_tokens: int,
) -> dict[str, Any]:
    """Translate one business output limit into protocol-specific kwargs."""

    if runtime.provider_type == "responses":
        return {"max_output_tokens": max_output_tokens}
    if runtime.provider_type == "anthropic":
        return {"max_tokens": max_output_tokens}
    # Compatible chat backends such as DeepSeek expect this provider spelling.
    return {"extra_body": {"max_tokens": max_output_tokens}}


__all__ = [
    "ModelExecutionProfile",
    "ProviderSummarizationMiddleware",
    "RuntimeModel",
    "clear_model_cache",
    "direct_output_limit_kwargs",
    "get_runtime_model",
    "structured_output_model",
    "test_provider_model",
]
