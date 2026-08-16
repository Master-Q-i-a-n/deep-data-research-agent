"""Custom FastAPI routes mounted by the LangGraph Agent Server."""

from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import logging
import mimetypes
import re
import unicodedata
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Annotated
from xml.etree.ElementTree import ParseError

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from langgraph_sdk import get_client
from langsmith import traceable
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import BaseModel, Field

from deep_data_research_agent import database, sandbox_manager
from deep_data_research_agent.artifacts import (
    ArtifactError,
    build_markdown_bundle,
    resolve_download_path,
    workspace_artifacts,
)
from deep_data_research_agent.auth import bearer_token
from deep_data_research_agent.config import get_settings
from deep_data_research_agent.memory import MEMORY_QUEUE, start_memory_worker

_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")
_PASSWORD_HASHER = PasswordHasher()
logger = logging.getLogger(__name__)
_FAILED_TASK_STATUSES = frozenset({"error", "timeout", "interrupted"})
_UPLOAD_SUFFIXES = frozenset({".csv", ".tsv", ".xlsx"})
_UPLOAD_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_MAX_UPLOAD_FILES = 5
_MAX_UPLOAD_FILE_BYTES = 50 * 1024 * 1024
_MAX_UPLOAD_TOTAL_BYTES = 100 * 1024 * 1024
_MAX_XLSX_ENTRIES = 10_000
_MAX_XLSX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*')


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class MemorySettingsRequest(BaseModel):
    failure_lesson_saving_enabled: bool


class AsyncTaskStatusRequest(BaseModel):
    """Identify the owning Supervisor thread; task IDs come from its state."""

    thread_id: str = Field(min_length=1, max_length=64)


def _sanitized_task_error(status: str, error: object = None) -> str | None:
    """Convert remote run failures to safe, user-facing summaries."""

    if status == "timeout":
        return "子任务执行超时，请缩小任务范围后重试。"
    if status == "interrupted":
        return "子任务已中断，需要恢复或重新发起。"
    if status != "error":
        return None

    # Only classify known exception categories. Never expose traceback paths,
    # request content, provider responses, credentials, or arbitrary messages.
    raw = str(error or "")
    if "TypeError" in raw:
        return "子任务发生内部类型错误，原样重试通常不会成功。"
    if "ValidationError" in raw or "JSONDecodeError" in raw:
        return "子任务结果格式或数据校验失败。"
    if any(name in raw for name in ("ConnectError", "ConnectionError")):
        return "子任务无法连接所需服务。"
    if "Timeout" in raw:
        return "子任务访问外部服务或执行过程超时。"
    if "PermissionError" in raw:
        return "子任务访问所需资源时权限不足。"
    return "子任务执行失败，详细诊断信息已保留在 LangSmith。"


async def _authenticated_user_id(authorization: str | None) -> str:
    """Resolve the current user without silently accepting an invalid token."""

    token = bearer_token(authorization)
    if token is None:
        if get_settings().app_env == "production":
            raise HTTPException(
                status_code=401,
                detail="请先登录",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return database.DEFAULT_USER_ID
    user = await database.resolve_login_session(token)
    if user is None:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    return user.id


def _client_ip(request: Request) -> str:
    """Return the ASGI peer address without trusting forwarded headers."""

    raw = request.client.host if request.client is not None else "unknown"
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return raw.casefold()
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return str(address.ipv4_mapped)
    return address.compressed


async def _enforce_rate_limit(
    *,
    scope: str,
    raw_key: str,
    limit: int,
    window_seconds: int,
    detail: str,
) -> None:
    """Translate durable limiter decisions into safe HTTP responses."""

    try:
        decision = await database.consume_rate_limit(
            scope,
            raw_key,
            limit=limit,
            window_seconds=window_seconds,
        )
    except Exception as exc:
        logger.exception("%s 限流存储不可用", scope)
        raise HTTPException(
            status_code=503,
            detail="请求保护服务暂不可用，请稍后重试",
        ) from exc
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail=detail,
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


def _workspace_artifacts(root: Path) -> list[dict[str, object]]:
    """Keep the existing webapp helper name while sharing artifact policy."""

    return workspace_artifacts(root)


def _uploaded_files(root: Path) -> list[dict[str, object]]:
    """List immutable input files from a user-owned local snapshot."""

    input_root = (root / "input").resolve()
    if not input_root.is_dir():
        return []
    files: list[dict[str, object]] = []
    for candidate in sorted(input_root.iterdir()):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(input_root):
            continue
        suffix = resolved.suffix.lower()
        if suffix not in _UPLOAD_SUFFIXES:
            continue
        files.append(
            {
                "name": resolved.name,
                "path": f"/workspace/input/{resolved.name}",
                "size": resolved.stat().st_size,
                "media_type": _UPLOAD_MEDIA_TYPES[suffix],
            }
        )
    return files


def _validated_upload_name(value: str | None) -> str:
    """Validate a portable leaf filename for the Windows-backed snapshot."""

    name = unicodedata.normalize("NFC", value or "")
    if not name or len(name) > 180:
        raise HTTPException(status_code=400, detail="文件名为空或过长")
    if "/" in name or "\\" in name or PurePosixPath(name).name != name:
        raise HTTPException(status_code=400, detail="文件名不能包含目录")
    if (
        name.endswith((" ", "."))
        or any(ord(char) < 32 for char in name)
        or any(char in _WINDOWS_INVALID_CHARACTERS for char in name)
    ):
        raise HTTPException(status_code=400, detail="文件名包含无效字符")
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise HTTPException(status_code=400, detail="文件名是系统保留名称")
    if Path(name).suffix.lower() not in _UPLOAD_SUFFIXES:
        raise HTTPException(status_code=400, detail="仅支持 CSV、TSV 和 XLSX 文件")
    return name


def _validate_xlsx(content: bytes) -> None:
    """Reject renamed files, encrypted archives and oversized OOXML packages."""

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise HTTPException(status_code=400, detail="XLSX 文件结构无效")
            if len(entries) > _MAX_XLSX_ENTRIES:
                raise HTTPException(status_code=400, detail="XLSX 文件条目过多")
            if any(entry.flag_bits & 0x1 for entry in entries):
                raise HTTPException(status_code=400, detail="不支持加密 XLSX 文件")
            if sum(entry.file_size for entry in entries) > _MAX_XLSX_UNCOMPRESSED_BYTES:
                raise HTTPException(status_code=400, detail="XLSX 解压后大小超过限制")
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="XLSX 文件结构无效") from exc

    # Parse workbook relationships without loading formulas, macros or links.
    try:
        workbook = load_workbook(
            io.BytesIO(content),
            read_only=True,
            data_only=True,
            keep_links=False,
        )
        workbook.close()
    except (
        InvalidFileException,
        KeyError,
        OSError,
        ParseError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
    ) as exc:
        raise HTTPException(status_code=400, detail="XLSX 文件结构无效") from exc


async def _read_upload(upload: UploadFile) -> bytes:
    """Read one bounded upload without trusting the client-reported size."""

    content = bytearray()
    while chunk := await upload.read(1024 * 1024):
        content.extend(chunk)
        if len(content) > _MAX_UPLOAD_FILE_BYTES:
            raise HTTPException(status_code=413, detail="单个文件不能超过 50 MB")
    return bytes(content)


async def _require_owned_supervisor_thread(
    thread_id: str,
    user_id: str,
    client,
) -> None:
    """Hide foreign threads and reject uploads to worker threads."""

    if await database.get_thread_owner(thread_id) != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        thread = await client.threads.get(thread_id=thread_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    metadata = thread.get("metadata") if isinstance(thread, dict) else None
    if not isinstance(metadata, dict) or metadata.get("graph_id") != "supervisor":
        raise HTTPException(status_code=400, detail="只能向 Supervisor 会话上传文件")


def _trace_upload_inputs(inputs: dict[str, object]) -> dict[str, object]:
    prepared = inputs.get("prepared") or []
    return {
        "thread_id": inputs.get("thread_id"),
        "user_hash": hashlib.sha256(
            str(inputs.get("user_id") or "").encode("utf-8")
        ).hexdigest()[:12],
        "files": [
            {
                "name": name,
                "format": Path(name).suffix.lower().removeprefix("."),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in prepared
        ],
    }


@traceable(name="file.upload", run_type="tool", process_inputs=_trace_upload_inputs)
async def _upload_to_supervisor_workspace(
    thread_id: str,
    user_id: str,
    prepared: list[tuple[str, bytes]],
) -> None:
    """Upload validated files and persist the exact bytes without tracing content."""

    await sandbox_manager.SANDBOX_MANAGER.ensure(
        thread_id,
        component="supervisor",
        network_enabled=True,
        user_id=user_id,
    )
    paths = [(f"/workspace/input/{name}", content) for name, content in prepared]
    try:
        await sandbox_manager.SANDBOX_MANAGER.upload_workspace_files(
            thread_id,
            paths,
            component="supervisor",
            persist=True,
        )
    except Exception:
        # A multi-file backend call may partially succeed. Remove all batch paths.
        for path, _content in paths:
            try:
                await sandbox_manager.SANDBOX_MANAGER.delete_workspace_file(
                    thread_id,
                    path,
                    component="supervisor",
                )
            except Exception:
                logger.warning("回滚沙箱上传文件失败：%s", path, exc_info=True)
        raise


def _download_path(root: Path, virtual_path: str) -> Path:
    """Translate artifact validation failures into API responses."""

    try:
        return resolve_download_path(root, virtual_path)
    except ArtifactError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _markdown_bundle(root: Path, virtual_path: str) -> tuple[bytes, str]:
    """Translate shared bundle validation failures into API responses."""

    try:
        return build_markdown_bundle(root, virtual_path)
    except ArtifactError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _user_payload(user: database.UserRecord) -> dict[str, object]:
    return {
        "id": user.id,
        "username": user.username,
        "is_default": user.is_system,
    }


async def _issue_token(user: database.UserRecord) -> dict[str, object]:
    return {
        "token": await database.create_login_session(user.id),
        "user": _user_payload(user),
    }


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await database.ensure_schema()
    memory_worker = await start_memory_worker()
    # Loopback Agent Protocol calls query checkpoints and child runs without
    # creating a Supervisor model run.
    agent_client = get_client(url=None, api_key=None)
    _app.state.agent_client = agent_client
    try:
        yield
    finally:
        await agent_client.aclose()
        if memory_worker is not None:
            await memory_worker.stop()
        await database.close_database()


app = FastAPI(title="深研账户 API", lifespan=lifespan)


@app.post("/auth/register", status_code=201)
async def register(payload: RegisterRequest, request: Request) -> dict[str, object]:
    settings = get_settings()
    await _enforce_rate_limit(
        scope="auth_register",
        raw_key=_client_ip(request),
        limit=settings.auth_register_limit,
        window_seconds=settings.auth_register_window_seconds,
        detail="注册请求过于频繁，请稍后再试",
    )
    username = payload.username.strip()
    if not _USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(
            status_code=422,
            detail="用户名须为 3–32 位字母、数字、下划线或连字符",
        )
    if payload.password != payload.confirm_password:
        raise HTTPException(status_code=422, detail="两次输入的密码不一致")

    password_hash = await asyncio.to_thread(_PASSWORD_HASHER.hash, payload.password)
    try:
        user = await database.create_user(username, password_hash)
    except database.UsernameExistsError as exc:
        raise HTTPException(status_code=409, detail="用户名已存在") from exc
    return await _issue_token(user)


@app.post("/auth/login")
async def login(payload: LoginRequest, request: Request) -> dict[str, object]:
    settings = get_settings()
    username = payload.username.strip()
    login_key = f"{_client_ip(request)}\0{username.casefold()}"
    await _enforce_rate_limit(
        scope="auth_login",
        raw_key=login_key,
        limit=settings.auth_login_failure_limit,
        window_seconds=settings.auth_login_window_seconds,
        detail="登录尝试过于频繁，请稍后再试",
    )

    user = await database.get_user_by_username(username)
    if user is None or user.is_system or not user.password_hash:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    try:
        await asyncio.to_thread(
            _PASSWORD_HASHER.verify,
            user.password_hash,
            payload.password,
        )
    except (VerificationError, InvalidHashError) as exc:
        raise HTTPException(status_code=401, detail="用户名或密码错误") from exc
    try:
        await database.clear_rate_limit("auth_login", login_key)
    except Exception as exc:
        logger.exception("清除登录限流记录失败")
        raise HTTPException(
            status_code=503,
            detail="请求保护服务暂不可用，请稍后重试",
        ) from exc
    return await _issue_token(user)


@app.post("/auth/logout")
async def logout(authorization: str | None = Header(default=None)) -> dict[str, str]:
    token = bearer_token(authorization)
    if token is not None:
        await database.revoke_login_session(token)
    return {"status": "logged_out"}


@app.get("/auth/me")
async def current_user(
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    token = bearer_token(authorization)
    if token is None:
        if get_settings().app_env == "production":
            raise HTTPException(
                status_code=401,
                detail="请先登录",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return {
            "user": {
                "id": database.DEFAULT_USER_ID,
                "username": "默认账户",
                "is_default": True,
            }
        }
    user = await database.resolve_login_session(token)
    if user is None:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    return {"user": _user_payload(user)}


@app.delete("/memories/user")
async def clear_current_user_memory(
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    """Clear only the authenticated user's learned preferences and feedback."""

    user_id = await _authenticated_user_id(authorization)
    identity_hash = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    try:
        async with asyncio.timeout(5):
            cancelled_jobs = await MEMORY_QUEUE.clear_user_memory(identity_hash)
    except Exception as exc:
        logger.exception("清除用户记忆失败")
        raise HTTPException(
            status_code=503,
            detail="记忆服务暂不可用，请稍后重试",
        ) from exc
    return {"status": "cleared", "cancelled_jobs": cancelled_jobs}


@app.get("/memories/settings")
async def get_current_memory_settings(
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    """Return optional memory-contribution settings for the authenticated user."""

    user_id = await _authenticated_user_id(authorization)
    identity_hash = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    try:
        async with asyncio.timeout(5):
            settings = await MEMORY_QUEUE.get_memory_settings(identity_hash)
    except Exception as exc:
        logger.exception("读取用户记忆设置失败")
        raise HTTPException(
            status_code=503,
            detail="记忆设置服务暂不可用，请稍后重试",
        ) from exc
    return {
        "failure_lesson_saving_enabled": settings.failure_lesson_saving_enabled,
    }


@app.patch("/memories/settings")
async def update_current_memory_settings(
    request: MemorySettingsRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    """Persist the failure-lesson switch and cancel disabled pending reviews."""

    user_id = await _authenticated_user_id(authorization)
    identity_hash = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    try:
        async with asyncio.timeout(5):
            settings, cancelled_jobs = await MEMORY_QUEUE.set_failure_lesson_saving(
                identity_hash,
                enabled=request.failure_lesson_saving_enabled,
            )
    except Exception as exc:
        logger.exception("更新用户记忆设置失败")
        raise HTTPException(
            status_code=503,
            detail="记忆设置服务暂不可用，请稍后重试",
        ) from exc
    return {
        "failure_lesson_saving_enabled": settings.failure_lesson_saving_enabled,
        "cancelled_jobs": cancelled_jobs,
    }


@app.get("/artifacts/{thread_id}")
async def list_artifacts(
    thread_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    """List downloadable Supervisor files owned by the current user."""

    user_id = await _authenticated_user_id(authorization)
    if await database.get_thread_owner(thread_id) != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    root = sandbox_manager.SANDBOX_MANAGER.local_workspace_path(
        thread_id,
        "supervisor",
        user_id=user_id,
    )
    return {"artifacts": await asyncio.to_thread(_workspace_artifacts, root)}


@app.get("/files/{thread_id}")
async def list_uploaded_files(
    thread_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    """List local table inputs belonging to one Supervisor conversation."""

    user_id = await _authenticated_user_id(authorization)
    await _require_owned_supervisor_thread(
        thread_id,
        user_id,
        request.app.state.agent_client,
    )
    root = sandbox_manager.SANDBOX_MANAGER.local_workspace_path(
        thread_id,
        "supervisor",
        user_id=user_id,
    )
    return {"files": await asyncio.to_thread(_uploaded_files, root)}


@app.post("/files/{thread_id}")
async def upload_files(
    thread_id: str,
    request: Request,
    files: Annotated[list[UploadFile], File(description="待分析的表格文件")],
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    """Upload a validated batch into the owned Supervisor sandbox."""

    user_id = await _authenticated_user_id(authorization)
    await _require_owned_supervisor_thread(
        thread_id,
        user_id,
        request.app.state.agent_client,
    )
    if not files or len(files) > _MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail="一次最多上传 5 个文件")

    root = sandbox_manager.SANDBOX_MANAGER.local_workspace_path(
        thread_id,
        "supervisor",
        user_id=user_id,
    )
    existing = await asyncio.to_thread(_uploaded_files, root)
    existing_names = {str(item["name"]).casefold() for item in existing}
    if len(existing) + len(files) > _MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail="当前会话最多保留 5 个上传文件")

    prepared: list[tuple[str, bytes]] = []
    batch_names: set[str] = set()
    for upload in files:
        name = _validated_upload_name(upload.filename)
        folded = name.casefold()
        if folded in existing_names or folded in batch_names:
            raise HTTPException(status_code=409, detail=f"文件已存在：{name}")
        content = await _read_upload(upload)
        if Path(name).suffix.lower() == ".xlsx":
            await asyncio.to_thread(_validate_xlsx, content)
        prepared.append((name, content))
        batch_names.add(folded)

    existing_size = sum(int(item["size"]) for item in existing)
    if existing_size + sum(len(content) for _name, content in prepared) > _MAX_UPLOAD_TOTAL_BYTES:
        raise HTTPException(status_code=413, detail="当前会话上传文件总计不能超过 100 MB")
    try:
        await _upload_to_supervisor_workspace(thread_id, user_id, prepared)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"文件写入沙箱失败：{exc}") from exc

    return {
        "files": [
            {
                "name": name,
                "path": f"/workspace/input/{name}",
                "size": len(content),
                "media_type": _UPLOAD_MEDIA_TYPES[Path(name).suffix.lower()],
            }
            for name, content in prepared
        ]
    }


@app.delete("/files/{thread_id}")
async def delete_uploaded_file(
    thread_id: str,
    request: Request,
    path: str = Query(..., min_length=1),
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    """Delete one owned input file from the snapshot and active sandbox."""

    user_id = await _authenticated_user_id(authorization)
    await _require_owned_supervisor_thread(
        thread_id,
        user_id,
        request.app.state.agent_client,
    )
    try:
        relative = sandbox_manager.workspace_relative_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if len(relative.parts) != 2 or relative.parts[0] != "input":
        raise HTTPException(status_code=400, detail="只能删除 /workspace/input 下的文件")

    root = sandbox_manager.SANDBOX_MANAGER.local_workspace_path(
        thread_id,
        "supervisor",
        user_id=user_id,
    )
    existing = await asyncio.to_thread(_uploaded_files, root)
    if path not in {str(item["path"]) for item in existing}:
        raise HTTPException(status_code=404, detail="上传文件不存在")
    await sandbox_manager.SANDBOX_MANAGER.ensure(
        thread_id,
        component="supervisor",
        network_enabled=True,
        user_id=user_id,
    )
    try:
        await sandbox_manager.SANDBOX_MANAGER.delete_workspace_file(
            thread_id,
            path,
            component="supervisor",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"删除上传文件失败：{exc}") from exc
    return {"status": "deleted", "path": path}


@app.get("/artifacts/{thread_id}/download")
async def download_artifact(
    thread_id: str,
    path: str = Query(..., min_length=1),
    authorization: str | None = Header(default=None),
) -> FileResponse:
    """Download one authenticated file from a Supervisor workspace snapshot."""

    user_id = await _authenticated_user_id(authorization)
    if await database.get_thread_owner(thread_id) != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    root = sandbox_manager.SANDBOX_MANAGER.local_workspace_path(
        thread_id,
        "supervisor",
        user_id=user_id,
    )
    target = await asyncio.to_thread(_download_path, root, path)
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, filename=target.name)


@app.get("/artifacts/{thread_id}/bundle")
async def download_markdown_bundle(
    thread_id: str,
    path: str = Query(..., min_length=1),
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    """Download a Markdown report together with its local images as a ZIP."""

    user_id = await _authenticated_user_id(authorization)
    if await database.get_thread_owner(thread_id) != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    root = sandbox_manager.SANDBOX_MANAGER.local_workspace_path(
        thread_id,
        "supervisor",
        user_id=user_id,
    )
    content, filename = await asyncio.to_thread(_markdown_bundle, root, path)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(io.BytesIO(content), media_type="application/zip", headers=headers)


@app.post("/async-tasks/status")
async def async_task_status(
    payload: AsyncTaskStatusRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    """Return live child-run statuses without invoking the Supervisor model."""

    token = bearer_token(authorization)
    if token is None:
        if get_settings().app_env == "production":
            raise HTTPException(
                status_code=401,
                detail="请先登录",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user_id = database.DEFAULT_USER_ID
    else:
        user = await database.resolve_login_session(token)
        if user is None:
            raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
        user_id = user.id

    owner = await database.get_thread_owner(payload.thread_id)
    if owner != user_id:
        # Do not reveal whether a thread owned by another user exists.
        raise HTTPException(status_code=404, detail="会话不存在")

    client = request.app.state.agent_client
    try:
        parent_thread = await client.threads.get(thread_id=payload.thread_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="暂时无法读取后台任务") from exc

    values = parent_thread.get("values") or {}
    tracked = values.get("async_tasks") if isinstance(values, dict) else None
    if not isinstance(tracked, dict):
        return {"tasks": []}

    async def inspect_task(task_id: str, raw_task: object) -> dict[str, object]:
        if not isinstance(raw_task, dict):
            return {
                "task_id": task_id,
                "status": "error",
                "error_summary": "后台任务记录无效。",
            }

        task = dict(raw_task)
        task["task_id"] = str(task.get("task_id") or task_id)
        cached_status = str(task.get("status") or "running")
        if cached_status in {"success", "cancelled"}:
            return task

        child_thread_id = str(task.get("thread_id") or task["task_id"])
        run_id = str(task.get("run_id") or "")
        if not run_id:
            if cached_status in _FAILED_TASK_STATUSES:
                task["error_summary"] = _sanitized_task_error(cached_status)
            else:
                task["poll_error"] = "任务缺少 run_id"
            return task
        try:
            run = await client.runs.get(thread_id=child_thread_id, run_id=run_id)
            live_status = str(run.get("status") or cached_status)
            task["status"] = live_status
            error_summary = _sanitized_task_error(live_status, run.get("error"))
            if error_summary is not None:
                task["error_summary"] = error_summary
        except Exception:  # noqa: BLE001 - keep the last known state on polling errors.
            if cached_status in _FAILED_TASK_STATUSES:
                task["error_summary"] = _sanitized_task_error(cached_status)
            else:
                task["poll_error"] = "暂时无法获取实时状态"
        return task

    tasks = await asyncio.gather(
        *(inspect_task(str(task_id), raw_task) for task_id, raw_task in tracked.items())
    )
    return {"tasks": tasks}
