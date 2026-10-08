"""Celery task that processes durable shared failure-memory jobs."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Iterator
from contextlib import contextmanager

from celery import Task
from celery.exceptions import Reject
from redis import Redis

from deep_data_research_agent.core.config import get_settings
from deep_data_research_agent.database import repository as database
from deep_data_research_agent.infrastructure.redis.client import redis_password
from deep_data_research_agent.infrastructure.redis.keys import KEY_PREFIX
from deep_data_research_agent.memory.service import MEMORY_QUEUE, MemoryJobFailed
from deep_data_research_agent.workers.app import celery_app

logger = logging.getLogger(__name__)
_MEMORY_LOCK_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


@contextmanager
def _memory_writer_lock() -> Iterator[bool]:
    """Serialize writes that rebuild shared failure-memory indexes."""

    settings = get_settings()
    client = Redis.from_url(
        settings.redis_url,
        username=settings.redis_username,
        password=redis_password(settings.redis_password_file),
        decode_responses=True,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
    )
    # The shared-index lock belongs to application Redis, independently of AMQP.
    key = f"{KEY_PREFIX}:lock:memory-writer"
    token = secrets.token_hex(16)
    acquired = False
    try:
        acquired = bool(
            client.set(
                key,
                token,
                nx=True,
                px=int((settings.memory_job_timeout_seconds + 30) * 1000),
            )
        )
        yield acquired
    finally:
        if acquired:
            try:
                client.eval(_MEMORY_LOCK_RELEASE_SCRIPT, 1, key, token)
            except Exception:
                logger.exception("释放记忆写入锁失败")
        client.close()


async def _process_memory_job(job_id: str) -> int | None:
    try:
        await MEMORY_QUEUE.ensure_indexes()
        job = await MEMORY_QUEUE.claim_job(job_id)
        if job is None:
            return None
        return await MEMORY_QUEUE._process_job(job)
    finally:
        await MEMORY_QUEUE.close()
        await database.close_database()


@celery_app.task(
    bind=True,
    name="ddra.memory.process",
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_memory_job(self: Task, job_id: str) -> dict[str, object]:
    """Process one idempotent MongoDB memory job."""

    try:
        with _memory_writer_lock() as acquired:
            if not acquired:
                raise self.retry(countdown=2, max_retries=None)
            retry_after = asyncio.run(_process_memory_job(job_id))
    except MemoryJobFailed as exc:
        # MongoDB already records the terminal failure; preserve this message in DLQ.
        raise Reject(str(exc), requeue=False) from exc
    if retry_after is not None:
        raise self.retry(countdown=retry_after, max_retries=None)
    return {"status": "handled", "job_id": job_id}


__all__ = ["process_memory_job"]
