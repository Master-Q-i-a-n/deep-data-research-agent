"""Owner-safe Redis distributed lock with an automatically renewed lease."""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from redis.exceptions import RedisError

from deep_data_research_agent.infrastructure.redis.client import get_redis

logger = logging.getLogger(__name__)

_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class DistributedLockUnavailable(RuntimeError):
    """Raised when coordination cannot be safely established."""


class DistributedLockLost(RuntimeError):
    """Raised when an operation no longer owns its distributed lease."""


@dataclass(slots=True)
class LockLease:
    key: str
    token: str
    lease_ms: int
    _lost: asyncio.Event = field(default_factory=asyncio.Event)

    def mark_lost(self) -> None:
        self._lost.set()

    def ensure_owned(self) -> None:
        if self._lost.is_set():
            raise DistributedLockLost("Redis 分布式锁租约已丢失")


async def _renew(lease: LockLease, interval_seconds: float) -> None:
    client = get_redis()
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            renewed = await client.eval(
                _RENEW_SCRIPT,
                1,
                lease.key,
                lease.token,
                lease.lease_ms,
            )
            if int(renewed) != 1:
                lease.mark_lost()
                return
    except asyncio.CancelledError:
        raise
    except (RedisError, OSError, RuntimeError):
        lease.mark_lost()
        logger.warning("Redis 分布式锁续租失败", exc_info=True)


@asynccontextmanager
async def distributed_lock(
    key: str,
    *,
    wait_seconds: float,
    lease_seconds: float,
    renew_seconds: float,
) -> AsyncIterator[LockLease]:
    """Acquire one owner-token lock and keep its TTL alive while in scope."""

    if renew_seconds <= 0 or lease_seconds <= renew_seconds * 2:
        raise ValueError("锁租约必须大于两倍续租间隔")
    client = get_redis()
    token = secrets.token_hex(16)
    lease_ms = int(lease_seconds * 1000)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            acquired = await client.set(key, token, nx=True, px=lease_ms)
        except (RedisError, OSError) as exc:
            raise DistributedLockUnavailable("Redis 分布式锁服务不可用") from exc
        if acquired:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DistributedLockUnavailable("等待 Redis 分布式锁超时")
        await asyncio.sleep(min(remaining, 0.05 + random.random() * 0.05))

    lease = LockLease(key=key, token=token, lease_ms=lease_ms)
    renewal = asyncio.create_task(_renew(lease, renew_seconds))
    try:
        yield lease
        lease.ensure_owned()
    finally:
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        try:
            await client.eval(_RELEASE_SCRIPT, 1, key, token)
        except (RedisError, OSError):
            # The TTL remains the final safety net; never mask the operation's
            # original exception with a best-effort release failure.
            logger.warning("Redis 分布式锁释放失败", exc_info=True)


__all__ = [
    "DistributedLockLost",
    "DistributedLockUnavailable",
    "LockLease",
    "distributed_lock",
]
