"""Custom FastAPI routes mounted by the LangGraph Agent Server."""

from __future__ import annotations

import asyncio
import hashlib
import io
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
from deep_data_research_agent.auth import bearer_token
from deep_data_research_agent.interaction_tools import DOWNLOADABLE_SUFFIXES
from deep_data_research_agent.memory import start_memory_worker

_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")
_PASSWORD_HASHER = PasswordHasher()
logger = logging.getLogger(__name__)
_FAILED_TASK_STATUSES = frozenset({"error", "timeout", "interrupted"})
_UPLOAD_SUFFIXES = frozenset({".csv", ".tsv", ".xlsx"})
_ARTIFACT_CARD_SUFFIXES = frozenset({".md", ".pdf", ".zip"})
_BUNDLE_COMPANION_SUFFIXES = frozenset({".csv", ".json", ".png", ".tsv", ".xlsx"})
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
_MARKDOWN_IMAGE_PATTERN = re.compile(
    r"!\[[^\]]*\]\(\s*(?:<(?P<angle>[^>]+)>|(?P<plain>[^\s)]+))"
    r"(?:\s+['\"][^'\"]*['\"])?\s*\)"
)
_HTML_IMAGE_PATTERN = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*(?P<quote>['\"])(?P<src>.*?)(?P=quote)[^>]*>",
    re.IGNORECASE,
)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)


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
        return database.DEFAULT_USER_ID
    user = await database.resolve_login_session(token)
    if user is None:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    return user.id


def _workspace_artifacts(root: Path) -> list[dict[str, object]]:
    """List user-facing artifacts without following links outside the snapshot."""

    if not root.is_dir():
        return []
    resolved_root = root.resolve()
    artifacts: list[dict[str, object]] = []
    for candidate in resolved_root.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(resolved_root):
            continue
        relative = resolved.relative_to(resolved_root)
        if relative.parts[0] in {"input", "profile", "raw", "scripts"}:
            continue
        is_output = relative.parts[0] in {"charts", "output"}
        is_report = "report" in resolved.stem.lower()
        if not is_output and not is_report:
            continue
        # 图片与表格作为报告附件进入 ZIP，不在产物卡中逐项占据位置。
        if resolved.suffix.lower() not in _ARTIFACT_CARD_SUFFIXES:
            continue
        mime_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        artifacts.append(
            {
                "path": f"/workspace/{relative.as_posix()}",
                "filename": resolved.name,
                "size": resolved.stat().st_size,
                "mime_type": mime_type,
            }
        )
    report_priority = {
        "/workspace/output/final_report.pdf": 0,
        "/workspace/output/final_report.md": 1,
        "/workspace/final_report.pdf": 2,
        "/workspace/final_report.md": 3,
    }
    return sorted(
        artifacts,
        key=lambda item: (report_priority.get(str(item["path"]), 4), str(item["path"])),
    )


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
    """Resolve one virtual workspace path while rejecting traversal and links."""

    try:
        relative = sandbox_manager.workspace_relative_path(virtual_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if relative.suffix.lower() not in DOWNLOADABLE_SUFFIXES:
        raise HTTPException(status_code=400, detail="下载文件类型不受支持")

    resolved_root = root.resolve()
    candidate = resolved_root / Path(*relative.parts)
    if candidate.is_symlink():
        raise HTTPException(status_code=404, detail="文件不存在")
    target = candidate.resolve()
    if not target.is_relative_to(resolved_root):
        raise HTTPException(status_code=400, detail="下载路径越过工作区")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return target


def _markdown_bundle(root: Path, virtual_path: str) -> tuple[bytes, str]:
    """Build a Markdown ZIP containing its images and companion data files.

    The report is placed at the ZIP root. Relative image paths keep their
    original layout. CSV, TSV, XLSX, JSON and PNG files beside the report are
    bundled recursively even when they are only listed as supporting outputs.
    """

    report_path = _download_path(root, virtual_path)
    if report_path.suffix.lower() != ".md":
        raise HTTPException(status_code=400, detail="只有 Markdown 报告支持图片打包下载")

    try:
        markdown = report_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=409, detail="Markdown 报告不是 UTF-8 编码") from exc

    sources = [
        match.group("angle") or match.group("plain") or ""
        for match in _MARKDOWN_IMAGE_PATTERN.finditer(markdown)
    ]
    sources.extend(match.group("src") for match in _HTML_IMAGE_PATTERN.finditer(markdown))

    resolved_root = root.resolve()
    report_parent = report_path.parent.resolve()
    assets: dict[str, Path] = {}
    included_files: set[Path] = set()
    rewrites: dict[str, str] = {}
    for raw_source in sources:
        source = raw_source.strip().replace("\\", "/")
        if not source or source.startswith(("http://", "https://", "data:", "blob:", "#")):
            continue
        # Queries and fragments are not part of a local filesystem path.
        source_path = source.split("#", 1)[0].split("?", 1)[0]
        pure = PurePosixPath(source_path)
        if ".." in pure.parts:
            raise HTTPException(status_code=409, detail=f"报告图片路径不安全：{source}")
        if source_path.startswith("/workspace/"):
            relative = sandbox_manager.workspace_relative_path(source_path)
            archive_path = PurePosixPath(*relative.parts).as_posix()
            candidate = resolved_root / Path(*relative.parts)
            # The report is extracted at the ZIP root, where /workspace does not exist.
            rewrites[raw_source] = archive_path
        elif pure.is_absolute():
            raise HTTPException(status_code=409, detail=f"报告图片必须位于工作区：{source}")
        else:
            archive_path = pure.as_posix()
            candidate = report_parent / Path(*pure.parts)

        if candidate.is_symlink():
            raise HTTPException(status_code=409, detail=f"报告图片不能是符号链接：{source}")
        image_path = candidate.resolve()
        if not image_path.is_relative_to(resolved_root):
            raise HTTPException(status_code=409, detail=f"报告图片越过工作区：{source}")
        if not image_path.is_file():
            raise HTTPException(status_code=409, detail=f"报告引用的图片不存在：{source}")
        assets[archive_path] = image_path
        included_files.add(image_path)

    # data-analyst 会把图表和指标表放在主报告目录树内；统一随报告打包，
    # 这样前端无需把每个辅助文件展示成独立下载项。
    for candidate in report_parent.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if candidate.suffix.lower() not in _BUNDLE_COMPANION_SUFFIXES:
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(resolved_root) or resolved in included_files:
            continue
        archive_path = PurePosixPath(
            *resolved.relative_to(report_parent).parts
        ).as_posix()
        assets[archive_path] = resolved
        included_files.add(resolved)

    bundled_markdown = markdown
    for original, replacement in rewrites.items():
        bundled_markdown = bundled_markdown.replace(original, replacement)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(report_path.name, bundled_markdown.encode("utf-8"))
        for archive_path, image_path in sorted(assets.items()):
            archive.write(image_path, archive_path)
    return buffer.getvalue(), f"{report_path.stem}-bundle.zip"


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
async def register(payload: RegisterRequest) -> dict[str, object]:
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
async def login(payload: LoginRequest) -> dict[str, object]:
    user = await database.get_user_by_username(payload.username.strip())
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
