"""DeepAgents filesystem backend configuration."""

from __future__ import annotations

from typing import Any

from deepagents.backends import (
    CompositeBackend,
    StateBackend,
    StoreBackend,
)
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)
from langgraph.runtime import get_runtime

from deep_data_research_agent import sandbox_manager
from deep_data_research_agent.identity import assigned_skill_namespace, user_identity
from deep_data_research_agent.memory import (
    failure_memory_namespace,
    user_memory_namespace,
)
from deep_data_research_agent.skill_storage import public_skill_namespace


def _under(path: str, prefix: str) -> bool:
    """Return whether a virtual absolute path is at or below one prefix."""

    normalized = "/" + path.lstrip("/")
    root = "/" + prefix.strip("/")
    return normalized == root or normalized.startswith(f"{root}/")


class ReadOnlyStoreBackend(StoreBackend):
    """Expose persisted Skills and memories without allowing Agent mutation."""

    def __init__(self, *, namespace, hide_archive: bool = False) -> None:
        super().__init__(namespace=namespace)
        self._hide_archive = hide_archive

    def _read_denied(self, path: str | None) -> bool:
        return bool(path and self._hide_archive and _under(path, "/archive"))

    def ls(self, path: str) -> LsResult:
        if self._read_denied(path):
            return LsResult(error="Permission denied: archived memory is hidden")
        return super().ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if self._read_denied(file_path):
            return ReadResult(error="Permission denied: archived memory is hidden")
        return super().read(file_path, offset, limit)

    async def aread(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        if self._read_denied(file_path):
            return ReadResult(error="Permission denied: archived memory is hidden")
        return await super().aread(file_path, offset, limit)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        if self._read_denied(path):
            return GrepResult(error="Permission denied: archived memory is hidden")
        return super().grep(pattern, path, glob, max_count=max_count)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        if self._read_denied(path):
            return GlobResult(error="Permission denied: archived memory is hidden")
        return super().glob(pattern, path)

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error="Permission denied: route is read-only")

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self.write(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return EditResult(error="Permission denied: route is read-only")

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return self.edit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error="Permission denied: route is read-only")

    async def adelete(self, file_path: str) -> DeleteResult:
        return self.delete(file_path)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [
            FileUploadResponse(path=path, error="permission_denied")
            for path, _content in files
        ]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for path in paths:
            if self._read_denied(path):
                responses.append(
                    FileDownloadResponse(path=path, error="permission_denied")
                )
            else:
                responses.extend(super().download_files([path]))
        return responses


class RestartSafeSandboxBackend(SandboxBackendProtocol):
    """Resolve one request's sandbox lazily from the active LangGraph runtime.

    DeepAgents 0.7 requires a concrete backend instance at graph construction
    time. The optional bound runtime remains useful for direct unit tests, while
    production graph calls resolve thread and user identity from context.
    """

    def __init__(
        self,
        *,
        component: str,
        network_enabled: bool,
        runtime: Any | None = None,
    ) -> None:
        self._component = component
        self._network_enabled = network_enabled
        self._bound_runtime = runtime

    def _runtime(self) -> Any:
        if self._bound_runtime is not None:
            return self._bound_runtime
        try:
            return get_runtime()
        except (RuntimeError, KeyError) as exc:
            raise RuntimeError(
                "沙箱 Backend 必须在 LangGraph 执行上下文中使用"
            ) from exc

    def _scope(self) -> tuple[str, str]:
        runtime = self._runtime()
        return _thread_id(runtime), user_identity(runtime)

    def _backend(self):
        """Return an existing backend for synchronous callers."""

        thread_id, user_id = self._scope()
        return sandbox_manager.SANDBOX_MANAGER.get_backend(
            thread_id,
            component=self._component,
            user_id=user_id,
        )

    async def _abackend(self):
        """Return the backend, recreating it only when this process has no handle."""

        thread_id, user_id = self._scope()
        try:
            return sandbox_manager.SANDBOX_MANAGER.get_backend(
                thread_id,
                component=self._component,
                user_id=user_id,
            )
        except sandbox_manager.SandboxNotInitializedError:
            pass
        return await sandbox_manager.SANDBOX_MANAGER.ensure(
            thread_id,
            component=self._component,
            network_enabled=self._network_enabled,
            user_id=user_id,
        )

    @property
    def id(self) -> str:
        try:
            return self._backend().id
        except (sandbox_manager.SandboxNotInitializedError, RuntimeError):
            # Graph construction happens before a request runtime exists.
            return f"pending:{self._component}"

    def ls(self, path):
        return self._backend().ls(path)

    async def als(self, path):
        return await (await self._abackend()).als(path)

    def read(self, file_path, offset=0, limit=2000):
        return self._backend().read(file_path, offset, limit)

    async def aread(self, file_path, offset=0, limit=2000):
        return await (await self._abackend()).aread(file_path, offset, limit)

    def grep(self, pattern, path=None, glob=None, *, max_count=None):
        return self._backend().grep(
            pattern,
            path,
            glob,
            max_count=max_count,
        )

    async def agrep(self, pattern, path=None, glob=None, *, max_count=None):
        return await (await self._abackend()).agrep(
            pattern,
            path,
            glob,
            max_count=max_count,
        )

    def glob(self, pattern, path=None):
        return self._backend().glob(pattern, path)

    async def aglob(self, pattern, path=None):
        return await (await self._abackend()).aglob(pattern, path)

    def write(self, file_path, content):
        if _under(file_path, "/workspace/input"):
            return WriteResult(error="Permission denied: uploaded inputs are read-only")
        return self._backend().write(file_path, content)

    async def awrite(self, file_path, content):
        if _under(file_path, "/workspace/input"):
            return WriteResult(error="Permission denied: uploaded inputs are read-only")
        return await (await self._abackend()).awrite(file_path, content)

    def edit(self, file_path, old_string, new_string, replace_all=False):
        if _under(file_path, "/workspace/input"):
            return EditResult(error="Permission denied: uploaded inputs are read-only")
        return self._backend().edit(
            file_path,
            old_string,
            new_string,
            replace_all=replace_all,
        )

    async def aedit(
        self,
        file_path,
        old_string,
        new_string,
        replace_all=False,
    ):
        if _under(file_path, "/workspace/input"):
            return EditResult(error="Permission denied: uploaded inputs are read-only")
        return await (await self._abackend()).aedit(
            file_path,
            old_string,
            new_string,
            replace_all=replace_all,
        )

    def upload_files(self, files):
        allowed = [(path, content) for path, content in files if not _under(path, "/workspace/input")]
        uploaded = iter(self._backend().upload_files(allowed))
        return [
            (
                FileUploadResponse(path=path, error="permission_denied")
                if _under(path, "/workspace/input")
                else next(uploaded)
            )
            for path, _content in files
        ]

    async def aupload_files(self, files):
        allowed = [(path, content) for path, content in files if not _under(path, "/workspace/input")]
        uploaded = iter(await (await self._abackend()).aupload_files(allowed))
        return [
            (
                FileUploadResponse(path=path, error="permission_denied")
                if _under(path, "/workspace/input")
                else next(uploaded)
            )
            for path, _content in files
        ]

    def download_files(self, paths):
        return self._backend().download_files(paths)

    async def adownload_files(self, paths):
        return await (await self._abackend()).adownload_files(paths)

    def execute(self, command, *, timeout=None):
        return self._backend().execute(command, timeout=timeout)

    async def aexecute(self, command, *, timeout=None):
        return await (await self._abackend()).aexecute(command, timeout=timeout)


def _thread_id(runtime: Any) -> str:
    """Return the sanitized LangGraph thread ID for a backend factory call."""

    return sandbox_manager.thread_id_from_runtime(runtime)


def _sandbox_backend(
    *,
    component: str,
    skill_agents: tuple[str, ...],
    network_enabled: bool,
    runtime: Any | None = None,
) -> CompositeBackend:
    """Build a concrete routed backend with runtime-scoped sandbox access."""

    sandbox = RestartSafeSandboxBackend(
        component=component,
        network_enabled=network_enabled,
        runtime=runtime,
    )
    routes: dict[str, Any] = {
        "/state/": StateBackend(),
        "/memories/user/": ReadOnlyStoreBackend(
            namespace=user_memory_namespace,
        ),
    }
    for agent_name in skill_agents:
        routes[f"/memories/agent/{agent_name}/"] = ReadOnlyStoreBackend(
            namespace=lambda _rt, name=agent_name: failure_memory_namespace(name),
            hide_archive=True,
        )
        # Route at the Agent directory so the remaining StoreBackend key keeps
        # the required /active/... prefix used in MongoDB.
        routes[f"/skills/public/{agent_name}/"] = ReadOnlyStoreBackend(
            namespace=lambda _rt, name=agent_name: public_skill_namespace(name),
        )
        routes[f"/skills/user/{agent_name}/"] = ReadOnlyStoreBackend(
            namespace=lambda rt, name=agent_name: assigned_skill_namespace(rt, name),
        )

    return CompositeBackend(
        default=sandbox,
        routes=routes,
        artifacts_root="/state",
    )


def create_backend(runtime: Any | None = None) -> CompositeBackend:
    """Create the concrete Supervisor backend used by DeepAgents 0.7."""

    return _sandbox_backend(
        component="supervisor",
        skill_agents=("supervisor", "data-analyst"),
        network_enabled=True,
        runtime=runtime,
    )


def create_worker_backend(runtime: Any | None = None) -> CompositeBackend:
    """Create the concrete crawl-worker backend used by DeepAgents 0.7."""

    return _sandbox_backend(
        component="crawl-worker",
        skill_agents=("crawl-worker",),
        network_enabled=False,
        runtime=runtime,
    )


# Graphs share immutable routing objects; request identity is resolved lazily
# by RestartSafeSandboxBackend and StoreBackend from the current runtime.
SUPERVISOR_BACKEND = create_backend()
WORKER_BACKEND = create_worker_backend()
