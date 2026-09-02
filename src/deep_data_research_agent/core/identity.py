"""Trusted user identity helpers for user-scoped persistent resources."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from langgraph.config import get_config


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

    raise RuntimeError("运行配置未提供经过认证的用户身份")


def user_identity(runtime: Any) -> str:
    """Return the authenticated identity supplied by LangGraph Server."""

    if identity := _server_user_identity(runtime):
        return identity

    runtime_config = getattr(runtime, "config", None)
    if runtime_config:
        return user_identity_from_config(runtime_config)

    # Some tool-node paths expose the authenticated RunnableConfig through
    # contextvars instead of attaching it to Runtime.
    try:
        active_config = get_config()
    except RuntimeError:
        active_config = None
    if active_config:
        return user_identity_from_config(active_config)

    raise RuntimeError("未提供经过认证的用户身份，拒绝访问用户级资源")


def user_hash(runtime: Any) -> str:
    """Return a stable non-PII namespace component for the current user."""

    return sha256(user_identity(runtime).encode("utf-8")).hexdigest()


def assigned_skill_namespace(runtime: Any, agent_name: str) -> tuple[str, ...]:
    """Build the MongoDB namespace containing one Agent's active Skills."""

    return (user_hash(runtime), "skills", agent_name)
