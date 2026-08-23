"""Small process-scoped MongoDB client used only by readiness probes."""

from __future__ import annotations

import asyncio

from pymongo import AsyncMongoClient

from deep_data_research_agent.core.config import get_settings

_client: AsyncMongoClient | None = None
_init_lock = asyncio.Lock()


async def initialize_mongodb_health_client() -> AsyncMongoClient:
    global _client
    if _client is not None:
        return _client
    async with _init_lock:
        if _client is None:
            settings = get_settings()
            if not settings.mongodb_uri.strip():
                raise RuntimeError("MONGODB_URI 未配置")
            timeout_ms = max(1, int(settings.health_check_timeout_seconds * 1000))
            _client = AsyncMongoClient(
                settings.mongodb_uri,
                connectTimeoutMS=timeout_ms,
                serverSelectionTimeoutMS=timeout_ms,
            )
    return _client


async def ping_mongodb() -> None:
    client = await initialize_mongodb_health_client()
    await client.admin.command("ping")


async def close_mongodb_health_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
    _client = None


__all__ = [
    "close_mongodb_health_client",
    "initialize_mongodb_health_client",
    "ping_mongodb",
]
