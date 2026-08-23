"""Trusted user identity helpers for user-scoped persistent resources."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from deep_data_research_agent.core.config import get_settings


def _server_user_identity(runtime: Any) -> str | None:
    """Read the authenticated identity injected by LangGraph Server."""

    server_info = getattr(runtime, "server_info", None)
    user = getattr(server_info, "user", None)
    if user is None:
        return None

    identity = getattr(user, "identity", None)
    if identity is None and isinstance(user, dict):
        identity = user.get("identity")
    value = str(identity or "").strip()
    return value or None


def user_identity_from_config(config: dict[str, Any] | None) -> str:
    """Read Agent Server auth fields from a run configuration."""

    configurable = (config or {}).get("configurable", {})
    identity = configurable.get("langgraph_auth_user_id")
    if not identity:
        user = configurable.get("langgraph_auth_user")
        if isinstance(user, dict):
            identity = user.get("identity")
        else:
            identity = getattr(user, "identity", None)
    value = str(identity or "").strip()
    if value:
        return value

    settings = get_settings()
    if settings.app_env == "development" and settings.local_dev_user_id.strip():
        return settings.local_dev_user_id.strip()
    raise RuntimeError("运行配置未提供经过认证的用户身份")


def user_identity(runtime: Any) -> str:
    """Return a trusted identity, with an explicit local-development fallback."""

    if identity := _server_user_identity(runtime):
        return identity

    runtime_config = getattr(runtime, "config", None)
    if runtime_config:
        return user_identity_from_config(runtime_config)

    settings = get_settings()
    if settings.app_env == "development":
        local_identity = settings.local_dev_user_id.strip()
        if local_identity:
            return local_identity
        raise RuntimeError("LOCAL_DEV_USER_ID 不能为空")

    raise RuntimeError("生产环境未提供经过认证的用户身份，拒绝访问用户级 Skill")


def user_hash(runtime: Any) -> str:
    """Return a stable non-PII namespace component for the current user."""

    return sha256(user_identity(runtime).encode("utf-8")).hexdigest()


def assigned_skill_namespace(runtime: Any, agent_name: str) -> tuple[str, ...]:
    """Build the MongoDB namespace containing one Agent's active Skills."""

    return (user_hash(runtime), "skills", agent_name)
