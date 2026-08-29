"""Local capability overrides for models served through user Providers."""

from __future__ import annotations

from pathlib import Path

import yaml
from langchain_core.language_models.model_profile import ModelProfile

_PROFILE_PATH = Path(__file__).with_name("model_profiles.yaml")


def _load_model_profiles(path: Path = _PROFILE_PATH) -> dict[str, ModelProfile]:
    """Load and validate exact model profiles from the packaged YAML file."""

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("model_profiles.yaml 顶层必须是模型映射")

    profiles: dict[str, ModelProfile] = {}
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

        model_name = raw_name.strip().lower()
        if model_name in profiles:
            raise ValueError(f"model_profiles.yaml 存在重复模型 ID：{model_name}")
        profiles[model_name] = {"max_input_tokens": max_input_tokens}
    return profiles


# OpenAI-compatible APIs do not expose these capabilities to LangChain.
MODEL_PROFILES = _load_model_profiles()


def model_profile(model_name: str) -> ModelProfile | None:
    """Return a copy of the local profile for one exact model ID."""

    profile = MODEL_PROFILES.get(model_name.strip().lower())
    return dict(profile) if profile is not None else None
