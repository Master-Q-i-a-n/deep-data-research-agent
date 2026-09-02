"""Provider registry and model capabilities loaded from packaged YAML."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

import yaml
from langchain_core.language_models.model_profile import ModelProfile

_PROFILE_PATH = Path(__file__).with_name("model_profiles.yaml")

ProviderType = Literal["responses", "chat_completions", "anthropic"]
StructuredOutputMethod = Literal[
    "json_schema", "function_calling", "json_mode", "none"
]
_PROVIDER_TYPES = frozenset({"responses", "chat_completions", "anthropic"})
_STRUCTURED_METHODS = frozenset(
    {"json_schema", "function_calling", "json_mode", "none"}
)


@dataclass(frozen=True, slots=True)
class ModelProviderCapabilities:
    """Capabilities used by the application in addition to ModelProfile."""

    max_input_tokens: int | None = None
    supports_tools: bool = True
    supports_streaming: bool = True
    structured_output_method: StructuredOutputMethod = "json_mode"
    supports_forced_tool_choice: bool = False
    supports_reasoning_replay: bool = False
    supports_web_search: bool = False
    responses_include: tuple[str, ...] = ()

    def as_model_profile(self) -> ModelProfile:
        """Expose standard capabilities to LangChain's strategy selection."""

        profile: ModelProfile = {
            "tool_calling": self.supports_tools,
            "tool_choice": self.supports_forced_tool_choice,
            "tool_call_streaming": self.supports_tools and self.supports_streaming,
            "structured_output": self.structured_output_method == "json_schema",
        }
        if self.max_input_tokens is not None:
            profile["max_input_tokens"] = self.max_input_tokens
        return profile


@dataclass(frozen=True, slots=True)
class ResolvedModelProvider:
    """Provider identity, wire protocol, and effective model capabilities."""

    provider_name: str
    provider_type: ProviderType
    capabilities: ModelProviderCapabilities


@dataclass(frozen=True, slots=True)
class _ProviderConfig:
    name: str
    hosts: tuple[str, ...]
    protocol: ProviderType
    capabilities: ModelProviderCapabilities
    models: dict[str, tuple[ProviderType | None, dict[str, object]]]


def _as_mapping(value: object, message: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(message)
    return cast("dict[str, object]", value)


def _protocol(value: object, *, field: str) -> ProviderType:
    if not isinstance(value, str) or value not in _PROVIDER_TYPES:
        raise ValueError(f"{field} 必须是 responses、chat_completions 或 anthropic")
    return cast("ProviderType", value)


def _capability_overrides(raw: dict[str, object], *, owner: str) -> dict[str, object]:
    allowed = {
        "max_input_tokens",
        "supports_tools",
        "supports_streaming",
        "structured_output_method",
        "supports_forced_tool_choice",
        "supports_reasoning_replay",
        "supports_web_search",
        "responses_include",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"{owner} 包含未知能力字段：{', '.join(sorted(unknown))}")

    values = dict(raw)
    max_input_tokens = values.get("max_input_tokens")
    if max_input_tokens is not None and (
        isinstance(max_input_tokens, bool)
        or not isinstance(max_input_tokens, int)
        or max_input_tokens <= 0
    ):
        raise TypeError(f"{owner}.max_input_tokens 必须是正整数")
    for key in (
        "supports_tools",
        "supports_streaming",
        "supports_forced_tool_choice",
        "supports_reasoning_replay",
        "supports_web_search",
    ):
        if key in values and not isinstance(values[key], bool):
            raise TypeError(f"{owner}.{key} 必须是布尔值")
    method = values.get("structured_output_method")
    if method is not None and (
        not isinstance(method, str) or method not in _STRUCTURED_METHODS
    ):
        raise ValueError(f"{owner}.structured_output_method 无效")
    include = values.get("responses_include")
    if include is not None:
        if not isinstance(include, list) or not all(
            isinstance(item, str) and item.strip() for item in include
        ):
            raise TypeError(f"{owner}.responses_include 必须是非空字符串列表")
        values["responses_include"] = tuple(dict.fromkeys(include))
    return values


def _capabilities(raw: object, *, owner: str) -> ModelProviderCapabilities:
    mapping = _as_mapping(raw, f"{owner} 必须是映射")
    return ModelProviderCapabilities(**_capability_overrides(mapping, owner=owner))


def _validate_effective(
    protocol: ProviderType,
    capabilities: ModelProviderCapabilities,
    *,
    owner: str,
) -> None:
    if capabilities.supports_web_search and protocol != "responses":
        raise ValueError(f"{owner} 仅能为 Responses 协议启用 hosted web search")
    if capabilities.responses_include and protocol != "responses":
        raise ValueError(f"{owner} 仅能为 Responses 协议配置 responses_include")
    if (
        "reasoning.encrypted_content" in capabilities.responses_include
        and not capabilities.supports_reasoning_replay
    ):
        raise ValueError(f"{owner} 回传 reasoning 时必须启用 supports_reasoning_replay")
    if (
        "web_search_call.action.sources" in capabilities.responses_include
        and not capabilities.supports_web_search
    ):
        raise ValueError(f"{owner} 回传搜索来源时必须启用 supports_web_search")


def _load_provider_registry(
    path: Path = _PROFILE_PATH,
) -> tuple[ResolvedModelProvider, tuple[_ProviderConfig, ...]]:
    raw = _as_mapping(
        yaml.safe_load(path.read_text(encoding="utf-8")), "配置顶层必须是映射"
    )
    fallback_raw = _as_mapping(raw.get("fallback"), "fallback 必须是映射")
    fallback_name = fallback_raw.get("provider_name")
    if not isinstance(fallback_name, str) or not fallback_name.strip():
        raise ValueError("fallback.provider_name 必须是非空字符串")
    fallback_protocol = _protocol(
        fallback_raw.get("protocol"), field="fallback.protocol"
    )
    fallback_capabilities = _capabilities(
        fallback_raw.get("capabilities"), owner="fallback.capabilities"
    )
    _validate_effective(fallback_protocol, fallback_capabilities, owner="fallback")
    fallback = ResolvedModelProvider(
        fallback_name.strip(), fallback_protocol, fallback_capabilities
    )

    providers_raw = _as_mapping(raw.get("providers"), "providers 必须是映射")
    providers: list[_ProviderConfig] = []
    seen_hosts: set[str] = set()
    for raw_name, raw_provider in providers_raw.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("Provider 名称必须是非空字符串")
        owner = f"providers.{raw_name}"
        provider = _as_mapping(raw_provider, f"{owner} 必须是映射")
        raw_hosts = provider.get("hosts")
        if not isinstance(raw_hosts, list) or not raw_hosts:
            raise TypeError(f"{owner}.hosts 必须是非空列表")
        hosts: list[str] = []
        for raw_host in raw_hosts:
            if not isinstance(raw_host, str) or not raw_host.strip():
                raise TypeError(f"{owner}.hosts 必须只包含非空字符串")
            host = raw_host.strip().lower().rstrip(".")
            if host in seen_hosts:
                raise ValueError(f"Provider 域名重复：{host}")
            seen_hosts.add(host)
            hosts.append(host)

        provider_protocol = _protocol(
            provider.get("protocol"), field=f"{owner}.protocol"
        )
        provider_capabilities = _capabilities(
            provider.get("capabilities"), owner=f"{owner}.capabilities"
        )
        _validate_effective(provider_protocol, provider_capabilities, owner=owner)

        models: dict[str, tuple[ProviderType | None, dict[str, object]]] = {}
        models_raw = provider.get("models", {})
        for raw_model_name, raw_model in _as_mapping(
            models_raw, f"{owner}.models 必须是映射"
        ).items():
            if not isinstance(raw_model_name, str) or not raw_model_name.strip():
                raise ValueError(f"{owner}.models 的模型名必须是非空字符串")
            model_owner = f"{owner}.models.{raw_model_name}"
            model = _as_mapping(raw_model, f"{model_owner} 必须是映射")
            model_protocol = (
                _protocol(model["protocol"], field=f"{model_owner}.protocol")
                if "protocol" in model
                else None
            )
            overrides = _capability_overrides(
                {key: value for key, value in model.items() if key != "protocol"},
                owner=model_owner,
            )
            effective_protocol = model_protocol or provider_protocol
            effective_capabilities = replace(provider_capabilities, **overrides)
            _validate_effective(
                effective_protocol, effective_capabilities, owner=model_owner
            )
            normalized_model_name = raw_model_name.strip().lower()
            if normalized_model_name in models:
                raise ValueError(f"{owner}.models 存在重复模型名：{normalized_model_name}")
            models[normalized_model_name] = (model_protocol, overrides)

        providers.append(
            _ProviderConfig(
                name=raw_name.strip(),
                hosts=tuple(hosts),
                protocol=provider_protocol,
                capabilities=provider_capabilities,
                models=models,
            )
        )
    return fallback, tuple(providers)


FALLBACK_PROVIDER, PROVIDER_REGISTRY = _load_provider_registry()
_PROVIDERS_BY_HOST = {
    host: provider for provider in PROVIDER_REGISTRY for host in provider.hosts
}


def resolve_model_provider(base_url: str, model_name: str) -> ResolvedModelProvider:
    """Resolve an exact hostname and model override without probing the network."""

    hostname = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    provider = _PROVIDERS_BY_HOST.get(hostname)
    if provider is None:
        return FALLBACK_PROVIDER

    protocol = provider.protocol
    capabilities = provider.capabilities
    model = provider.models.get(model_name.strip().lower())
    if model is not None:
        protocol_override, capability_overrides = model
        protocol = protocol_override or protocol
        capabilities = replace(capabilities, **capability_overrides)
    return ResolvedModelProvider(provider.name, protocol, capabilities)


__all__ = [
    "FALLBACK_PROVIDER",
    "PROVIDER_REGISTRY",
    "ModelProviderCapabilities",
    "ProviderType",
    "ResolvedModelProvider",
    "StructuredOutputMethod",
    "resolve_model_provider",
]
