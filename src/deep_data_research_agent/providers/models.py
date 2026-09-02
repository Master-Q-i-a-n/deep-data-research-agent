"""Runtime model routing for authenticated per-user Provider settings."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from deepagents.middleware.summarization import (
    SummarizationState,
    create_summarization_middleware,
)
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.config import get_config

from deep_data_research_agent.core.config import (
    _ReviewerChatOpenAI,
    _WorkerChatOpenAI,
    get_settings,
)
from deep_data_research_agent.core.identity import (
    user_identity,
    user_identity_from_config,
)
from deep_data_research_agent.providers.context_usage import ContextTokenCounter
from deep_data_research_agent.providers.model_profiles import (
    model_capabilities,
    model_profile,
)
from deep_data_research_agent.providers.responses import SupervisorResponsesChatOpenAI
from deep_data_research_agent.providers.service import (
    ResolvedProvider,
    resolve_provider,
)

ModelRole = Literal[
    "supervisor",
    "data-analyst",
    "analysis-reviewer",
    "crawl-worker",
    "memory",
    "test",
]


@dataclass(slots=True)
class _CacheEntry:
    model: BaseChatModel
    sync_client: httpx.Client
    async_client: httpx.AsyncClient
    expires_at: float


_MODEL_CACHE: OrderedDict[tuple[str, int, ModelRole], _CacheEntry] = OrderedDict()
_MODEL_CACHE_LOCK = asyncio.Lock()


def _model_class(role: ModelRole) -> type[BaseChatModel]:
    """Choose only the local harness class; protocol selection comes from YAML."""

    if role == "analysis-reviewer":
        return _ReviewerChatOpenAI
    if role in {"data-analyst", "crawl-worker"}:
        return _WorkerChatOpenAI
    if role == "supervisor":
        return SupervisorResponsesChatOpenAI
    # Generic OpenAI-compatible memory and connection tests do not need a
    # harness-specific subclass.
    from langchain_openai import ChatOpenAI

    return ChatOpenAI


def _build_model(
    provider: ResolvedProvider,
    role: ModelRole,
    *,
    test_timeout: bool = False,
) -> tuple[BaseChatModel, httpx.Client, httpx.AsyncClient]:
    settings = get_settings()
    timeout = (
        settings.model_provider_test_timeout_seconds
        if test_timeout
        else settings.model_provider_timeout_seconds
    )
    # Provider redirects are not followed so a public endpoint cannot redirect
    # a model request into an internal network after URL validation.
    sync_client = httpx.Client(follow_redirects=False, timeout=timeout)
    async_client = httpx.AsyncClient(follow_redirects=False, timeout=timeout)
    model_class = _model_class(role)
    streaming = role == "supervisor" and settings.model_provider_streaming
    capabilities = model_capabilities(provider.model_name)
    model_options: dict[str, Any] = {}
    if capabilities.supports_responses_api:
        # Responses-compatible providers differ in supported include values.
        # Only send values explicitly declared for this exact model.
        include = [
            value
            for value in capabilities.responses_include
            if value != "web_search_call.action.sources" or role == "supervisor"
        ]
        model_options.update(
            use_responses_api=True,
            output_version="responses/v1",
            store=False,
        )
        if include:
            model_options["include"] = include
    model = model_class(
        model=provider.model_name,
        api_key=provider.api_key,
        base_url=provider.base_url,
        temperature=0,
        timeout=timeout,
        max_retries=0 if test_timeout or role == "memory" else 2,
        streaming=streaming,
        http_client=sync_client,
        http_async_client=async_client,
        **model_options,
    )
    local_profile = model_profile(provider.model_name)
    if local_profile is not None:
        # Preserve capabilities supplied by a dedicated integration while
        # letting the local registry override values such as context length.
        model.profile = {**(model.profile or {}), **local_profile}
    # Keep the configuration version beside the cached client. It contains no
    # secret and lets checkpoint anchors reject usage from an older Provider.
    object.__setattr__(model, "_deep_data_provider_version", provider.version)
    return model, sync_client, async_client


async def _close_entries(entries: list[_CacheEntry]) -> None:
    for entry in entries:
        entry.sync_client.close()
        await entry.async_client.aclose()


async def get_runtime_model(user_id: str, role: ModelRole) -> BaseChatModel:
    """Resolve and cache one role-specific model without caching by API Key."""

    provider = await resolve_provider(user_id)
    key = (provider.user_id, provider.version, role)
    now = time.monotonic()
    evicted: list[_CacheEntry] = []
    async with _MODEL_CACHE_LOCK:
        for cached_key, entry in list(_MODEL_CACHE.items()):
            if entry.expires_at <= now:
                evicted.append(_MODEL_CACHE.pop(cached_key))
        entry = _MODEL_CACHE.get(key)
        if entry is not None:
            _MODEL_CACHE.move_to_end(key)
            model = entry.model
        else:
            model, sync_client, async_client = _build_model(provider, role)
            _MODEL_CACHE[key] = _CacheEntry(
                model=model,
                sync_client=sync_client,
                async_client=async_client,
                expires_at=now + get_settings().model_provider_cache_ttl_seconds,
            )
            while len(_MODEL_CACHE) > get_settings().model_provider_cache_size:
                _, removed = _MODEL_CACHE.popitem(last=False)
                evicted.append(removed)
    await _close_entries(evicted)
    return model


async def clear_model_cache(user_id: str) -> None:
    """Evict decrypted model clients after a Provider update or deletion."""

    evicted: list[_CacheEntry] = []
    async with _MODEL_CACHE_LOCK:
        for key in list(_MODEL_CACHE):
            if key[0] == str(user_id):
                evicted.append(_MODEL_CACHE.pop(key))
    await _close_entries(evicted)


async def test_provider_model(provider: ResolvedProvider) -> AIMessage:
    """Perform one minimal non-streaming chat call without entering the cache."""

    model, sync_client, async_client = _build_model(
        provider,
        "test",
        test_timeout=True,
    )
    try:
        return await model.ainvoke("Return exactly: OK")
    finally:
        sync_client.close()
        await async_client.aclose()


class ProviderModelMiddleware(AgentMiddleware):
    """Replace the graph-import placeholder with the authenticated user's model."""

    def __init__(self, role: ModelRole) -> None:
        self.role = role

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        model = await get_runtime_model(user_identity(request.runtime), self.role)
        overrides: dict[str, Any] = {"model": model}
        capabilities = model_capabilities(model.model_name)
        if (
            self.role == "supervisor"
            and capabilities.supports_responses_api
            and capabilities.supports_web_search
        ):
            tools = list(request.tools)
            if not any(
                isinstance(tool, dict) and tool.get("type") == "web_search"
                for tool in tools
            ):
                tools.append({"type": "web_search"})
            overrides["tools"] = tools
        return await handler(request.override(**overrides))


class ProviderSummaryChatModel(BaseChatModel):
    """Delegate DeepAgents' direct summarization calls to the user Provider."""

    role: ModelRole

    @property
    def _llm_type(self) -> str:
        return "deep-data-provider-summary"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        del messages, stop, run_manager, kwargs
        raise RuntimeError(
            "Provider summarization only supports asynchronous Agent runs"
        )

    async def _agenerate(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        del run_manager
        config = get_config()
        model = await get_runtime_model(user_identity_from_config(config), self.role)
        message = await model.ainvoke(messages, config=config, stop=stop, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=message)])


class ProviderSummarizationMiddleware(AgentMiddleware):
    """Select summarization limits from the model resolved for this request."""

    state_schema = SummarizationState

    def __init__(self, role: ModelRole, backend: Any) -> None:
        self.role = role
        self.backend = backend

    @property
    def name(self) -> str:
        """Replace DeepAgents' built-in middleware by its public name."""

        return "SummarizationMiddleware"

    def _delegate(self, request: ModelRequest) -> AgentMiddleware:
        profile = getattr(request.model, "profile", None)
        max_input_tokens = (
            profile.get("max_input_tokens") if isinstance(profile, dict) else None
        )
        if not isinstance(max_input_tokens, int) or max_input_tokens <= 0:
            max_input_tokens = None

        summary_profile = (
            {"max_input_tokens": max_input_tokens}
            if max_input_tokens is not None
            else None
        )
        token_counter = ContextTokenCounter(request)
        delegate = create_summarization_middleware(
            ProviderSummaryChatModel(role=self.role, profile=summary_profile),
            self.backend,
            token_counter=token_counter,
        )
        # The counter uses the historical usage anchor only for this exact
        # effective request. DeepAgents' cutoff probes remain pure estimates.
        token_counter.bind(delegate._get_effective_messages(request))
        return delegate

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Delegate synchronous calls using the request model's profile."""

        return self._delegate(request).wrap_model_call(request, handler)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Delegate asynchronous calls using the request model's profile."""

        return await self._delegate(request).awrap_model_call(request, handler)


def provider_summarization_middleware(role: ModelRole, backend: Any) -> AgentMiddleware:
    """Build a request-aware replacement for DeepAgents' static summarizer."""

    return ProviderSummarizationMiddleware(role, backend)


__all__ = [
    "ModelRole",
    "ProviderModelMiddleware",
    "ProviderSummarizationMiddleware",
    "ProviderSummaryChatModel",
    "clear_model_cache",
    "get_runtime_model",
    "provider_summarization_middleware",
    "test_provider_model",
]
