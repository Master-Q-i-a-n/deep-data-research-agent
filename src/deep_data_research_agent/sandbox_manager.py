"""OpenSandbox lifecycle and workspace synchronization for worker graphs."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from deepagents.backends.protocol import ExecuteResponse
from deepagents_opensandbox import OpensandboxBackend
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from opensandbox import SandboxSync
from opensandbox.config import ConnectionConfigSync
from opensandbox.exceptions import SandboxException
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import NetworkPolicy

from deep_data_research_agent.config import Settings, get_settings

_SAFE_THREAD_ID = re.compile(r"[^A-Za-z0-9_.-]")
_WORKSPACE_ROOT = PurePosixPath("/workspace")
# OpenSandbox 的 Docker 冷启动会同时创建容器、端口和可选 egress sidecar。
# 整个进程统一串行创建，避免不同 Agent 任务同时争用这些资源。
_SANDBOX_CREATE_SEMAPHORE = asyncio.Semaphore(1)
_SANDBOX_CREATE_RETRY_DELAYS = (1.0, 3.0)

logger = logging.getLogger(__name__)


def sanitize_thread_id(value: Any) -> str:
    """Return a filesystem-safe representation of a LangGraph thread ID."""

    return _SAFE_THREAD_ID.sub("_", str(value)) or "local"


def thread_id_from_config(config: dict[str, Any] | None) -> str:
    """Read the current task ID from a LangGraph runnable config."""

    value = (config or {}).get("configurable", {}).get("thread_id", "local")
    return sanitize_thread_id(value)


def thread_id_from_runtime(runtime: Any) -> str:
    """Read the current task ID from a LangGraph or tool runtime."""

    # Backend factories receive LangGraph Runtime, where request metadata lives
    # on execution_info rather than config. ToolRuntime still exposes config.
    execution_info = getattr(runtime, "execution_info", None)
    thread_id = getattr(execution_info, "thread_id", None)
    if thread_id:
        return sanitize_thread_id(thread_id)
    return thread_id_from_config(getattr(runtime, "config", {}) or {})


def _trace_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Keep SDK clients, local paths, and file contents out of LangSmith inputs."""

    manager = inputs.get("self")
    settings = getattr(manager, "_settings", None)
    return {
        "thread_id": inputs.get("thread_id"),
        "component": inputs.get("component", "crawl-worker"),
        "image": getattr(settings, "open_sandbox_image", None),
    }


def _trace_backend_output(
    backend: OpensandboxBackend | None,
) -> dict[str, str]:
    # LangSmith may invoke the output processor with None when the span fails.
    return {"sandbox_id": backend.id} if backend is not None else {}


def _trace_file_count(output: int) -> dict[str, int]:
    return {"file_count": output}


def _trace_export_output(
    output: list[dict[str, Any]] | None,
) -> dict[str, int]:
    """Keep artifact contents out of traces while exposing the export count."""

    return {"file_count": len(output or [])}


def _trace_sync_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "thread_id": inputs.get("thread_id"),
        "component": inputs.get("component"),
        "root": inputs.get("root"),
        "file_count": len(inputs.get("files") or []),
    }


def _add_trace_metadata(
    *,
    thread_id: str,
    sandbox_id: str,
    image: str,
) -> None:
    run = get_current_run_tree()
    if run is not None:
        run.add_metadata(
            {
                "thread_id": thread_id,
                "sandbox_id": sandbox_id,
                "image": image,
            }
        )


def _workspace_relative(path: str) -> PurePosixPath:
    """Validate a path and return its location relative to ``/workspace``."""

    candidate = PurePosixPath(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(_WORKSPACE_ROOT)
        except ValueError as exc:
            raise ValueError(f"沙箱文件必须位于 /workspace：{path}") from exc

    if not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"无效的沙箱工作区路径：{path}")
    return candidate


def _sandbox_path(relative: PurePosixPath) -> str:
    return f"/workspace/{relative.as_posix()}"


def _load_local_workspace(root: Path) -> list[tuple[str, bytes]]:
    """Read a successful local snapshot in a worker thread."""

    if not root.is_dir():
        return []

    files: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            relative = PurePosixPath(path.relative_to(root).as_posix())
            files.append((_sandbox_path(relative), path.read_bytes()))
    return files


def _write_local_workspace(
    root: Path,
    files: list[tuple[PurePosixPath, bytes]],
) -> None:
    """Merge a sandbox snapshot into the local artifact directory."""

    root.mkdir(parents=True, exist_ok=True)
    resolved_root = root.resolve()
    for relative, content in files:
        target = (root.joinpath(*relative.parts)).resolve()
        if not target.is_relative_to(resolved_root):
            raise ValueError(f"拒绝导出工作区之外的文件：{relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


class _LinePreservingOpensandboxBackend(OpensandboxBackend):
    """Preserve boundaries between OpenSandbox stdout/stderr messages.

    ``deepagents-opensandbox==1.0.2`` concatenates output messages without a
    delimiter. DeepAgents' ``ls`` and ``glob`` emit one JSON object per line,
    so concatenation makes a valid multi-line response impossible to parse.
    """

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        opts = RunCommandOpts()
        if timeout is not None:
            opts = RunCommandOpts(timeout=timedelta(seconds=timeout))

        execution = self._sandbox.commands.run(command, opts=opts)
        stdout = "\n".join(
            message.text.rstrip("\n") for message in execution.logs.stdout
        )
        stderr = "\n".join(
            message.text.rstrip("\n") for message in execution.logs.stderr
        )
        output = stdout
        if stderr:
            output = f"{output}\n{stderr}" if output else stderr

        exit_code: int | None = None
        if execution.id:
            status = self._sandbox.commands.get_command_status(execution.id)
            exit_code = status.exit_code
        return ExecuteResponse(
            output=output,
            exit_code=exit_code,
            truncated=False,
        )


@dataclass(slots=True)
class _SandboxHandle:
    sandbox: SandboxSync
    backend: OpensandboxBackend
    component: str = "crawl-worker"
    network_enabled: bool = False
    base_distributions: frozenset[str] = frozenset()


class SandboxManager:
    """Manage one OpenSandbox instance for each worker component and thread."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        artifact_root: Path | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        configured_root = artifact_root or self._settings.artifact_root
        # Resolve once during module initialization, outside ASGI request handling.
        self._artifact_root = configured_root.expanduser().resolve()
        self._handles: dict[tuple[str, str, str], _SandboxHandle] = {}
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._thread_users: dict[str, str] = {}

    def _user_for_thread(self, thread_id: str, user_id: str | None = None) -> str:
        """Bind a globally unique Agent Protocol thread to one local user path."""

        thread_id = sanitize_thread_id(thread_id)
        normalized_user = str(user_id or "").strip()
        existing = self._thread_users.get(thread_id)
        if normalized_user:
            if existing is not None and existing != normalized_user:
                raise RuntimeError("同一 thread_id 不能绑定到不同用户")
            self._thread_users[thread_id] = normalized_user
            return normalized_user
        if existing:
            return existing
        fallback = self._settings.local_dev_user_id.strip()
        if not fallback:
            raise RuntimeError(f"任务 {thread_id} 尚未绑定用户")
        self._thread_users[thread_id] = fallback
        return fallback

    def _local_workspace(self, thread_id: str, component: str) -> Path:
        return (
            self._artifact_root
            / self._user_for_thread(thread_id)
            / "jobs"
            / sanitize_thread_id(thread_id)
            / component
            / "workspace"
        )

    def _key(
        self,
        thread_id: str,
        component: str,
        user_id: str | None = None,
    ) -> tuple[str, str, str]:
        normalized_thread = sanitize_thread_id(thread_id)
        return (
            self._user_for_thread(normalized_thread, user_id),
            component,
            normalized_thread,
        )

    def _lock_for(self, thread_id: str, component: str) -> asyncio.Lock:
        key = self._key(thread_id, component)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _validate_settings(self) -> None:
        missing: list[str] = []
        if not self._settings.open_sandbox_domain:
            missing.append("OPEN_SANDBOX_DOMAIN")
        if not self._settings.open_sandbox_api_key:
            missing.append("OPEN_SANDBOX_API_KEY")
        if not self._settings.open_sandbox_image:
            missing.append("OPEN_SANDBOX_IMAGE")
        if missing:
            names = "、".join(missing)
            raise RuntimeError(f"OpenSandbox 配置不完整，请在 .env 中配置：{names}")

    def _create_sandbox_sync(
        self,
        thread_id: str,
        component: str,
        network_enabled: bool,
    ) -> SandboxSync:
        """Create a sandbox; callers must offload this blocking SDK method."""

        self._validate_settings()
        connection = ConnectionConfigSync(
            api_key=self._settings.open_sandbox_api_key,
            domain=self._settings.open_sandbox_domain,
            protocol=self._settings.open_sandbox_protocol,
            use_server_proxy=self._settings.open_sandbox_use_server_proxy,
            # Container creation can legitimately exceed the SDK's 30-second
            # default on a cold Docker host.
            request_timeout=timedelta(seconds=120),
        )
        return SandboxSync.create(
            self._settings.open_sandbox_image,
            timeout=timedelta(
                seconds=self._settings.open_sandbox_timeout_seconds,
            ),
            metadata={
                "thread_id": thread_id,
                "component": component,
            },
            # Supervisor sandbox is networked so Skill download/install works
            # via execute; crawl-worker remains network-isolated.
            network_policy=NetworkPolicy(
                defaultAction="allow" if network_enabled else "deny",
                egress=[],
            ),
            connection_config=connection,
        )

    async def _ensure_directories(
        self,
        handle: _SandboxHandle,
        directories: set[str],
    ) -> None:
        if not directories:
            return
        command = "mkdir -p " + " ".join(
            shlex.quote(path) for path in sorted(directories)
        )
        result = await handle.backend.aexecute(command)
        if result.exit_code not in {None, 0}:
            detail = result.output.strip() or f"退出码 {result.exit_code}"
            raise RuntimeError(f"无法创建沙箱工作目录：{detail}")

    async def _capture_base_distributions(
        self,
        handle: _SandboxHandle,
    ) -> frozenset[str]:
        """Record packages present before any restored or candidate code runs."""

        code = (
            "import importlib.metadata as m, json, re; "
            "normalize=lambda value: re.sub(r'[-_.]+', '-', value).lower(); "
            "print(json.dumps(sorted({normalize(d.metadata['Name']) "
            "for d in m.distributions() if d.metadata['Name']})))"
        )
        result = await handle.backend.aexecute(
            "python -c " + shlex.quote(code),
        )
        if result.exit_code not in {None, 0}:
            detail = result.output.strip() or f"退出码 {result.exit_code}"
            raise RuntimeError(f"无法读取沙箱基础依赖：{detail}")
        try:
            values = json.loads(result.output.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError("沙箱基础依赖检查没有返回有效结果") from exc
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise TypeError("沙箱基础依赖检查结果格式无效")
        return frozenset(values)

    async def _create_handle(
        self,
        thread_id: str,
        component: str = "crawl-worker",
        network_enabled: bool = False,
    ) -> _SandboxHandle:
        # 重试期间继续持有进程级信号量，给 Docker 和 egress sidecar 留出恢复时间，
        # 同时保证所有 SandboxSync.create() 调用都不会交错执行。
        async with _SANDBOX_CREATE_SEMAPHORE:
            for attempt in range(len(_SANDBOX_CREATE_RETRY_DELAYS) + 1):
                try:
                    sandbox = await asyncio.to_thread(
                        self._create_sandbox_sync,
                        thread_id,
                        component,
                        network_enabled,
                    )
                    break
                except Exception as exc:
                    error_code = str(
                        getattr(getattr(exc, "error", None), "code", "")
                    )
                    message = str(exc)
                    retryable = (
                        error_code.endswith("SANDBOX_START_FAILED")
                        or error_code == "READY_TIMEOUT"
                        or "Egress sidecar container failed to start" in message
                    )
                    if (
                        not retryable
                        or attempt >= len(_SANDBOX_CREATE_RETRY_DELAYS)
                    ):
                        raise RuntimeError(
                            f"无法为任务 {thread_id} 创建 OpenSandbox：{exc}"
                        ) from exc

                    delay = _SANDBOX_CREATE_RETRY_DELAYS[attempt]
                    logger.warning(
                        "OpenSandbox 冷启动失败，%.0f 秒后重试（任务=%s，组件=%s，"
                        "第 %d/%d 次重试）：%s",
                        delay,
                        thread_id,
                        component,
                        attempt + 1,
                        len(_SANDBOX_CREATE_RETRY_DELAYS),
                        message,
                    )
                    await asyncio.sleep(delay)

        handle = _SandboxHandle(
            sandbox=sandbox,
            backend=_LinePreservingOpensandboxBackend(sandbox=sandbox),
            component=component,
            network_enabled=network_enabled,
        )
        try:
            await self._ensure_directories(handle, {"/workspace"})
            handle.base_distributions = await self._capture_base_distributions(
                handle
            )
            await self.restore_workspace(
                thread_id,
                handle,
                component=component,
            )
        except Exception:
            # Creation succeeded but initialization failed; avoid leaking a container.
            await asyncio.to_thread(sandbox.destroy)
            raise
        return handle

    @traceable(
        name="sandbox.ensure",
        run_type="chain",
        process_inputs=_trace_inputs,
        process_outputs=_trace_backend_output,
    )
    async def ensure(
        self,
        thread_id: str,
        *,
        component: str = "crawl-worker",
        network_enabled: bool = False,
        user_id: str | None = None,
    ) -> OpensandboxBackend:
        """Create, renew, or replace the sandbox for a task."""

        thread_id = sanitize_thread_id(thread_id)
        key = self._key(thread_id, component, user_id)
        async with self._lock_for(thread_id, component):
            handle = self._handles.get(key)
            if handle is not None:
                healthy = await asyncio.to_thread(handle.sandbox.is_healthy)
                if healthy:
                    try:
                        await asyncio.to_thread(
                            handle.sandbox.renew,
                            timedelta(
                                seconds=self._settings.open_sandbox_timeout_seconds,
                            ),
                        )
                    except SandboxException:
                        healthy = False
                if healthy:
                    _add_trace_metadata(
                        thread_id=thread_id,
                        sandbox_id=handle.backend.id,
                        image=self._settings.open_sandbox_image,
                    )
                    return handle.backend

                self._handles.pop(key, None)
                await asyncio.to_thread(handle.sandbox.close)

            handle = await self._create_handle(
                thread_id,
                component,
                network_enabled,
            )
            self._handles[key] = handle
            _add_trace_metadata(
                thread_id=thread_id,
                sandbox_id=handle.backend.id,
                image=self._settings.open_sandbox_image,
            )
            return handle.backend

    def get_backend(
        self,
        thread_id: str,
        *,
        component: str = "crawl-worker",
    ) -> OpensandboxBackend:
        """Return the already initialized backend for a worker request."""

        thread_id = sanitize_thread_id(thread_id)
        handle = self._handles.get(self._key(thread_id, component))
        if handle is None:
            raise RuntimeError(
                f"任务 {thread_id} 的沙箱尚未初始化，请先执行 ensure_sandbox"
            )
        return handle.backend

    def base_distributions(
        self,
        thread_id: str,
        *,
        component: str,
    ) -> frozenset[str]:
        """Return the immutable package baseline captured at sandbox creation."""

        return self._get_handle(thread_id, component).base_distributions

    def _get_handle(
        self,
        thread_id: str,
        component: str = "crawl-worker",
    ) -> _SandboxHandle:
        thread_id = sanitize_thread_id(thread_id)
        handle = self._handles.get(self._key(thread_id, component))
        if handle is None:
            raise RuntimeError(f"任务 {thread_id} 的沙箱不可用")
        return handle

    @traceable(
        name="sandbox.restore",
        run_type="chain",
        process_inputs=_trace_inputs,
        process_outputs=_trace_file_count,
    )
    async def restore_workspace(
        self,
        thread_id: str,
        handle: _SandboxHandle | None = None,
        *,
        component: str = "crawl-worker",
    ) -> int:
        """Restore the last successful local snapshot into a new sandbox."""

        thread_id = sanitize_thread_id(thread_id)
        active_handle = handle or self._get_handle(thread_id, component)
        files = await asyncio.to_thread(
            _load_local_workspace,
            self._local_workspace(thread_id, component),
        )
        if not files:
            return 0

        directories = {
            str(PurePosixPath(path).parent)
            for path, _content in files
        }
        await self._ensure_directories(active_handle, directories)
        responses = await active_handle.backend.aupload_files(files)
        failures = [response.path for response in responses if response.error]
        if failures:
            raise RuntimeError(f"恢复沙箱文件失败：{'、'.join(failures)}")

        _add_trace_metadata(
            thread_id=thread_id,
            sandbox_id=active_handle.backend.id,
            image=self._settings.open_sandbox_image,
        )
        return len(files)

    async def upload_workspace_files(
        self,
        thread_id: str,
        files: list[tuple[str, bytes]],
        *,
        component: str = "crawl-worker",
    ) -> None:
        """Upload and overwrite task files within the sandbox workspace."""

        handle = self._get_handle(thread_id, component)
        normalized: list[tuple[str, bytes]] = []
        directories: set[str] = set()
        for path, content in files:
            relative = _workspace_relative(path)
            sandbox_path = _sandbox_path(relative)
            normalized.append((sandbox_path, content))
            directories.add(str(PurePosixPath(sandbox_path).parent))

        await self._ensure_directories(handle, directories)
        responses = await handle.backend.aupload_files(normalized)
        failures = [response.path for response in responses if response.error]
        if failures:
            raise RuntimeError(f"写入沙箱文件失败：{'、'.join(failures)}")

    @traceable(
        name="sandbox.skills.sync",
        run_type="chain",
        process_inputs=_trace_sync_inputs,
        process_outputs=_trace_file_count,
    )
    async def replace_directory_files(
        self,
        thread_id: str,
        root: str,
        files: list[tuple[str, bytes]],
        *,
        component: str,
    ) -> int:
        """Replace a controlled physical directory inside one sandbox.

        CompositeBackend routes file-tool access for these paths elsewhere, but
        shell execution still needs real copies inside the container.
        """

        if root not in {"/skills", "/persisted-skills"}:
            raise ValueError(f"不允许同步沙箱目录：{root}")

        handle = self._get_handle(thread_id, component)
        normalized: list[tuple[str, bytes]] = []
        directories = {root}
        root_path = PurePosixPath(root)
        for relative_value, content in files:
            relative = PurePosixPath(relative_value)
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise ValueError(f"无效的 Skill 同步路径：{relative_value}")
            target = root_path / relative
            normalized.append((target.as_posix(), content))
            directories.add(target.parent.as_posix())

        # Use Python instead of shell-specific recursive deletion semantics.
        cleanup_code = (
            "from pathlib import Path; import shutil; "
            f"root=Path({root!r}); "
            "shutil.rmtree(root, ignore_errors=True); "
            "root.mkdir(parents=True, exist_ok=True)"
        )
        result = await handle.backend.aexecute(
            "python -c " + shlex.quote(cleanup_code),
        )
        if result.exit_code not in {None, 0}:
            detail = result.output.strip() or f"退出码 {result.exit_code}"
            raise RuntimeError(f"无法清理沙箱 Skill 目录：{detail}")

        await self._ensure_directories(handle, directories)
        if not normalized:
            return 0
        responses = await handle.backend.aupload_files(normalized)
        failures = [response.path for response in responses if response.error]
        if failures:
            raise RuntimeError(f"同步沙箱 Skill 失败：{'、'.join(failures)}")
        return len(normalized)

    @traceable(
        name="sandbox.export",
        run_type="chain",
        process_inputs=_trace_inputs,
        process_outputs=_trace_export_output,
    )
    async def export_workspace(
        self,
        thread_id: str,
        *,
        component: str = "crawl-worker",
    ) -> list[dict[str, Any]]:
        """Export the workspace and return a content-free artifact manifest."""

        thread_id = sanitize_thread_id(thread_id)
        handle = self._get_handle(thread_id, component)
        matches = await handle.backend.aglob("**/*", path="/workspace")
        if matches.error:
            raise RuntimeError(f"无法列出沙箱工作区：{matches.error}")

        paths: list[tuple[str, PurePosixPath]] = []
        for info in matches.matches or []:
            if info.get("is_dir"):
                continue
            relative = _workspace_relative(info["path"])
            paths.append((_sandbox_path(relative), relative))

        if not paths:
            return []

        responses = await handle.backend.adownload_files(
            [sandbox_path for sandbox_path, _relative in paths]
        )
        downloaded: list[tuple[PurePosixPath, bytes]] = []
        relative_by_path = dict(paths)
        failures: list[str] = []
        for response in responses:
            if response.error or response.content is None:
                failures.append(response.path)
                continue
            downloaded.append((relative_by_path[response.path], response.content))
        if failures:
            raise RuntimeError(f"导出沙箱文件失败：{'、'.join(failures)}")

        await asyncio.to_thread(
            _write_local_workspace,
            self._local_workspace(thread_id, component),
            downloaded,
        )
        _add_trace_metadata(
            thread_id=thread_id,
            sandbox_id=handle.backend.id,
            image=self._settings.open_sandbox_image,
        )
        return [
            {
                "path": _sandbox_path(relative),
                "size": len(content),
            }
            for relative, content in downloaded
        ]


SANDBOX_MANAGER = SandboxManager()
