import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from deep_data_research_agent.core.config import Settings
from deep_data_research_agent.infrastructure.workspace import (
    LocalWorkspaceStore,
    WorkspaceScope,
)
from deep_data_research_agent.tools.interaction import _SMTPDeliveryError
from deep_data_research_agent.workers.app import celery_app
from deep_data_research_agent.workers.tasks import email as celery_tasks


def _settings(**overrides) -> Settings:
    values = {
        "smtp_enabled": True,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_username": "sender@qq.com",
        "smtp_password": SecretStr("authorization-code"),
        "smtp_use_ssl": True,
        "smtp_max_attachment_bytes": 20 * 1024 * 1024,
    }
    values.update(overrides)
    return Settings(**values)


def _delivery(**overrides):
    values = {
        "idempotency_key": "d" * 64,
        "thread_id": "thread-a",
        "user_id": "user-a",
        "recipient": "reader@example.com",
        "subject": "研究报告",
        "pdf_filename": "final_report.pdf",
        "zip_filename": "final_report-bundle.zip",
        "pdf_path": "/workspace/output/final_report.pdf",
        "markdown_path": "/workspace/output/final_report.md",
        "message_id": "<stable@example.com>",
        "status": "processing",
        "attempts": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _workspace(root: Path) -> None:
    output = root / "output"
    charts = output / "charts"
    charts.mkdir(parents=True)
    (output / "final_report.pdf").write_bytes(b"%PDF-1.7\nreport")
    (output / "final_report.md").write_text(
        "# 报告\n\n![趋势](charts/trend.png)\n",
        encoding="utf-8",
    )
    (charts / "trend.png").write_bytes(b"png")
    (output / "metrics.csv").write_text("name,value\na,1\n", encoding="utf-8")


def _install_worker_workspace(monkeypatch, tmp_path: Path, delivery) -> None:
    store = LocalWorkspaceStore(tmp_path)
    scope = WorkspaceScope(delivery.user_id, delivery.thread_id, "supervisor")
    manager = celery_tasks.sandbox_manager.SANDBOX_MANAGER
    manager._thread_users.pop(delivery.thread_id, None)
    monkeypatch.setattr(manager, "workspace_store", store)
    _workspace(store.workspace_path(scope))


def test_celery_routes_use_three_explicit_queues() -> None:
    routes = celery_app.conf.task_routes
    assert routes["ddra.memory.process"]["queue"] == "memory"
    assert routes["ddra.mail.send"]["queue"] == "mail"
    assert routes["ddra.maintenance.recover_memory"]["queue"] == "maintenance"
    assert celery_app.conf.task_ignore_result is True
    assert celery_app.conf.broker_transport_options["global_keyprefix"] == "ddra-celery:"


@pytest.mark.asyncio
async def test_email_worker_builds_complete_zip_and_marks_sent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    delivery = _delivery()
    _install_worker_workspace(monkeypatch, tmp_path, delivery)
    submitted: list[str] = []
    finished: list[tuple[str, str, str | None]] = []
    sent_messages = []

    async def claim(_delivery_id):
        return delivery

    async def submitting(delivery_id, **_kwargs):
        submitted.append(delivery_id)
        return delivery

    async def finish(delivery_id, *, status, error_summary=None):
        finished.append((delivery_id, status, error_summary))
        return delivery

    async def close_database():
        return None

    def send(email, **_kwargs):
        sent_messages.append(email)

    monkeypatch.setattr(celery_tasks.database, "claim_email_delivery", claim)
    monkeypatch.setattr(celery_tasks.database, "mark_email_submitting", submitting)
    monkeypatch.setattr(celery_tasks.database, "finish_email_delivery", finish)
    monkeypatch.setattr(celery_tasks.database, "close_database", close_database)
    monkeypatch.setattr(celery_tasks, "get_settings", _settings)
    monkeypatch.setattr(celery_tasks, "_send_smtp_message", send)

    assert await celery_tasks._process_email_delivery(delivery.idempotency_key) is None
    assert submitted == [delivery.idempotency_key]
    assert finished == [(delivery.idempotency_key, "sent", None)]
    attachments = list(sent_messages[0].iter_attachments())
    assert [part.get_filename() for part in attachments] == [
        "final_report.pdf",
        "final_report-bundle.zip",
    ]
    with zipfile.ZipFile(io.BytesIO(attachments[1].get_payload(decode=True))) as archive:
        assert set(archive.namelist()) == {
            "charts/trend.png",
            "final_report.md",
            "metrics.csv",
        }


@pytest.mark.asyncio
async def test_email_worker_retries_only_known_pre_submit_disconnects(
    monkeypatch,
    tmp_path: Path,
) -> None:
    delivery = _delivery(attempts=1)
    _install_worker_workspace(monkeypatch, tmp_path, delivery)
    retries: list[tuple[int, str]] = []

    async def claim(_delivery_id):
        return delivery

    async def submitting(_delivery_id, **_kwargs):
        return delivery

    async def schedule(_delivery_id, *, delay_seconds, error_summary, **_kwargs):
        retries.append((delay_seconds, error_summary))
        return delivery, delay_seconds

    async def close_database():
        return None

    def disconnect(*_args, **_kwargs):
        raise _SMTPDeliveryError("暂时无法连接 SMTP", retryable=True)

    monkeypatch.setattr(celery_tasks.database, "claim_email_delivery", claim)
    monkeypatch.setattr(celery_tasks.database, "mark_email_submitting", submitting)
    monkeypatch.setattr(celery_tasks.database, "schedule_email_retry", schedule)
    monkeypatch.setattr(celery_tasks.database, "close_database", close_database)
    monkeypatch.setattr(celery_tasks, "get_settings", _settings)
    monkeypatch.setattr(celery_tasks, "_send_smtp_message", disconnect)

    assert await celery_tasks._process_email_delivery(delivery.idempotency_key) == 10
    assert retries == [(10, "暂时无法连接 SMTP")]


@pytest.mark.asyncio
async def test_email_worker_marks_ambiguous_submission_uncertain(
    monkeypatch,
    tmp_path: Path,
) -> None:
    delivery = _delivery()
    _install_worker_workspace(monkeypatch, tmp_path, delivery)
    finished: list[str] = []

    async def claim(_delivery_id):
        return delivery

    async def submitting(_delivery_id, **_kwargs):
        return delivery

    async def finish(_delivery_id, *, status, **_kwargs):
        finished.append(status)
        return delivery

    async def close_database():
        return None

    def disconnect(*_args, **_kwargs):
        raise _SMTPDeliveryError("投递状态不确定", uncertain=True)

    monkeypatch.setattr(celery_tasks.database, "claim_email_delivery", claim)
    monkeypatch.setattr(celery_tasks.database, "mark_email_submitting", submitting)
    monkeypatch.setattr(celery_tasks.database, "finish_email_delivery", finish)
    monkeypatch.setattr(celery_tasks.database, "close_database", close_database)
    monkeypatch.setattr(celery_tasks, "get_settings", _settings)
    monkeypatch.setattr(celery_tasks, "_send_smtp_message", disconnect)

    assert await celery_tasks._process_email_delivery(delivery.idempotency_key) is None
    assert finished == ["uncertain"]
