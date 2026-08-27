"""Celery broker configuration and small task publishing helpers."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from celery import Celery
from celery.signals import worker_process_shutdown

from deep_data_research_agent.core.config import get_settings

if sys.platform == "win32":
    # psycopg's async connection requires the selector policy on Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _redis_password(path: Path) -> str:
    """Read the shared Docker secret without copying it into .env."""

    try:
        password = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"无法读取 Redis 密钥文件：{path}") from exc
    if len(password) < 32:
        raise RuntimeError("Redis 密钥至少需要 32 个字符")
    return password


def celery_broker_url() -> str:
    """Add ACL credentials to the configured non-secret broker URL."""

    settings = get_settings()
    parsed = urlsplit(settings.celery_broker_url)
    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
        raise ValueError("CELERY_BROKER_URL 必须是有效的 Redis URL")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    username = quote(settings.celery_redis_username, safe="")
    password = quote(_redis_password(settings.redis_password_file), safe="")
    return urlunsplit(
        (parsed.scheme, f"{username}:{password}@{host}", parsed.path, parsed.query, "")
    )


celery_app = Celery(
    "deep-data-research-agent",
    broker=celery_broker_url(),
    include=[
        "deep_data_research_agent.workers.tasks.memory",
        "deep_data_research_agent.workers.tasks.email",
        "deep_data_research_agent.workers.tasks.maintenance",
    ],
)

_settings = get_settings()
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_ignore_result=True,
    timezone="Asia/Shanghai",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    worker_enable_remote_control=False,
    task_send_sent_event=False,
    broker_connection_retry_on_startup=True,
    broker_transport_options={
        "global_keyprefix": _settings.celery_broker_key_prefix,
        "visibility_timeout": _settings.celery_visibility_timeout_seconds,
        "socket_connect_timeout": _settings.redis_connect_timeout_seconds,
        "socket_timeout": max(5.0, _settings.redis_socket_timeout_seconds),
        "health_check_interval": 30,
        "max_connections": 20,
    },
    task_routes={
        "ddra.memory.process": {"queue": "memory"},
        "ddra.mail.send": {"queue": "mail"},
        "ddra.maintenance.recover_memory": {"queue": "maintenance"},
        "ddra.maintenance.recover_mail": {"queue": "maintenance"},
    },
    beat_schedule={
        "recover-memory-jobs": {
            "task": "ddra.maintenance.recover_memory",
            "schedule": 30.0,
        },
        "recover-email-deliveries": {
            "task": "ddra.maintenance.recover_mail",
            "schedule": 30.0,
        },
    },
)


@worker_process_shutdown.connect
def _close_workspace_storage(**_kwargs) -> None:
    """Close the per-process asynchronous OSS transport during worker shutdown."""

    from deep_data_research_agent.infrastructure.sandbox import (
        manager as sandbox_manager,
    )

    asyncio.run(sandbox_manager.SANDBOX_MANAGER.workspace_store.close())


def publish_memory_job(job_id: str) -> None:
    """Publish only the durable MongoDB identifier, never its model payload."""

    celery_app.send_task(
        "ddra.memory.process",
        args=[job_id],
        queue="memory",
        task_id=f"memory-{job_id}",
        retry=False,
    )


def publish_email_delivery(delivery_id: str) -> None:
    """Publish only the durable PostgreSQL delivery identifier."""

    celery_app.send_task(
        "ddra.mail.send",
        args=[delivery_id],
        queue="mail",
        task_id=f"mail-{delivery_id}",
        retry=False,
    )


__all__ = [
    "celery_app",
    "celery_broker_url",
    "publish_email_delivery",
    "publish_memory_job",
]
