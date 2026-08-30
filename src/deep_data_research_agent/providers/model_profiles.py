"""Local capability overrides for models served through user Providers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from langchain_core.language_models.model_profile import ModelProfile

_PROFILE_PATH = Path(__file__).with_name("model_profiles.yaml")


@dataclass(frozen=True, slots=True)
class ModelProviderCapabilities:
    """Provider protocol features that are intentionally kept out of ModelProfile."""

    supports_responses_api: bool = False
    supports_web_search: bool = False
    responses_include: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ConfiguredModel:
    profile: ModelProfile
    capabilities: ModelProviderCapabilities


def _load_model_profiles(path: Path = _PROFILE_PATH) -> dict[str, _ConfiguredModel]:
    """Load and validate exact model profiles from the packaged YAML file."""

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("model_profiles.yaml 顶层必须是模型映射")

    profiles: dict[str, _ConfiguredModel] = {}
    for raw_name, raw_profile in raw.items():
        if not isinstance(raw_name, str):
            raise TypeError("model_profiles.yaml 的模型 ID 必须是字符串")
        if not raw_name.strip():
            raise ValueError("model_profiles.yaml 的模型 ID 必须是非空字符串")
        if not isinstance(raw_profile, dict):
            raise TypeError(f"模型 {raw_name} 的画像必须是映射")
        max_input_tokens = raw_profile.get("max_input_tokens")
        if not isinstance(max_input_tokens, int):
            raise TypeError(f"模型 {raw_name} 的 max_input_tokens 必须是整数")
        if max_input_tokens <= 0:
            raise ValueError(f"模型 {raw_name} 的 max_input_tokens 必须是正整数")
        supports_responses_api = raw_profile.get("supports_responses_api", False)
        supports_web_search = raw_profile.get("supports_web_search", False)
        responses_include = raw_profile.get("responses_include", [])
        if not isinstance(supports_responses_api, bool):
            raise TypeError(f"模型 {raw_name} 的 supports_responses_api 必须是布尔值")
        if not isinstance(supports_web_search, bool):
            raise TypeError(f"模型 {raw_name} 的 supports_web_search 必须是布尔值")
        if not isinstance(responses_include, list) or not all(
            isinstance(value, str) and value.strip() for value in responses_include
        ):
            raise TypeError(f"模型 {raw_name} 的 responses_include 必须是非空字符串列表")
        if supports_web_search and not supports_responses_api:
            raise ValueError(
                f"模型 {raw_name} 启用 web_search 时必须同时启用 Responses API"
            )
        if responses_include and not supports_responses_api:
            raise ValueError(
                f"模型 {raw_name} 配置 responses_include 时必须同时启用 Responses API"
            )

        model_name = raw_name.strip().lower()
        if model_name in profiles:
            raise ValueError(f"model_profiles.yaml 存在重复模型 ID：{model_name}")
        profiles[model_name] = _ConfiguredModel(
            profile={"max_input_tokens": max_input_tokens},
            capabilities=ModelProviderCapabilities(
                supports_responses_api=supports_responses_api,
                supports_web_search=supports_web_search,
                responses_include=tuple(dict.fromkeys(responses_include)),
            ),
        )
    return profiles


# OpenAI-compatible APIs do not expose these capabilities to LangChain.
MODEL_PROFILES = _load_model_profiles()


def model_profile(model_name: str) -> ModelProfile | None:
    """Return a copy of the local profile for one exact model ID."""

    configured = MODEL_PROFILES.get(model_name.strip().lower())
    return dict(configured.profile) if configured is not None else None


def model_capabilities(model_name: str) -> ModelProviderCapabilities:
    """Return exact-match protocol capabilities; unknown models are conservative."""

    configured = MODEL_PROFILES.get(model_name.strip().lower())
    return configured.capabilities if configured is not None else ModelProviderCapabilities()


__all__ = ["ModelProviderCapabilities", "model_capabilities", "model_profile"]
