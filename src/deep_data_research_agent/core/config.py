"""Application settings and OpenAI-compatible model construction."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _WorkerChatOpenAI(ChatOpenAI):
    """Select the worker-specific DeepAgents harness without changing API calls."""

    def _get_ls_params(self, *args, **kwargs):
        params = super()._get_ls_params(*args, **kwargs)
        params["ls_provider"] = "deep-data-worker"
        return params


class _ReviewerChatOpenAI(_WorkerChatOpenAI):
    """Select a read-only harness profile for the analysis reviewer."""

    def _get_ls_params(self, *args, **kwargs):
        params = super()._get_ls_params(*args, **kwargs)
        params["ls_provider"] = "deep-data-reviewer"
        return params


class _ThinkingChatDeepSeek(ChatDeepSeek):
    """Preserve DeepSeek thinking context across multi-turn tool calls."""

    _ls_provider_name: ClassVar[str] = "deep-data-worker"

    def _get_ls_params(self, *args, **kwargs):
        params = super()._get_ls_params(*args, **kwargs)
        params["ls_provider"] = self._ls_provider_name
        return params

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        payload_messages = payload.get("messages", [])
        source_messages = self._convert_input(input_).to_messages()
        # ChatDeepSeek extracts reasoning_content from responses but currently
        # does not place it back into multi-turn chat-completion requests.
        if len(payload_messages) == len(source_messages):
            for source, target in zip(source_messages, payload_messages, strict=True):
                if not isinstance(source, AIMessage) or not isinstance(target, dict):
                    continue
                reasoning = source.additional_kwargs.get("reasoning_content")
                if reasoning is not None:
                    target["reasoning_content"] = reasoning
        return payload


class _ReviewerChatDeepSeek(_ThinkingChatDeepSeek):
    """Select the Reviewer harness while retaining multi-turn thinking."""

    _ls_provider_name: ClassVar[str] = "deep-data-reviewer"


class _SupervisorChatDeepSeek(_ThinkingChatDeepSeek):
    """Use the Supervisor harness label with DeepSeek reasoning support."""

    _ls_provider_name: ClassVar[str] = "openai"


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
    # Online Agent runs use per-user Provider credentials. These operational
    # limits remain deployment-owned and never contain user secrets.
    model_provider_encryption_key_file: Path = Path(".secrets/model_provider_key")
    model_provider_host_allowlist: str = ""
    model_provider_timeout_seconds: float = Field(default=120.0, ge=5, le=300)
    model_provider_test_timeout_seconds: float = Field(default=20.0, ge=3, le=60)
    model_provider_streaming: bool = False
    model_provider_cache_size: int = Field(default=128, ge=1, le=1024)
    model_provider_cache_ttl_seconds: int = Field(default=900, ge=30, le=86400)
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
    sandbox_lock_wait_seconds: float = Field(default=5.0, ge=0.1, le=60)
    sandbox_lock_lease_seconds: float = Field(default=180.0, ge=10, le=600)
    sandbox_lock_renew_seconds: float = Field(default=30.0, ge=1, le=120)
    sandbox_delete_tombstone_seconds: int = Field(default=600, ge=60, le=3600)

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
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_username: str = "ddra"
    redis_password_file: Path = Path(".secrets/redis_password")
    redis_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    redis_socket_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    redis_max_connections: int = Field(default=20, ge=1, le=200)
    health_check_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    # Celery reuses the Redis service but has its own logical DB and ACL user.
    celery_broker_url: str = "redis://127.0.0.1:6379/1"
    celery_redis_username: str = "ddra-celery"
    celery_broker_key_prefix: str = "ddra-celery:"
    celery_visibility_timeout_seconds: int = Field(default=300, ge=60, le=3600)
    auth_login_limit: int = Field(default=10, ge=1, le=100)
    auth_login_window_seconds: int = Field(default=60, ge=1, le=86400)
    auth_register_limit: int = Field(default=3, ge=1, le=100)
    auth_register_window_seconds: int = Field(default=3600, ge=1, le=86400)
    question_limit: int = Field(default=20, ge=1, le=1000)
    question_window_seconds: int = Field(default=60, ge=1, le=86400)
    thread_concurrency_limit: int = Field(default=3, ge=1, le=50)
    run_permit_ttl_seconds: int = Field(default=30, ge=5, le=300)
    run_reservation_ttl_seconds: int = Field(default=15, ge=5, le=120)
    run_admission_lock_seconds: int = Field(default=5, ge=1, le=30)
    token_bucket_capacity: int = Field(default=100_000_000, ge=1)
    token_bucket_refill_per_hour: int = Field(default=10_000_000, ge=1)
    token_reservation_output_tokens: int = Field(default=8_192, ge=1, le=1_000_000)
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
    memory_model_timeout_seconds: float = Field(default=60.0, ge=5, le=300)
    memory_job_timeout_seconds: float = Field(default=75.0, ge=10, le=600)
    failure_review_max_output_tokens: int = Field(default=4096, ge=512, le=8192)
    failure_review_bundle_max_bytes: int = Field(
        default=256 * 1024,
        ge=1024,
        le=2 * 1024 * 1024,
    )
    failure_review_payload_ttl_hours: int = Field(default=24, ge=1, le=168)

    workspace_storage_backend: Literal["local", "oss"] = "local"
    artifact_root: Path = Path("data/users")
    oss_region: str = "cn-beijing"
    oss_endpoint: str = "https://oss-cn-beijing-internal.aliyuncs.com"
    oss_bucket_name: str = ""
    oss_prefix: str = "users"
    oss_ecs_ram_role: str = "DeepAgentsECSRole"

    @model_validator(mode="after")
    def validate_production_security(self) -> Settings:
        """Require a stable keyed hash secret for distributed production limits."""

        secret = self.rate_limit_key_secret.get_secret_value()
        if self.app_env == "production" and len(secret) < 32:
            raise ValueError("生产环境 RATE_LIMIT_KEY_SECRET 至少需要 32 个字符")
        if self.app_env == "production" and not self.redis_username.strip():
            raise ValueError("生产环境 REDIS_USERNAME 不能为空")
        if self.app_env == "production" and not self.celery_redis_username.strip():
            raise ValueError("生产环境 CELERY_REDIS_USERNAME 不能为空")
        if self.app_env == "production" and self.workspace_storage_backend != "oss":
            raise ValueError("生产环境 WORKSPACE_STORAGE_BACKEND 必须为 oss")
        if self.workspace_storage_backend == "oss":
            required_oss = {
                "OSS_REGION": self.oss_region,
                "OSS_ENDPOINT": self.oss_endpoint,
                "OSS_BUCKET_NAME": self.oss_bucket_name,
                "OSS_PREFIX": self.oss_prefix,
                "OSS_ECS_RAM_ROLE": self.oss_ecs_ram_role,
            }
            missing = [name for name, value in required_oss.items() if not value.strip()]
            if missing:
                raise ValueError(f"OSS 工作区存储缺少配置：{'、'.join(missing)}")
        if not self.celery_broker_key_prefix or self.celery_broker_key_prefix.startswith(
            "ddra:"
        ):
            raise ValueError("CELERY_BROKER_KEY_PREFIX 必须与应用 ddra:* 键空间隔离")
        if self.memory_job_timeout_seconds <= self.memory_model_timeout_seconds:
            raise ValueError("MEMORY_JOB_TIMEOUT_SECONDS 必须大于 MEMORY_MODEL_TIMEOUT_SECONDS")
        if self.sandbox_lock_lease_seconds <= self.sandbox_lock_renew_seconds * 2:
            raise ValueError("SANDBOX_LOCK_LEASE_SECONDS 必须大于两倍续租间隔")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide immutable settings object."""

    return Settings()


def create_graph_placeholder_model(
    role: Literal["supervisor", "worker", "reviewer"],
) -> ChatOpenAI:
    """Build an inert graph-import model without deployment credentials.

    Online calls replace it in ``ProviderModelMiddleware``. A concrete model is
    still needed while DeepAgents compiles tool schemas, but it must not retain
    any user's API Key or depend on the legacy ``OPENAI_*`` environment.
    """

    model_class: type[ChatOpenAI]
    if role == "worker":
        model_class = _WorkerChatOpenAI
    elif role == "reviewer":
        model_class = _ReviewerChatOpenAI
    else:
        model_class = ChatOpenAI
    return model_class(
        model="provider-placeholder",
        api_key="not-configured",
        base_url="https://provider.invalid/v1",
        max_retries=0,
    )


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
def create_data_analyst_model() -> BaseChatModel:
    """Create a thinking-capable model for multi-turn data analysis."""

    settings = get_settings()
    if settings.openai_model.startswith("deepseek"):
        extra_body: dict[str, object] | None = None
        if settings.openai_model.startswith("deepseek-v4"):
            extra_body = {"thinking": {"type": "enabled"}}
        return _ThinkingChatDeepSeek(
            model=settings.openai_model,
            api_key=settings.openai_api_key or "not-configured",
            base_url=settings.openai_base_url,
            temperature=0,
            timeout=settings.openai_timeout_seconds,
            max_retries=2,
            streaming=False,
            extra_body=extra_body,
        )
    return _WorkerChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key or "not-configured",
        base_url=settings.openai_base_url,
        temperature=0,
        timeout=settings.openai_timeout_seconds,
        max_retries=2,
        streaming=False,
    )


@lru_cache(maxsize=1)
def create_reviewer_model() -> BaseChatModel:
    """Create a thinking-capable model with Reviewer-only tool filtering."""

    settings = get_settings()
    if settings.openai_model.startswith("deepseek"):
        extra_body: dict[str, object] | None = None
        if settings.openai_model.startswith("deepseek-v4"):
            extra_body = {"thinking": {"type": "enabled"}}
        return _ReviewerChatDeepSeek(
            model=settings.openai_model,
            api_key=settings.openai_api_key or "not-configured",
            base_url=settings.openai_base_url,
            temperature=0,
            timeout=settings.openai_timeout_seconds,
            max_retries=2,
            streaming=False,
            extra_body=extra_body,
        )
    return _ReviewerChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key or "not-configured",
        base_url=settings.openai_base_url,
        temperature=0,
        timeout=settings.openai_timeout_seconds,
        max_retries=2,
        streaming=False,
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
        timeout=settings.memory_model_timeout_seconds,
        # Consolidation jobs have their own durable retry policy.
        max_retries=0,
        streaming=False,
    )
