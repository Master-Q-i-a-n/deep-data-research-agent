"""Periodic recovery tasks for durable MongoDB and PostgreSQL outboxes."""

from __future__ import annotations

import asyncio
import logging

from deep_data_research_agent.database import repository as database
from deep_data_research_agent.memory.service import MEMORY_QUEUE
from deep_data_research_agent.workers.app import (
    celery_app,
    publish_email_delivery,
    publish_memory_job,
)

logger = logging.getLogger(__name__)


async def _recover_memory_jobs() -> list[str]:
    try:
        await MEMORY_QUEUE.ensure_indexes()
        return await MEMORY_QUEUE.recoverable_job_ids()
    finally:
        await MEMORY_QUEUE.close()


@celery_app.task(name="ddra.maintenance.recover_memory", ignore_result=True)
def recover_memory_jobs() -> dict[str, int]:
    """Republish MongoDB jobs whose broker message was lost or expired."""

    job_ids = asyncio.run(_recover_memory_jobs())
    published = 0
    for job_id in job_ids:
        try:
            publish_memory_job(job_id)
            published += 1
        except Exception:
            logger.exception("恢复发布记忆任务失败：%s", job_id)
    return {"published": published}


async def _recover_email_deliveries() -> list[str]:
    try:
        return await database.recover_email_deliveries()
    finally:
        await database.close_database()


@celery_app.task(name="ddra.maintenance.recover_mail", ignore_result=True)
def recover_email_deliveries() -> dict[str, int]:
    """Republish safe deliveries and terminalize stale SMTP submissions."""

    delivery_ids = asyncio.run(_recover_email_deliveries())
    published = 0
    for delivery_id in delivery_ids:
        try:
            publish_email_delivery(delivery_id)
            published += 1
        except Exception:
            logger.exception("恢复发布邮件任务失败：%s", delivery_id)
    return {"published": published}


__all__ = ["recover_email_deliveries", "recover_memory_jobs"]
