"""RabbitMQ task topology, dead-letter destinations and publishing helpers."""

from __future__ import annotations

import asyncio
import sys

from celery import Celery
from celery.signals import before_task_publish, worker_process_shutdown
from kombu import Exchange, Queue

from deep_data_research_agent.core.config import get_settings

if sys.platform == "win32":
    # psycopg's async connection requires the selector policy on Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


_settings = get_settings()
_task_exchange = Exchange("ddra", type="direct", durable=True)
_dead_exchange = Exchange("ddra.dead", type="direct", durable=True)
_queue_names = ("memory", "mail", "maintenance")
_task_queues = tuple(
    Queue(
        name,
        exchange=_task_exchange,
        routing_key=name,
        durable=True,
        queue_arguments={
            "x-queue-type": "classic",
            "x-dead-letter-exchange": _dead_exchange.name,
            "x-dead-letter-routing-key": f"{name}.dead",
        },
    )
    for name in _queue_names
)
# Dead queues are declared separately, so ordinary workers never consume them.
_dead_queues = tuple(
    Queue(
        f"{name}.dead",
        exchange=_dead_exchange,
        routing_key=f"{name}.dead",
        durable=True,
        queue_arguments={"x-queue-type": "classic"},
    )
    for name in _queue_names
)

celery_app = Celery(
    "deep-data-research-agent",
    broker=_settings.celery_broker_url,
    include=[
        "deep_data_research_agent.workers.tasks.memory",
        "deep_data_research_agent.workers.tasks.email",
        "deep_data_research_agent.workers.tasks.maintenance",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_ignore_result=True,
    task_default_queue="maintenance",
    task_default_exchange="ddra",
    task_default_exchange_type="direct",
    task_default_routing_key="maintenance",
    task_default_delivery_mode="persistent",
    task_queues=_task_queues,
    task_create_missing_queues=False,
    # Late-acked unhandled failures are rejected without requeue and go to DLX.
    # Domain-level safe retries continue to use self.retry() explicitly.
    task_acks_on_failure_or_timeout=False,
    timezone="Asia/Shanghai",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    worker_enable_remote_control=False,
    task_send_sent_event=False,
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=5,
    broker_transport_options={"confirm_publish": True},
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


@before_task_publish.connect
def _declare_dead_queues(sender=None, declare=None, **_kwargs) -> None:
    """Declare dead-letter destinations before a ddra task can be delivered."""

    if sender and sender.startswith("ddra.") and declare is not None:
        # Kombu caches declarations per connection, avoiding repeated broker calls.
        declare.extend(_dead_queues)


def declare_task_queues() -> None:
    """Check AMQP credentials/vhost and provision the project's six queues."""

    with celery_app.connection_for_write() as connection:
        connection.ensure_connection(max_retries=0)
        for queue in (*_dead_queues, *_task_queues):
            queue(connection.default_channel).declare()


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
    "declare_task_queues",
    "publish_email_delivery",
    "publish_memory_job",
]
