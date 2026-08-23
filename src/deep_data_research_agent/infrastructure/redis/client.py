"""Process-scoped asynchronous Redis client shared by API subsystems."""

from __future__ import annotations

import asyncio
from pathlib import Path

from redis.asyncio import BlockingConnectionPool, Redis

from deep_data_research_agent.core.config import get_settings

_client: Redis | None = None
_init_lock = asyncio.Lock()


def redis_password(path: Path) -> str:
    try:
        password = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"无法读取 Redis 密钥文件：{path}") from exc
    if len(password) < 32:
        raise RuntimeError("Redis 密钥至少需要 32 个字符")
    return password


async def initialize_redis() -> Redis:
    """Create and verify the shared asynchronous Redis connection pool."""

    global _client
    if _client is not None:
        return _client
    async with _init_lock:
        if _client is not None:
            return _client
        settings = get_settings()
        pool = BlockingConnectionPool.from_url(
            settings.redis_url,
            username=settings.redis_username,
            password=redis_password(settings.redis_password_file),
            decode_responses=True,
            protocol=2,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_socket_timeout_seconds,
            max_connections=settings.redis_max_connections,
            timeout=settings.redis_socket_timeout_seconds,
            health_check_interval=30,
        )
        client = Redis(connection_pool=pool)
        try:
            await client.ping()
        except Exception:
            await client.aclose(close_connection_pool=True)
            raise
        _client = client
        return client


def get_redis() -> Redis:
    if _client is None:
        raise RuntimeError("Redis 尚未初始化")
    return _client


async def ping_redis() -> None:
    await get_redis().ping()


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose(close_connection_pool=True)
    _client = None


__all__ = ["close_redis", "get_redis", "initialize_redis", "ping_redis", "redis_password"]
