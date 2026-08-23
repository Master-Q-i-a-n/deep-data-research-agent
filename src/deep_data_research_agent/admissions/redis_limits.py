"""Redis-backed sliding windows and run-admission permits."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from deep_data_research_agent.core.config import get_settings
from deep_data_research_agent.infrastructure.redis.client import (
    close_redis as close_shared_redis,
)
from deep_data_research_agent.infrastructure.redis.client import (
    get_redis,
)
from deep_data_research_agent.infrastructure.redis.client import (
    initialize_redis as initialize_shared_redis,
)
from deep_data_research_agent.infrastructure.redis.keys import (
    KEY_PREFIX,
    digest_key,
    key_secret,
)

_KEY_PREFIX = KEY_PREFIX


_SLIDING_WINDOW_SCRIPT = r"""
local now_parts = redis.call('TIME')
local now_ms = (tonumber(now_parts[1]) * 1000) + math.floor(tonumber(now_parts[2]) / 1000)
local window_ms = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local request_id = ARGV[3]
local cutoff = now_ms - window_ms

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
local existing = redis.call('ZSCORE', KEYS[1], request_id)
local count = redis.call('ZCARD', KEYS[1])
if existing then
  return {1, count, 0, 1}
end

if count >= limit then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local retry_ms = 1
  if #oldest >= 2 then
    retry_ms = math.max(1, tonumber(oldest[2]) + window_ms - now_ms)
  end
  return {0, count, math.ceil(retry_ms / 1000), 0}
end

redis.call('ZADD', KEYS[1], now_ms, request_id)
redis.call('PEXPIRE', KEYS[1], window_ms + 1000)
return {1, count + 1, 0, 0}
"""


_RUN_ADMISSION_SCRIPT = r"""
local now_parts = redis.call('TIME')
local now_ms = (tonumber(now_parts[1]) * 1000) + math.floor(tonumber(now_parts[2]) / 1000)
local question_limit = tonumber(ARGV[1])
local question_window_ms = tonumber(ARGV[2])
local thread_limit = tonumber(ARGV[3])
local reservation_ttl_ms = tonumber(ARGV[4])
local permit_ttl_ms = tonumber(ARGV[5])
local submission_id = ARGV[6]
local requested_member = ARGV[7]
local permit_target = ARGV[8]
local busy_count = tonumber(ARGV[9])
local token_capacity = tonumber(ARGV[10])
local token_refill = tonumber(ARGV[11])

if redis.call('GET', KEYS[4]) then
  return {3, 0, 0, 0}
end

local existing_permit = redis.call('GET', KEYS[3])
if existing_permit then
  if existing_permit ~= permit_target then
    return {4, 0, 0, 0}
  end
  return {0, redis.call('ZCARD', KEYS[1]), 0, math.max(1, math.ceil(redis.call('PTTL', KEYS[3]) / 1000)), tonumber(redis.call('HGET', KEYS[5], 'balance') or '0')}
end

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms - question_window_ms)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now_ms)
if redis.call('ZSCORE', KEYS[1], submission_id) then
  return {3, redis.call('ZCARD', KEYS[1]), 0, 0}
end

local active = {}
local active_ids = {}
local function add_active(member)
  if not active[member] then
    active[member] = true
    active_ids[#active_ids + 1] = member
  end
end

local reservations = redis.call('ZRANGE', KEYS[2], 0, -1)
for _, member in ipairs(reservations) do
  add_active(member)
end
for index = 1, busy_count do
  add_active(ARGV[11 + index])
end

if not active[requested_member] and #active_ids >= thread_limit then
  local response = {2, #active_ids, 0, 0}
  for _, member in ipairs(active_ids) do
    if string.sub(member, 1, 7) == 'thread:' then
      response[#response + 1] = member
    end
  end
  return response
end

local token_state = redis.call('HMGET', KEYS[5], 'balance', 'last_refill_hour', 'version')
if not token_state[1] or not token_state[2] or not token_state[3] then
  return {6, 0, 0, 0}
end
local balance = tonumber(token_state[1])
local last_refill_hour = tonumber(token_state[2])
local current_hour = math.floor(now_ms / 3600000)
local elapsed_hours = math.max(0, current_hour - last_refill_hour)
if elapsed_hours > 0 then
  balance = math.min(token_capacity, balance + (elapsed_hours * token_refill))
  redis.call('HSET', KEYS[5], 'balance', balance, 'last_refill_hour', current_hour)
end
if balance <= 0 then
  local hours_needed = math.floor((-balance) / token_refill) + 1
  local next_refill_ms = (current_hour + hours_needed) * 3600000
  local retry_seconds = math.max(1, math.ceil((next_refill_ms - now_ms) / 1000))
  return {5, 0, retry_seconds, 0, balance, math.floor(next_refill_ms / 1000)}
end

local question_count = redis.call('ZCARD', KEYS[1])
if question_count >= question_limit then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local retry_ms = 1
  if #oldest >= 2 then
    retry_ms = math.max(1, tonumber(oldest[2]) + question_window_ms - now_ms)
  end
  return {1, question_count, math.ceil(retry_ms / 1000), 0}
end

redis.call('ZADD', KEYS[1], now_ms, submission_id)
redis.call('PEXPIRE', KEYS[1], question_window_ms + 1000)
redis.call('ZADD', KEYS[2], now_ms + reservation_ttl_ms, requested_member)
redis.call('PEXPIRE', KEYS[2], reservation_ttl_ms + 1000)
redis.call('SET', KEYS[3], permit_target, 'PX', permit_ttl_ms, 'NX')
return {0, question_count + 1, 0, math.ceil(permit_ttl_ms / 1000), balance}
"""


_SYNC_TOKEN_BUCKET_SCRIPT = r"""
local existing_version = tonumber(redis.call('HGET', KEYS[1], 'version') or '-1')
local incoming_version = tonumber(ARGV[3])
if existing_version > incoming_version then
  return {0, redis.call('HGET', KEYS[1], 'balance'), existing_version}
end
redis.call('HSET', KEYS[1],
  'balance', ARGV[1],
  'last_refill_hour', ARGV[2],
  'version', ARGV[3])
return {1, ARGV[1], incoming_version}
"""


_CONSUME_PERMIT_SCRIPT = r"""
local target = redis.call('GET', KEYS[1])
if not target then
  if redis.call('GET', KEYS[2]) then
    return {2}
  end
  return {1}
end
if target ~= '*' and target ~= ARGV[1] then
  return {3}
end

local now_parts = redis.call('TIME')
local now_ms = (tonumber(now_parts[1]) * 1000) + math.floor(tonumber(now_parts[2]) / 1000)
local reservation_ttl_ms = tonumber(ARGV[3])
redis.call('GETDEL', KEYS[1])
redis.call('SET', KEYS[2], '1', 'PX', ARGV[4])
redis.call('ZREM', KEYS[3], 'submission:' .. ARGV[2])
redis.call('ZADD', KEYS[3], now_ms + reservation_ttl_ms, 'thread:' .. ARGV[1])
redis.call('PEXPIRE', KEYS[3], reservation_ttl_ms + 1000)
return {0}
"""


_RELEASE_LOCK_SCRIPT = r"""
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    count: int
    limit: int
    retry_after_seconds: int
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class RunAdmissionDecision:
    code: str
    allowed: bool
    count: int = 0
    retry_after_seconds: int = 0
    permit_expires_in_seconds: int = 0
    active_thread_ids: tuple[str, ...] = ()
    token_balance: int | None = None
    next_refill_epoch_seconds: int | None = None


class AdmissionLockUnavailable(RuntimeError):
    """Raised when another admission request holds the per-user lock too long."""


_sliding_window_script: AsyncScript | None = None
_run_admission_script: AsyncScript | None = None
_consume_permit_script: AsyncScript | None = None
_release_lock_script: AsyncScript | None = None
_sync_token_bucket_script: AsyncScript | None = None
_init_lock = asyncio.Lock()


def _secret() -> bytes:
    return key_secret()


def _digest(scope: str, raw_key: str) -> str:
    return digest_key(scope, raw_key)


async def initialize_redis() -> Redis:
    """Initialize shared Redis and register admission-specific scripts."""

    global _sliding_window_script, _run_admission_script
    global _consume_permit_script, _release_lock_script, _sync_token_bucket_script
    client = await initialize_shared_redis()
    if _sliding_window_script is not None:
        return client
    async with _init_lock:
        if _sliding_window_script is not None:
            return client
        _sliding_window_script = client.register_script(_SLIDING_WINDOW_SCRIPT)
        _run_admission_script = client.register_script(_RUN_ADMISSION_SCRIPT)
        _consume_permit_script = client.register_script(_CONSUME_PERMIT_SCRIPT)
        _release_lock_script = client.register_script(_RELEASE_LOCK_SCRIPT)
        _sync_token_bucket_script = client.register_script(_SYNC_TOKEN_BUCKET_SCRIPT)
        return client


async def close_redis() -> None:
    global _sliding_window_script, _run_admission_script
    global _consume_permit_script, _release_lock_script, _sync_token_bucket_script
    await close_shared_redis()
    _sliding_window_script = None
    _run_admission_script = None
    _consume_permit_script = None
    _release_lock_script = None
    _sync_token_bucket_script = None


def _require_client() -> Redis:
    return get_redis()


async def consume_sliding_window(
    scope: str,
    raw_key: str,
    *,
    limit: int,
    window_seconds: int,
    request_id: str,
) -> RateLimitDecision:
    if not scope or not raw_key or not request_id:
        raise ValueError("限流作用域、键和请求 ID 不能为空")
    if limit < 1 or window_seconds < 1:
        raise ValueError("限流次数和窗口必须为正数")
    if _sliding_window_script is None:
        raise RuntimeError("Redis 尚未初始化")
    key = f"{_KEY_PREFIX}:rl:{scope}:{_digest(scope, raw_key)}"
    raw = await _sliding_window_script(
        keys=[key],
        args=[window_seconds * 1000, limit, request_id],
        client=_require_client(),
    )
    return RateLimitDecision(
        allowed=bool(int(raw[0])),
        count=int(raw[1]),
        limit=limit,
        retry_after_seconds=int(raw[2]),
        duplicate=bool(int(raw[3])),
    )


def _user_tag(user_id: str) -> str:
    return _digest("user", user_id)


async def sync_token_bucket(
    user_id: str,
    *,
    balance_tokens: int,
    last_refill_hour: int,
    version: int,
) -> int:
    """Copy a PostgreSQL snapshot to Redis without allowing stale overwrites."""

    if _sync_token_bucket_script is None:
        raise RuntimeError("Redis 尚未初始化")
    key = f"{_KEY_PREFIX}:{{{_user_tag(user_id)}}}:token-bucket"
    raw = await _sync_token_bucket_script(
        keys=[key],
        args=[balance_tokens, last_refill_hour, version],
        client=_require_client(),
    )
    return int(raw[1])


@asynccontextmanager
async def admission_lock(user_id: str) -> AsyncIterator[None]:
    """Serialize active-thread snapshots and Redis reservations per user."""

    client = _require_client()
    if _release_lock_script is None:
        raise RuntimeError("Redis 尚未初始化")
    settings = get_settings()
    key = f"{_KEY_PREFIX}:{{{_user_tag(user_id)}}}:admission-lock"
    token = secrets.token_hex(16)
    acquired = False
    for attempt in range(8):
        acquired = bool(
            await client.set(
                key,
                token,
                nx=True,
                px=settings.run_admission_lock_seconds * 1000,
            )
        )
        if acquired:
            break
        await asyncio.sleep(0.025 * (attempt + 1))
    if not acquired:
        raise AdmissionLockUnavailable("用户准入检查正忙")
    try:
        yield
    finally:
        await _release_lock_script(keys=[key], args=[token], client=client)


async def admit_run(
    user_id: str,
    submission_id: str,
    thread_id: str | None,
    active_thread_ids: list[str],
) -> RunAdmissionDecision:
    """Atomically consume question quota and reserve one user-visible thread."""

    if _run_admission_script is None:
        raise RuntimeError("Redis 尚未初始化")
    settings = get_settings()
    tag = _user_tag(user_id)
    base = f"{_KEY_PREFIX}:{{{tag}}}"
    requested_member = f"thread:{thread_id}" if thread_id else f"submission:{submission_id}"
    permit_target = thread_id or "*"
    busy_members = [f"thread:{value}" for value in dict.fromkeys(active_thread_ids)]
    raw = await _run_admission_script(
        keys=[
            f"{base}:questions",
            f"{base}:reservations",
            f"{base}:permit:{submission_id}",
            f"{base}:used:{submission_id}",
            f"{base}:token-bucket",
        ],
        args=[
            settings.question_limit,
            settings.question_window_seconds * 1000,
            settings.thread_concurrency_limit,
            settings.run_reservation_ttl_seconds * 1000,
            settings.run_permit_ttl_seconds * 1000,
            submission_id,
            requested_member,
            permit_target,
            len(busy_members),
            settings.token_bucket_capacity,
            settings.token_bucket_refill_per_hour,
            *busy_members,
        ],
        client=_require_client(),
    )
    result_code = int(raw[0])
    if result_code == 0:
        return RunAdmissionDecision(
            code="RUN_ADMITTED",
            allowed=True,
            count=int(raw[1]),
            permit_expires_in_seconds=int(raw[3]),
            token_balance=int(raw[4]),
        )
    if result_code == 1:
        return RunAdmissionDecision(
            code="QUESTION_RATE_LIMITED",
            allowed=False,
            count=int(raw[1]),
            retry_after_seconds=max(1, int(raw[2])),
        )
    if result_code == 2:
        active = tuple(str(value).removeprefix("thread:") for value in raw[4:])
        return RunAdmissionDecision(
            code="THREAD_CONCURRENCY_LIMIT",
            allowed=False,
            count=int(raw[1]),
            active_thread_ids=active,
        )
    if result_code == 3:
        return RunAdmissionDecision(code="RUN_ADMISSION_ALREADY_USED", allowed=False)
    if result_code == 5:
        return RunAdmissionDecision(
            code="TOKEN_BUDGET_EXHAUSTED",
            allowed=False,
            retry_after_seconds=max(1, int(raw[2])),
            token_balance=int(raw[4]),
            next_refill_epoch_seconds=int(raw[5]),
        )
    if result_code == 6:
        return RunAdmissionDecision(code="TOKEN_BUCKET_UNAVAILABLE", allowed=False)
    return RunAdmissionDecision(code="RUN_ADMISSION_MISMATCH", allowed=False)


async def consume_run_permit(user_id: str, submission_id: str, thread_id: str) -> str:
    """Consume one top-level run permit and return a stable result code."""

    if _consume_permit_script is None:
        raise RuntimeError("Redis 尚未初始化")
    settings = get_settings()
    tag = _user_tag(user_id)
    base = f"{_KEY_PREFIX}:{{{tag}}}"
    raw = await _consume_permit_script(
        keys=[
            f"{base}:permit:{submission_id}",
            f"{base}:used:{submission_id}",
            f"{base}:reservations",
        ],
        args=[
            thread_id,
            submission_id,
            settings.run_reservation_ttl_seconds * 1000,
            max(settings.question_window_seconds, settings.run_permit_ttl_seconds) * 1000,
        ],
        client=_require_client(),
    )
    return {
        0: "CONSUMED",
        1: "MISSING_OR_EXPIRED",
        2: "ALREADY_USED",
        3: "THREAD_MISMATCH",
    }.get(int(raw[0]), "INVALID")


def issue_internal_run_marker(
    *,
    user_id: str,
    graph_id: str,
    parent_thread_id: str,
    token_budget_session_id: str = "",
    ttl_seconds: int = 60,
) -> dict[str, Any]:
    """Create a short-lived, single-use marker for a server-launched child run."""

    expires_at = int(time.time()) + ttl_seconds
    nonce = secrets.token_hex(16)
    payload = f"internal-run\0{user_id}\0{graph_id}\0{parent_thread_id}\0{token_budget_session_id}\0{expires_at}\0{nonce}"
    signature = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "graph_id": graph_id,
        "parent_thread_id": parent_thread_id,
        "token_budget_session_id": token_budget_session_id,
        "expires_at": expires_at,
        "nonce": nonce,
        "signature": signature,
    }


async def consume_internal_run_marker(
    marker: Mapping[str, Any],
    *,
    user_id: str,
    graph_id: str,
) -> bool:
    """Verify and consume a child-run marker so copied metadata cannot be replayed."""

    try:
        marker_graph = str(marker["graph_id"])
        parent_thread_id = str(marker["parent_thread_id"])
        token_budget_session_id = str(marker.get("token_budget_session_id") or "")
        expires_at = int(marker["expires_at"])
        nonce = str(marker["nonce"])
        signature = str(marker["signature"])
    except (KeyError, TypeError, ValueError):
        return False
    now = int(time.time())
    if marker_graph != graph_id or expires_at <= now or expires_at > now + 120:
        return False
    payload = f"internal-run\0{user_id}\0{graph_id}\0{parent_thread_id}\0{token_budget_session_id}\0{expires_at}\0{nonce}"
    expected = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False
    key = f"{_KEY_PREFIX}:internal-used:{_user_tag(user_id)}:{nonce}"
    return bool(
        await _require_client().set(
            key,
            "1",
            nx=True,
            px=max(1000, (expires_at - now) * 1000),
        )
    )
