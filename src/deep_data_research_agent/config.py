"""Application settings and OpenAI-compatible model construction."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from langchain_openai import ChatOpenAI
from pydantic import Field, SecretStr, model_validator
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

    # PostgreSQL credentials remain inside the standalone MCP container.  The
    # Agent process only needs the local SSE endpoint and result-size limits.
    postgres_mcp_enabled: bool = False
    postgres_mcp_url: str = "http://127.0.0.1:8000/sse"
    postgres_mcp_connect_timeout_seconds: float = Field(default=5.0, ge=1, le=30)
    postgres_mcp_tool_timeout_seconds: float = Field(default=30.0, ge=5, le=120)
    postgres_mcp_preview_rows: int = Field(default=200, ge=1, le=1000)
    postgres_mcp_export_rows: int = Field(default=50_000, ge=1, le=100_000)
    postgres_mcp_export_bytes: int = Field(
        default=20 * 1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
    )

    open_sandbox_domain: str = ""
    open_sandbox_api_key: str = ""
    open_sandbox_protocol: Literal["http", "https"] = "http"
    open_sandbox_use_server_proxy: bool = True
    open_sandbox_image: str = "python:3.13-slim"
    open_sandbox_timeout_seconds: int = Field(default=1800, ge=60, le=86400)

    app_env: Literal["development", "production"] = "development"
    local_dev_user_id: str = "local-user"
    postgres_uri: str = ""
    postgres_app_pool_size: int = Field(default=5, ge=1, le=50)
    postgres_app_max_overflow: int = Field(default=10, ge=0, le=100)
    postgres_checkpoint_pool_min_size: int = Field(default=1, ge=1, le=20)
    postgres_checkpoint_pool_max_size: int = Field(default=5, ge=1, le=50)
    postgres_pool_timeout_seconds: float = Field(default=30.0, ge=1, le=120)
    auth_session_days: int = Field(default=7, ge=1, le=90)
    rate_limit_key_secret: SecretStr = SecretStr("")
    auth_login_failure_limit: int = Field(default=5, ge=1, le=100)
    auth_login_window_seconds: int = Field(default=900, ge=1, le=86400)
    auth_register_limit: int = Field(default=3, ge=1, le=100)
    auth_register_window_seconds: int = Field(default=3600, ge=1, le=86400)
    agent_run_limit: int = Field(default=10, ge=1, le=1000)
    agent_run_window_seconds: int = Field(default=60, ge=1, le=86400)
    mongodb_uri: str = ""
    mongodb_database: str = "deep_data_research_agent"
    mongodb_skill_collection: str = "skill_files"
    mongodb_memory_collection: str = "memories"
    mongodb_memory_job_collection: str = "memory_update_jobs"

    # Fixed platform mailbox used by the Supervisor's approval-gated report tool.
    smtp_enabled: bool = False
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_use_ssl: bool = True
    smtp_sender_name: str = "深研"
    smtp_timeout_seconds: float = Field(default=30.0, ge=1, le=120)
    smtp_max_attachment_bytes: int = Field(
        default=20 * 1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
    )

    # User-memory capture and automatic failure review enqueue work for the
    # lifespan-managed background consolidator.
    memory_model: str | None = None
    memory_consolidation_timeout_seconds: float = Field(default=30.0, ge=5, le=120)
    failure_review_snapshot_max_bytes: int = Field(
        default=4 * 1024 * 1024,
        ge=1024,
        le=8 * 1024 * 1024,
    )
    failure_review_delay_seconds: float = Field(default=1.0, ge=0, le=300)
    failure_review_payload_ttl_hours: int = Field(default=24, ge=1, le=168)

    artifact_root: Path = Path("data/users")

    @model_validator(mode="after")
    def validate_production_security(self) -> Settings:
        """Require a stable keyed hash secret for distributed production limits."""

        secret = self.rate_limit_key_secret.get_secret_value()
        if self.app_env == "production" and len(secret) < 32:
            raise ValueError("生产环境 RATE_LIMIT_KEY_SECRET 至少需要 32 个字符")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide immutable settings object."""

    return Settings()


@lru_cache(maxsize=2)
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


@lru_cache(maxsize=1)
def create_memory_model() -> ChatOpenAI:
    """Create the non-streaming background consolidation model."""

    settings = get_settings()
    return ChatOpenAI(
        model=settings.memory_model or settings.openai_model,
        api_key=settings.openai_api_key or "not-configured",
        base_url=settings.openai_base_url,
        temperature=0,
        timeout=settings.memory_consolidation_timeout_seconds,
        # Consolidation jobs have their own durable retry policy.
        max_retries=0,
        streaming=False,
    )


@lru_cache(maxsize=8)
def create_failure_review_model(
    model_name: str | None = None,
    *,
    worker: bool = False,
) -> ChatOpenAI:
    """Reuse the corresponding business model client for failure review.

    Sharing the model object also shares its AsyncOpenAI connection pool, which
    lets this diagnostic path test whether DeepSeek cache locality is tied to
    the original client. ``ainvoke`` still aggregates one private AIMessage.
    """

    settings = get_settings()
    selected_model = model_name or settings.openai_model
    if selected_model == settings.openai_model:
        # Preserve the exact cache key used when each graph is constructed.
        return create_chat_model(worker=True) if worker else create_chat_model()
    return ChatOpenAI(
        model=selected_model,
        api_key=settings.openai_api_key or "not-configured",
        base_url=settings.openai_base_url,
        temperature=0,
        timeout=settings.memory_consolidation_timeout_seconds,
        # The durable MongoDB queue owns retry and backoff behavior.
        max_retries=0,
        streaming=False if worker else settings.openai_streaming,
    )
