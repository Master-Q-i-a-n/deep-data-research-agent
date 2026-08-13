import io
import json
import smtplib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from blockbuster import blockbuster_ctx
from pydantic import SecretStr

from deep_data_research_agent import interaction_tools, sandbox_manager
from deep_data_research_agent.config import Settings


def _runtime():
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-a"),
        server_info=SimpleNamespace(user=SimpleNamespace(identity="user-a")),
        tool_call_id="call-download",
    )


class FakeBackend:
    def __init__(self, content: bytes | None = b"report") -> None:
        self.content = content
        self.download_calls: list[list[str]] = []

    async def adownload_files(self, paths):
        self.download_calls.append(list(paths))
        return [
            SimpleNamespace(
                path=paths[0],
                content=self.content,
                error=None if self.content is not None else "文件不存在",
            )
        ]


def _smtp_settings(**overrides) -> Settings:
    values = {
        "smtp_enabled": True,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_username": "sender@qq.com",
        "smtp_password": SecretStr("authorization-code"),
        "smtp_use_ssl": True,
        "smtp_sender_name": "深研",
        "smtp_timeout_seconds": 30,
        "smtp_max_attachment_bytes": 20 * 1024 * 1024,
    }
    values.update(overrides)
    return Settings(**values)


def _delivery_record(**overrides):
    values = {
        "idempotency_key": "delivery-key",
        "thread_id": "thread-a",
        "user_id": "user-a",
        "recipient": "reader@example.com",
        "subject": "研究报告：final_report",
        "pdf_filename": "final_report.pdf",
        "zip_filename": "final_report-bundle.zip",
        "message_id": "<message@example.com>",
        "status": "sending",
        "error_summary": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _prepare_email_workspace(root: Path) -> None:
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


def _patch_email_workspace(monkeypatch, root: Path) -> None:
    async def ensure(*_args, **_kwargs):
        return FakeBackend()

    async def export(*_args, **_kwargs):
        return []

    def local_workspace_path(*_args, **_kwargs):
        return root

    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "ensure", ensure)
    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "export_workspace", export)
    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER,
        "local_workspace_path",
        local_workspace_path,
    )


def test_ask_user_has_defensive_result() -> None:
    result = interaction_tools.ask_user.func(
        question="采购数量是多少？",
        missing_fields=["quantity"],
        known_information="型号已确认",
    )

    assert "不能继续假设" in result


@pytest.mark.asyncio
async def test_request_report_download_exports_after_approval(monkeypatch) -> None:
    backend = FakeBackend(b"# report")
    export_calls: list[tuple[str, str]] = []

    async def ensure(*args, **kwargs):
        assert args == ("thread-a",)
        assert kwargs["user_id"] == "user-a"
        return backend

    async def export(thread_id: str, *, component: str):
        export_calls.append((thread_id, component))
        return [{"path": "/workspace/final_report.md", "size": 8}]

    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "ensure", ensure)
    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "export_workspace", export)

    result = await interaction_tools.request_report_download.coroutine(
        file_path="/workspace/final_report.md",
        download_name="采购报告.md",
        runtime=_runtime(),
    )

    assert result.status == "success"
    assert result.artifact == {
        "type": "file_download",
        "path": "/workspace/final_report.md",
        "filename": "采购报告.md",
        "size": 8,
    }
    assert backend.download_calls == [["/workspace/final_report.md"]]
    assert export_calls == [("thread-a", "supervisor")]


@pytest.mark.asyncio
async def test_request_report_download_rejects_path_outside_workspace(monkeypatch) -> None:
    async def unexpected_ensure(*_args, **_kwargs):
        raise AssertionError("非法路径不应访问沙箱")

    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER,
        "ensure",
        unexpected_ensure,
    )

    result = await interaction_tools.request_report_download.coroutine(
        file_path="/etc/passwd",
        download_name=None,
        runtime=_runtime(),
    )

    assert result.status == "error"
    assert "必须位于 /workspace" in str(result.content)


def test_email_input_validation_rejects_lists_headers_and_non_output_paths() -> None:
    with pytest.raises(ValueError, match="单个有效邮箱"):
        interaction_tools._validated_email_address(
            "first@example.com,second@example.com",
            label="收件邮箱",
        )
    with pytest.raises(ValueError, match="换行符"):
        interaction_tools._validated_subject("标题\nBcc: hidden@example.com", "report.pdf")
    with pytest.raises(ValueError, match="/workspace/output/"):
        interaction_tools._validated_report_path(
            "/workspace/input/report.pdf",
            suffix=".pdf",
            label="PDF 报告",
        )


@pytest.mark.asyncio
async def test_send_report_email_builds_pdf_and_complete_zip(monkeypatch, tmp_path: Path) -> None:
    _prepare_email_workspace(tmp_path)
    _patch_email_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(interaction_tools, "get_settings", _smtp_settings)

    async def begin_email_delivery(**kwargs):
        assert kwargs["recipient"] == "reader@example.com"
        return _delivery_record(
            idempotency_key=kwargs["idempotency_key"],
            subject=kwargs["subject"],
            message_id=kwargs["message_id"],
        ), True

    finished: list[tuple[str, str]] = []

    async def finish_email_delivery(key: str, *, status: str, error_summary=None):
        assert error_summary is None
        finished.append((key, status))
        return _delivery_record(idempotency_key=key, status=status)

    sent_messages = []

    def send_smtp_message(email, *, settings, recipient):
        assert settings.smtp_password.get_secret_value() == "authorization-code"
        assert recipient == "reader@example.com"
        sent_messages.append(email)

    monkeypatch.setattr(interaction_tools.database, "begin_email_delivery", begin_email_delivery)
    monkeypatch.setattr(interaction_tools.database, "finish_email_delivery", finish_email_delivery)
    monkeypatch.setattr(interaction_tools, "_send_smtp_message", send_smtp_message)

    result = await interaction_tools.send_report_email.coroutine(
        recipient="reader@example.com",
        subject=None,
        pdf_path="/workspace/output/final_report.pdf",
        markdown_path="/workspace/output/final_report.md",
        runtime=_runtime(),
    )

    payload = json.loads(result.content)
    assert result.status == "success"
    assert payload["status"] == "sent"
    assert payload["attachments"] == ["final_report.pdf", "final_report-bundle.zip"]
    assert len(sent_messages) == 1
    email = sent_messages[0]
    assert str(email["From"]) == "深研 <sender@qq.com>"
    assert str(email["To"]) == "reader@example.com"
    assert str(email["Subject"]) == "研究报告：final_report"
    attachments = list(email.iter_attachments())
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
    assert finished and finished[0][1] == "sent"


@pytest.mark.asyncio
async def test_send_report_email_does_not_block_langgraph_event_loop(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Exercise attachment resolution under the same guard used by LangGraph Server."""

    _prepare_email_workspace(tmp_path)
    _patch_email_workspace(monkeypatch, tmp_path)
    # LangGraph initializes and caches settings before tool execution. Construct
    # the test settings before enabling BlockBuster to mirror that lifecycle.
    settings = _smtp_settings()
    monkeypatch.setattr(interaction_tools, "get_settings", lambda: settings)

    async def begin_email_delivery(**kwargs):
        return _delivery_record(
            idempotency_key=kwargs["idempotency_key"],
            subject=kwargs["subject"],
            message_id=kwargs["message_id"],
        ), True

    async def finish_email_delivery(key: str, *, status: str, error_summary=None):
        return _delivery_record(
            idempotency_key=key,
            status=status,
            error_summary=error_summary,
        )

    monkeypatch.setattr(interaction_tools.database, "begin_email_delivery", begin_email_delivery)
    monkeypatch.setattr(interaction_tools.database, "finish_email_delivery", finish_email_delivery)
    monkeypatch.setattr(interaction_tools, "_send_smtp_message", lambda *_args, **_kwargs: None)

    with blockbuster_ctx(scanned_modules=["deep_data_research_agent"]):
        result = await interaction_tools.send_report_email.coroutine(
            recipient="reader@example.com",
            runtime=_runtime(),
        )

    assert result.status == "success"
    assert json.loads(result.content)["status"] == "sent"


@pytest.mark.asyncio
async def test_send_report_email_replay_does_not_send_again(monkeypatch, tmp_path: Path) -> None:
    _prepare_email_workspace(tmp_path)
    _patch_email_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(interaction_tools, "get_settings", _smtp_settings)

    async def begin_email_delivery(**_kwargs):
        return _delivery_record(status="sent"), False

    def unexpected_send(*_args, **_kwargs):
        raise AssertionError("同一工具调用不得重复发送")

    monkeypatch.setattr(interaction_tools.database, "begin_email_delivery", begin_email_delivery)
    monkeypatch.setattr(interaction_tools, "_send_smtp_message", unexpected_send)

    result = await interaction_tools.send_report_email.coroutine(
        recipient="reader@example.com",
        runtime=_runtime(),
    )

    assert result.status == "success"
    assert "未重复发送" in str(result.content)


@pytest.mark.asyncio
async def test_send_report_email_internal_error_reports_safe_stage(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _prepare_email_workspace(tmp_path)
    _patch_email_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(interaction_tools, "get_settings", _smtp_settings)

    async def fail_delivery_record(**_kwargs):
        raise KeyError("private database detail")

    monkeypatch.setattr(
        interaction_tools.database,
        "begin_email_delivery",
        fail_delivery_record,
    )

    result = await interaction_tools.send_report_email.coroutine(
        recipient="reader@example.com",
        runtime=_runtime(),
    )

    assert result.status == "error"
    assert "投递记录阶段发生内部错误" in str(result.content)
    assert "本轮不会自动重试" in str(result.content)
    assert '"diagnostic_code": "idempotency_record:KeyError"' in str(result.content)
    assert "private database detail" not in str(result.content)


@pytest.mark.asyncio
async def test_send_report_email_marks_ambiguous_disconnect_uncertain(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _prepare_email_workspace(tmp_path)
    _patch_email_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(interaction_tools, "get_settings", _smtp_settings)

    async def begin_email_delivery(**_kwargs):
        return _delivery_record(), True

    finished: list[tuple[str, str, str | None]] = []

    async def finish_email_delivery(key: str, *, status: str, error_summary=None):
        finished.append((key, status, error_summary))
        return _delivery_record(status=status, error_summary=error_summary)

    def disconnect(*_args, **_kwargs):
        raise interaction_tools._SMTPDeliveryError(
            "SMTP 提交过程中连接中断，邮件投递状态不确定。",
            uncertain=True,
        )

    monkeypatch.setattr(interaction_tools.database, "begin_email_delivery", begin_email_delivery)
    monkeypatch.setattr(interaction_tools.database, "finish_email_delivery", finish_email_delivery)
    monkeypatch.setattr(interaction_tools, "_send_smtp_message", disconnect)

    result = await interaction_tools.send_report_email.coroutine(
        recipient="reader@example.com",
        runtime=_runtime(),
    )

    assert result.status == "error"
    assert json.loads(result.content)["status"] == "uncertain"
    assert finished[0][1] == "uncertain"


@pytest.mark.asyncio
async def test_send_report_email_rejects_oversized_attachments_before_db(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _prepare_email_workspace(tmp_path)
    _patch_email_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(
        interaction_tools,
        "get_settings",
        lambda: _smtp_settings(smtp_max_attachment_bytes=1024),
    )
    (tmp_path / "output" / "final_report.pdf").write_bytes(b"x" * 2048)

    async def unexpected_begin(**_kwargs):
        raise AssertionError("超限附件不应创建投递记录")

    monkeypatch.setattr(interaction_tools.database, "begin_email_delivery", unexpected_begin)

    result = await interaction_tools.send_report_email.coroutine(
        recipient="reader@example.com",
        runtime=_runtime(),
    )

    assert result.status == "error"
    assert "超过发送上限" in str(result.content)


@pytest.mark.asyncio
async def test_send_report_email_disabled_does_not_touch_sandbox(monkeypatch) -> None:
    monkeypatch.setattr(
        interaction_tools,
        "get_settings",
        lambda: _smtp_settings(smtp_enabled=False),
    )

    async def unexpected_ensure(*_args, **_kwargs):
        raise AssertionError("未启用 SMTP 时不应访问沙箱")

    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "ensure", unexpected_ensure)

    result = await interaction_tools.send_report_email.coroutine(
        recipient="reader@example.com",
        runtime=_runtime(),
    )

    assert result.status == "error"
    assert "尚未启用" in str(result.content)


def test_smtp_submission_uses_qq_credentials_once(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class FakeSMTP:
        def __init__(self, host, port, *, timeout, context):
            calls.append(("connect", (host, port, timeout, context is not None)))

        def login(self, username, password):
            calls.append(("login", (username, password)))

        def send_message(self, email, *, from_addr, to_addrs):
            calls.append(("send", (email["Subject"], from_addr, to_addrs)))
            return {}

        def quit(self):
            calls.append(("quit", None))

        def close(self):
            calls.append(("close", None))

    monkeypatch.setattr(interaction_tools.smtplib, "SMTP_SSL", FakeSMTP)
    settings = _smtp_settings()
    email = interaction_tools._build_report_email(
        settings=settings,
        recipient="reader@example.com",
        subject="研究报告",
        message_id="<message@example.com>",
        pdf_filename="report.pdf",
        pdf_content=b"pdf",
        zip_filename="report-bundle.zip",
        zip_content=b"zip",
    )

    interaction_tools._send_smtp_message(
        email,
        settings=settings,
        recipient="reader@example.com",
    )

    assert [name for name, _value in calls] == ["connect", "login", "send", "quit"]
    assert calls[1][1] == ("sender@qq.com", "authorization-code")


@pytest.mark.parametrize(
    ("failure", "expected", "uncertain"),
    [
        (
            smtplib.SMTPAuthenticationError(535, b"authentication failed"),
            "SMTP 认证失败",
            False,
        ),
        (
            smtplib.SMTPRecipientsRefused({"reader@example.com": (550, b"rejected")}),
            "收件邮箱被服务器拒绝",
            False,
        ),
        (TimeoutError("timed out"), "投递状态不确定", True),
    ],
)
def test_smtp_submission_sanitizes_failures(
    monkeypatch,
    failure: Exception,
    expected: str,
    uncertain: bool,
) -> None:
    class FailingSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, _username, _password):
            if isinstance(failure, smtplib.SMTPAuthenticationError):
                raise failure

        def send_message(self, *_args, **_kwargs):
            raise failure

        def quit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(interaction_tools.smtplib, "SMTP_SSL", FailingSMTP)
    settings = _smtp_settings()
    email = interaction_tools._build_report_email(
        settings=settings,
        recipient="reader@example.com",
        subject="研究报告",
        message_id="<message@example.com>",
        pdf_filename="report.pdf",
        pdf_content=b"pdf",
        zip_filename="report-bundle.zip",
        zip_content=b"zip",
    )

    with pytest.raises(interaction_tools._SMTPDeliveryError, match=expected) as raised:
        interaction_tools._send_smtp_message(
            email,
            settings=settings,
            recipient="reader@example.com",
        )

    assert raised.value.uncertain is uncertain
