"""Dependency-safe liveness and readiness checks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from deep_data_research_agent.core.config import get_settings
from deep_data_research_agent.database import repository as database
from deep_data_research_agent.infrastructure.mongodb.health import ping_mongodb
from deep_data_research_agent.infrastructure.redis.client import ping_redis
from deep_data_research_agent.infrastructure.sandbox import manager as sandbox_manager
from deep_data_research_agent.providers.service import check_encryption_ready

Check = Callable[[], Awaitable[None]]


async def _run_check(check: Check, timeout_seconds: float) -> str:
    try:
        async with asyncio.timeout(timeout_seconds):
            await check()
        return "ok"
    except TimeoutError:
        return "timeout"
    except Exception:  # noqa: BLE001 - probes expose categories, never secrets.
        return "error"


async def readiness_checks() -> dict[str, str]:
    """Run independent core dependency checks concurrently."""

    timeout = get_settings().health_check_timeout_seconds
    async def provider_encryption() -> None:
        await asyncio.to_thread(check_encryption_ready)

    names = (
        "postgres",
        "redis",
        "mongodb",
        "workspace_storage",
        "provider_encryption",
    )
    results = await asyncio.gather(
        _run_check(database.check_database_ready, timeout),
        _run_check(ping_redis, timeout),
        _run_check(ping_mongodb, timeout),
        _run_check(
            sandbox_manager.SANDBOX_MANAGER.workspace_store.check_ready,
            timeout,
        ),
        _run_check(provider_encryption, timeout),
    )
    return dict(zip(names, results, strict=True))


__all__ = ["readiness_checks"]
