"""Application settings and OpenAI-compatible model construction."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from langchain_openai import ChatOpenAI
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class _WorkerChatOpenAI(ChatOpenAI):
    """Select the worker-specific DeepAgents harness without changing API calls."""

    def _get_ls_params(self, *args, **kwargs):
        params = super()._get_ls_params(*args, **kwargs)
        params["ls_provider"] = "deep-data-worker"
        return params


class Settings(BaseSettings):
    """Settings loaded from process environment or the local ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str = ""
    openai_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    openai_model: str = "qwen-plus"
    openai_streaming: bool = False
    openai_timeout_seconds: float = 120.0

    tavily_api_key: str = ""
    tavily_project: str | None = None

    open_sandbox_domain: str = ""
    open_sandbox_api_key: str = ""
    open_sandbox_protocol: Literal["http", "https"] = "http"
    open_sandbox_use_server_proxy: bool = True
    open_sandbox_image: str = "python:3.13-slim"
    open_sandbox_timeout_seconds: int = Field(default=1800, ge=60, le=86400)

    app_env: Literal["development", "production"] = "development"
    local_dev_user_id: str = "local-user"
    mysql_uri: str = ""
    auth_session_days: int = Field(default=7, ge=1, le=90)
    mongodb_uri: str = ""
    mongodb_database: str = "deep_data_research_agent"
    mongodb_skill_collection: str = "skill_files"

    artifact_root: Path = Path("data/users")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide immutable settings object."""

    return Settings()


def create_chat_model(
    *,
    worker: bool = False,
) -> ChatOpenAI:
    """Create the MVP's OpenAI-compatible chat model.

    The placeholder key allows graph imports and static tests before ``.env`` is
    configured. The upstream API still rejects real requests until a valid key
    is provided.
    """

    settings = get_settings()
    if worker:
        model_class = _WorkerChatOpenAI
    else:
        model_class = ChatOpenAI
    return model_class(
        model=settings.openai_model,
        api_key=settings.openai_api_key or "not-configured",
        base_url=settings.openai_base_url,
        temperature=0,
        timeout=settings.openai_timeout_seconds,
        max_retries=2,
        streaming=False if worker else settings.openai_streaming,
    )
