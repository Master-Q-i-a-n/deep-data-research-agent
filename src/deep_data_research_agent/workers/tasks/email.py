"""Celery task that builds attachments and sends durable email deliveries."""

from __future__ import annotations

import asyncio
import logging

from celery import Task

from deep_data_research_agent.artifacts.service import ArtifactError
from deep_data_research_agent.core.config import get_settings
from deep_data_research_agent.database import repository as database
from deep_data_research_agent.infrastructure.sandbox import manager as sandbox_manager
from deep_data_research_agent.tools.interaction import (
    _build_report_email,
    _load_report_attachments,
    _send_smtp_message,
    _SMTPDeliveryError,
)
from deep_data_research_agent.workers.app import celery_app

logger = logging.getLogger(__name__)
_EMAIL_RETRY_DELAYS = (10, 60, 300)


async def _process_email_delivery(delivery_id: str) -> int | None:
    """Build attachments and submit one claimed delivery."""

    try:
        delivery = await database.claim_email_delivery(delivery_id)
    except Exception:
        await database.close_database()
        raise
    if delivery is None:
        await database.close_database()
        return None
    settings = get_settings()
    phase = "attachments"
    try:
        if not delivery.pdf_path or not delivery.markdown_path:
            raise ValueError("邮件投递缺少报告源路径")
        scope = sandbox_manager.SANDBOX_MANAGER.workspace_scope(
            delivery.thread_id,
            "supervisor",
            user_id=delivery.user_id,
        )
        pdf_filename, pdf_content, zip_filename, zip_content = await _load_report_attachments(
            scope,
            delivery.pdf_path,
            delivery.markdown_path,
        )
        if len(pdf_content) + len(zip_content) > settings.smtp_max_attachment_bytes:
            raise ValueError("邮件附件总量超过发送上限，请改用浏览器下载")
        email = _build_report_email(
            settings=settings,
            recipient=delivery.recipient,
            subject=delivery.subject,
            message_id=delivery.message_id,
            pdf_filename=pdf_filename,
            pdf_content=pdf_content,
            zip_filename=zip_filename,
            zip_content=zip_content,
        )

        phase = "submitting"
        await database.mark_email_submitting(
            delivery_id,
            lease_seconds=max(120, int(settings.smtp_timeout_seconds) + 30),
        )
        await asyncio.to_thread(
            _send_smtp_message,
            email,
            settings=settings,
            recipient=delivery.recipient,
        )
    except _SMTPDeliveryError as exc:
        if exc.uncertain:
            await database.finish_email_delivery(
                delivery_id,
                status="uncertain",
                error_summary=str(exc),
            )
            return None
        if exc.retryable:
            delay = _EMAIL_RETRY_DELAYS[
                min(max(delivery.attempts - 1, 0), len(_EMAIL_RETRY_DELAYS) - 1)
            ]
            _record, retry_after = await database.schedule_email_retry(
                delivery_id,
                delay_seconds=delay,
                error_summary=str(exc),
            )
            return retry_after
        await database.finish_email_delivery(
            delivery_id,
            status="failed",
            error_summary=str(exc),
        )
        return None
    except (ArtifactError, ValueError) as exc:
        await database.finish_email_delivery(
            delivery_id,
            status="failed",
            error_summary=str(exc),
        )
        return None
    except Exception as exc:
        logger.exception("后台邮件任务失败（阶段=%s）：%s", phase, delivery_id)
        if phase == "submitting":
            # Submission may have reached SMTP; recovery must never resend it.
            try:
                await database.finish_email_delivery(
                    delivery_id,
                    status="uncertain",
                    error_summary="SMTP 提交阶段发生内部错误",
                )
            except Exception:
                logger.exception("无法保存邮件不确定状态：%s", delivery_id)
            return None
        delay = _EMAIL_RETRY_DELAYS[
            min(max(delivery.attempts - 1, 0), len(_EMAIL_RETRY_DELAYS) - 1)
        ]
        try:
            _record, retry_after = await database.schedule_email_retry(
                delivery_id,
                delay_seconds=delay,
                error_summary=f"{type(exc).__name__}: 后台处理失败",
            )
        except Exception:  # noqa: BLE001 - the durable lease is the fallback.
            # The processing lease lets maintenance recover a database outage.
            return None
        return retry_after
    else:
        try:
            await database.finish_email_delivery(delivery_id, status="sent")
        except Exception:
            # SMTP already accepted the message. A stale submitting lease is
            # later classified as uncertain instead of being sent twice.
            logger.exception("SMTP 已接受邮件，但保存成功状态失败：%s", delivery_id)
        return None
    finally:
        await database.close_database()


@celery_app.task(
    bind=True,
    name="ddra.mail.send",
    acks_late=True,
    reject_on_worker_lost=True,
)
def send_email_delivery(self: Task, delivery_id: str) -> dict[str, object]:
    """Execute one replay-safe PostgreSQL email delivery."""

    retry_after = asyncio.run(_process_email_delivery(delivery_id))
    if retry_after is not None:
        raise self.retry(countdown=retry_after, max_retries=None)
    return {"status": "handled", "delivery_id": delivery_id}


__all__ = ["send_email_delivery"]
