"""Approval-gated user interaction, report download, and email tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import smtplib
import ssl
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path, PurePosixPath
from typing import Annotated

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from pydantic import Field

from deep_data_research_agent import database, sandbox_manager
from deep_data_research_agent.artifacts import (
    DOWNLOADABLE_SUFFIXES,
    ArtifactError,
    build_markdown_bundle,
    resolve_download_path,
)
from deep_data_research_agent.config import Settings, get_settings
from deep_data_research_agent.identity import user_identity

logger = logging.getLogger(__name__)
_EMAIL_STATUS_MESSAGES = {
    "sending": "相同发送请求正在处理，系统未重复发送。",
    "failed": "相同发送请求此前失败，系统未自动重发。",
    "uncertain": "相同发送请求的投递状态不确定，系统未自动重发。",
}


class _SMTPDeliveryError(RuntimeError):
    """A sanitized SMTP failure with explicit delivery uncertainty."""

    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


def _validated_download_name(value: str | None, source: PurePosixPath) -> str:
    """Return a safe browser download name without accepting a local path."""

    if value is None or not value.strip():
        return source.name
    name = value.strip()
    if PurePosixPath(name).name != name or "\\" in name or name in {".", ".."}:
        raise ValueError("下载名称只能是文件名，不能包含目录")
    if PurePosixPath(name).suffix.lower() not in DOWNLOADABLE_SUFFIXES:
        raise ValueError("下载文件类型不受支持")
    return name


def _validated_email_address(value: str, *, label: str) -> str:
    """Accept one plain addr-spec and reject display names or recipient lists."""

    address_text = value.strip()
    if (
        not address_text
        or len(address_text) > 320
        or any(character in address_text for character in "\r\n,;")
    ):
        raise ValueError(f"{label}必须是单个有效邮箱地址")
    try:
        address = Address(addr_spec=address_text)
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是单个有效邮箱地址") from exc
    if not address.username or not address.domain or address.display_name:
        raise ValueError(f"{label}必须是单个有效邮箱地址")
    return address.addr_spec


def _validated_report_path(value: str, *, suffix: str, label: str) -> str:
    """Restrict outgoing attachments to one expected report type under output."""

    relative = sandbox_manager.workspace_relative_path(value)
    if len(relative.parts) < 2 or relative.parts[0] != "output":
        raise ValueError(f"{label}必须位于 /workspace/output/ 下")
    if relative.suffix.lower() != suffix:
        raise ValueError(f"{label}必须是 {suffix} 文件")
    return f"/workspace/{relative.as_posix()}"


def _validated_subject(subject: str | None, pdf_filename: str) -> str:
    """Return a bounded header-safe subject, using the report name by default."""

    value = subject.strip() if subject is not None else ""
    if not value:
        value = f"研究报告：{PurePosixPath(pdf_filename).stem}"
    if "\r" in value or "\n" in value:
        raise ValueError("邮件主题不能包含换行符")
    if len(value) > 120:
        raise ValueError("邮件主题不能超过 120 个字符")
    return value


def _email_result(
    runtime: ToolRuntime,
    *,
    status: str,
    recipient: str | None = None,
    attachments: list[str] | None = None,
    message: str | None = None,
    diagnostic_code: str | None = None,
) -> ToolMessage:
    """Return a paired tool result without exposing SMTP implementation details."""

    payload: dict[str, object] = {"status": status}
    if recipient:
        payload["recipient"] = recipient
    if attachments:
        payload["attachments"] = attachments
    if message:
        payload["message"] = message
    if diagnostic_code:
        payload["diagnostic_code"] = diagnostic_code
    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        tool_call_id=runtime.tool_call_id,
        name="send_report_email",
        status="success" if status == "sent" else "error",
    )


def _build_report_email(
    *,
    settings: Settings,
    recipient: str,
    subject: str,
    message_id: str,
    pdf_filename: str,
    pdf_content: bytes,
    zip_filename: str,
    zip_content: bytes,
) -> EmailMessage:
    """Build the fixed report email; credentials never enter the message."""

    sender = _validated_email_address(settings.smtp_username, label="发件邮箱配置")
    email = EmailMessage()
    email["From"] = Address(
        display_name=settings.smtp_sender_name.strip() or "深研",
        addr_spec=sender,
    )
    email["To"] = recipient
    email["Subject"] = subject
    email["Message-ID"] = message_id
    email.set_content(
        "您好，\n\n附件中包含 PDF 主报告和完整分析材料 ZIP。\n\n此邮件由深研平台发送。"
    )
    email.add_attachment(
        pdf_content,
        maintype="application",
        subtype="pdf",
        filename=pdf_filename,
    )
    email.add_attachment(
        zip_content,
        maintype="application",
        subtype="zip",
        filename=zip_filename,
    )
    return email


def _load_report_attachments(
    root: Path,
    pdf_path: str,
    markdown_path: str,
) -> tuple[str, bytes, str, bytes]:
    """Resolve and read report attachments outside the async event loop."""

    # Path resolution, stat calls, recursive directory scans, and file reads are
    # all blocking operations. Keep the complete filesystem workflow in one
    # worker-thread boundary so LangGraph Server's BlockBuster remains satisfied.
    pdf_file = resolve_download_path(root, pdf_path)
    if pdf_file.suffix.lower() != ".pdf":
        raise ValueError("PDF 报告文件类型无效。")
    pdf_content = pdf_file.read_bytes()
    zip_content, zip_filename = build_markdown_bundle(root, markdown_path)
    return pdf_file.name, pdf_content, zip_filename, zip_content


def _send_smtp_message(
    email: EmailMessage,
    *,
    settings: Settings,
    recipient: str,
) -> None:
    """Submit one message without retries and classify ambiguous disconnects."""

    password = settings.smtp_password.get_secret_value()
    client: smtplib.SMTP | smtplib.SMTP_SSL | None = None
    phase = "connecting"
    try:
        context = ssl.create_default_context()
        if settings.smtp_use_ssl:
            client = smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
                context=context,
            )
        else:
            client = smtplib.SMTP(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
            )
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        phase = "authenticating"
        client.login(settings.smtp_username, password)
        phase = "submitting"
        refused = client.send_message(
            email,
            from_addr=settings.smtp_username,
            to_addrs=[recipient],
        )
        if refused:
            raise _SMTPDeliveryError("收件邮箱被服务器拒绝。")
    except smtplib.SMTPAuthenticationError as exc:
        raise _SMTPDeliveryError("SMTP 认证失败，请检查 QQ 邮箱授权码。") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise _SMTPDeliveryError("收件邮箱被服务器拒绝。") from exc
    except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
        raise _SMTPDeliveryError("SMTP 服务器拒绝了本次邮件。") from exc
    except _SMTPDeliveryError:
        raise
    except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
        uncertain = phase == "submitting"
        message = (
            "SMTP 提交过程中连接中断，邮件投递状态不确定。"
            if uncertain
            else "无法连接 QQ SMTP 服务。"
        )
        raise _SMTPDeliveryError(message, uncertain=uncertain) from exc
    except smtplib.SMTPException as exc:
        raise _SMTPDeliveryError("QQ SMTP 服务拒绝或无法处理本次邮件。") from exc
    finally:
        if client is not None:
            try:
                # Once send_message returns, a failed QUIT must not downgrade success.
                client.quit()
            except (OSError, smtplib.SMTPException):
                try:
                    client.close()
                except OSError:
                    pass


@tool(
    "ask_user",
    description=(
        "当前任务缺少会影响分析正确性的关键信息时，暂停执行并请求用户补充。"
        "一次最多询问三个缺失字段；信息足够时不要调用。"
    ),
)
def ask_user(
    question: Annotated[str, "需要用户回答的明确问题"],
    missing_fields: Annotated[
        list[str],
        Field(min_length=1, max_length=3, description="缺失字段名称，最多三个"),
    ],
    known_information: Annotated[str, "已经确认的信息摘要"] = "",
) -> str:
    """Provide a defensive result if HITL interception is accidentally disabled."""

    return "尚未收到用户补充信息，不能继续假设或执行后续分析。"


@tool(
    "request_report_download",
    description=(
        "仅当用户明确要求下载、保存到本地或导出文件时调用。默认下载"
        "/workspace/output/final_report.pdf；PDF 不存在时显式传入 Markdown 路径。"
        "调用前必须确认目标文件已经生成。"
    ),
)
async def request_report_download(
    file_path: Annotated[str, "要下载的 /workspace 下文件路径"] = (
        "/workspace/output/final_report.pdf"
    ),
    download_name: Annotated[str | None, "浏览器保存时使用的文件名，可省略"] = None,
    *,
    runtime: ToolRuntime,
) -> ToolMessage:
    """Export one approved sandbox file and return app-facing download metadata."""

    try:
        relative = sandbox_manager.workspace_relative_path(file_path)
        if relative.suffix.lower() not in DOWNLOADABLE_SUFFIXES:
            raise ValueError("仅支持下载报告、表格、图片、JSON 或 ZIP 产物")
        virtual_path = f"/workspace/{relative.as_posix()}"
        filename = _validated_download_name(download_name, relative)

        thread_id = sandbox_manager.thread_id_from_runtime(runtime)
        user_id = user_identity(runtime)
        backend = await sandbox_manager.SANDBOX_MANAGER.ensure(
            thread_id,
            component="supervisor",
            network_enabled=True,
            user_id=user_id,
        )
        responses = await backend.adownload_files([virtual_path])
        response = responses[0] if responses else None
        if response is None or response.error or response.content is None:
            detail = response.error if response is not None else "文件不存在"
            raise RuntimeError(f"无法读取下载文件：{detail}")

        # HITL 批准后立即导出，不能依赖仅在完整 run 结束时执行的 aafter_agent。
        await sandbox_manager.SANDBOX_MANAGER.export_workspace(
            thread_id,
            component="supervisor",
        )
        return ToolMessage(
            content=f"文件已准备下载：{filename}",
            tool_call_id=runtime.tool_call_id,
            name="request_report_download",
            status="success",
            artifact={
                "type": "file_download",
                "path": virtual_path,
                "filename": filename,
                "size": len(response.content),
            },
        )
    except Exception as exc:  # noqa: BLE001 - keep download failures recoverable.
        return ToolMessage(
            content=f"下载准备失败：{exc}",
            tool_call_id=runtime.tool_call_id,
            name="request_report_download",
            status="error",
        )


@tool(
    "send_report_email",
    description=(
        "仅当用户明确要求通过邮件发送报告时调用。每次只接受一个用户提供的收件邮箱，"
        "并同时发送 PDF 主报告和由 Markdown、图片及数据文件组成的完整 ZIP。调用前必须"
        "确认两个报告文件已经生成；不要推测收件邮箱。"
    ),
)
async def send_report_email(
    recipient: Annotated[str, "用户本次明确提供的单个收件邮箱"],
    subject: Annotated[
        str | None,
        Field(default=None, max_length=120, description="可选邮件主题"),
    ] = None,
    pdf_path: Annotated[str, "位于 /workspace/output/ 下的 PDF 主报告路径"] = (
        "/workspace/output/final_report.pdf"
    ),
    markdown_path: Annotated[str, "位于 /workspace/output/ 下的 Markdown 主报告路径"] = (
        "/workspace/output/final_report.md"
    ),
    *,
    runtime: ToolRuntime,
) -> ToolMessage:
    """Export, bundle, and send one approved report email without automatic retries."""

    normalized_recipient: str | None = None
    stage = "configuration"
    try:
        settings = get_settings()
        if not settings.smtp_enabled:
            raise ValueError("邮件发送功能尚未启用。")
        if (
            not settings.smtp_host.strip()
            or not settings.smtp_username.strip()
            or not settings.smtp_password.get_secret_value()
        ):
            raise ValueError("QQ SMTP 发件邮箱或授权码尚未配置。")

        normalized_recipient = _validated_email_address(recipient, label="收件邮箱")
        normalized_pdf_path = _validated_report_path(
            pdf_path,
            suffix=".pdf",
            label="PDF 报告",
        )
        normalized_markdown_path = _validated_report_path(
            markdown_path,
            suffix=".md",
            label="Markdown 报告",
        )

        stage = "workspace_export"
        thread_id = sandbox_manager.thread_id_from_runtime(runtime)
        user_id = user_identity(runtime)
        await sandbox_manager.SANDBOX_MANAGER.ensure(
            thread_id,
            component="supervisor",
            network_enabled=True,
            user_id=user_id,
        )
        # HITL resumes before the run-level after_agent export, so snapshot now.
        await sandbox_manager.SANDBOX_MANAGER.export_workspace(
            thread_id,
            component="supervisor",
        )
        root = sandbox_manager.SANDBOX_MANAGER.local_workspace_path(
            thread_id,
            "supervisor",
            user_id=user_id,
        )
        stage = "attachment_build"
        pdf_filename, pdf_content, zip_filename, zip_content = await asyncio.to_thread(
            _load_report_attachments,
            root,
            normalized_pdf_path,
            normalized_markdown_path,
        )
        stage = "attachment_limit"
        total_size = len(pdf_content) + len(zip_content)
        if total_size > settings.smtp_max_attachment_bytes:
            raise ValueError(
                "邮件附件总量超过发送上限，请改用浏览器下载报告和完整 ZIP。"
            )

        normalized_subject = _validated_subject(subject, pdf_filename)
        sender = _validated_email_address(
            settings.smtp_username,
            label="发件邮箱配置",
        )
        message_id = make_msgid(domain=sender.rsplit("@", 1)[1])
        tool_call_id = str(runtime.tool_call_id or "").strip()
        if not tool_call_id:
            raise RuntimeError("邮件工具调用缺少幂等标识。")
        idempotency_key = hashlib.sha256(
            f"{user_id}:{thread_id}:{tool_call_id}".encode()
        ).hexdigest()

        stage = "idempotency_record"
        delivery, created = await database.begin_email_delivery(
            idempotency_key=idempotency_key,
            thread_id=thread_id,
            user_id=user_id,
            recipient=normalized_recipient,
            subject=normalized_subject,
            pdf_filename=pdf_filename,
            zip_filename=zip_filename,
            message_id=message_id,
        )
        attachments = [delivery.pdf_filename, delivery.zip_filename]
        if not created:
            if delivery.status == "sent":
                return _email_result(
                    runtime,
                    status="sent",
                    recipient=delivery.recipient,
                    attachments=attachments,
                    message="相同发送请求已完成，系统未重复发送。",
                )
            return _email_result(
                runtime,
                status=delivery.status,
                recipient=delivery.recipient,
                attachments=attachments,
                message=_EMAIL_STATUS_MESSAGES.get(
                    delivery.status,
                    "相同发送请求已有记录，系统未重复发送。",
                ),
            )

        stage = "message_build"
        email = _build_report_email(
            settings=settings,
            recipient=normalized_recipient,
            subject=normalized_subject,
            message_id=delivery.message_id,
            pdf_filename=pdf_filename,
            pdf_content=pdf_content,
            zip_filename=zip_filename,
            zip_content=zip_content,
        )
        stage = "smtp_submit"
        try:
            await asyncio.to_thread(
                _send_smtp_message,
                email,
                settings=settings,
                recipient=normalized_recipient,
            )
        except _SMTPDeliveryError as exc:
            delivery_status = "uncertain" if exc.uncertain else "failed"
            try:
                await database.finish_email_delivery(
                    idempotency_key,
                    status=delivery_status,
                    error_summary=str(exc),
                )
            except Exception:
                logger.exception("无法保存邮件失败状态，幂等记录将保持 sending")
            return _email_result(
                runtime,
                status=delivery_status,
                recipient=normalized_recipient,
                attachments=attachments,
                message=str(exc),
            )

        try:
            await database.finish_email_delivery(
                idempotency_key,
                status="sent",
            )
        except Exception:  # noqa: BLE001 - SMTP already accepted the message.
            return _email_result(
                runtime,
                status="uncertain",
                recipient=normalized_recipient,
                attachments=attachments,
                message=(
                    "SMTP 已接受邮件，但投递状态保存失败；为避免重复发送，"
                    "系统不会自动重试。"
                ),
            )
        return _email_result(
            runtime,
            status="sent",
            recipient=normalized_recipient,
            attachments=attachments,
            message="报告邮件已发送。",
        )
    except (ArtifactError, RuntimeError, ValueError) as exc:
        return _email_result(
            runtime,
            status="failed",
            recipient=normalized_recipient,
            message=str(exc),
        )
    except Exception as exc:
        logger.exception("邮件发送内部错误（阶段=%s）", stage)
        stage_label = {
            "configuration": "配置检查",
            "workspace_export": "工作区导出",
            "attachment_build": "附件构建",
            "attachment_limit": "附件大小检查",
            "idempotency_record": "投递记录",
            "message_build": "邮件构建",
            "smtp_submit": "SMTP 提交",
        }.get(stage, "内部处理")
        return _email_result(
            runtime,
            status="failed",
            recipient=normalized_recipient,
            message=(
                f"邮件发送在{stage_label}阶段发生内部错误；本轮不会自动重试，"
                "报告文件仍可通过浏览器下载。"
            ),
            diagnostic_code=f"{stage}:{type(exc).__name__}",
        )


INTERACTION_TOOLS = [ask_user, request_report_download, send_report_email]
