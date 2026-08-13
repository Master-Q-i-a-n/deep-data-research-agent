"""DeepAgents filesystem backend configuration."""

from __future__ import annotations

from typing import Any

from deepagents import FilesystemPermission
from deepagents.backends import (
    CompositeBackend,
    StateBackend,
    StoreBackend,
)
from deepagents.backends.protocol import SandboxBackendProtocol

from deep_data_research_agent import sandbox_manager
from deep_data_research_agent.identity import assigned_skill_namespace, user_identity
from deep_data_research_agent.memory import (
    failure_memory_namespace,
    user_memory_namespace,
)
from deep_data_research_agent.skill_storage import public_skill_namespace


class RestartSafeSandboxBackend(SandboxBackendProtocol):
    """Lazily rebuild a sandbox when a checkpoint resumes after process restart."""

    def __init__(
        self,
        *,
        thread_id: str,
        component: str,
        user_id: str,
        network_enabled: bool,
    ) -> None:
        self._thread_id = thread_id
        self._component = component
        self._user_id = user_id
        self._network_enabled = network_enabled

    def _backend(self):
        """Return an existing backend for synchronous callers."""

        return sandbox_manager.SANDBOX_MANAGER.get_backend(
            self._thread_id,
            component=self._component,
            user_id=self._user_id,
        )

    async def _abackend(self):
        """Return the backend, recreating it only when this process has no handle."""

        try:
            return self._backend()
        except sandbox_manager.SandboxNotInitializedError:
            pass
        return await sandbox_manager.SANDBOX_MANAGER.ensure(
            self._thread_id,
            component=self._component,
            network_enabled=self._network_enabled,
            user_id=self._user_id,
        )

    @property
    def id(self) -> str:
        try:
            return self._backend().id
        except sandbox_manager.SandboxNotInitializedError:
            # DeepAgents only uses this before execution to detect sandbox support.
            return f"pending:{self._component}:{self._thread_id}"

    def ls(self, path):
        return self._backend().ls(path)

    async def als(self, path):
        return await (await self._abackend()).als(path)

    def read(self, file_path, offset=0, limit=2000):
        return self._backend().read(file_path, offset, limit)

    async def aread(self, file_path, offset=0, limit=2000):
        return await (await self._abackend()).aread(file_path, offset, limit)

    def grep(self, pattern, path=None, glob=None):
        return self._backend().grep(pattern, path, glob)

    async def agrep(self, pattern, path=None, glob=None):
        return await (await self._abackend()).agrep(pattern, path, glob)

    def glob(self, pattern, path=None):
        return self._backend().glob(pattern, path)

    async def aglob(self, pattern, path=None):
        return await (await self._abackend()).aglob(pattern, path)

    def write(self, file_path, content):
        return self._backend().write(file_path, content)

    async def awrite(self, file_path, content):
        return await (await self._abackend()).awrite(file_path, content)

    def edit(self, file_path, old_string, new_string, replace_all=False):
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
        return await (await self._abackend()).aedit(
            file_path,
            old_string,
            new_string,
            replace_all=replace_all,
        )

    def upload_files(self, files):
        return self._backend().upload_files(files)

    async def aupload_files(self, files):
        return await (await self._abackend()).aupload_files(files)

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
    runtime: Any,
    *,
    component: str,
    skill_agents: tuple[str, ...],
    network_enabled: bool,
) -> CompositeBackend:
    """Build one request-local sandbox backend with stable routed storage."""

    # Backend factories can run while resuming a middle checkpoint, where the
    # graph-level before_agent hook is intentionally skipped. Capture the
    # authenticated identity now so lazy sandbox recovery never guesses an owner.
    sandbox = RestartSafeSandboxBackend(
        thread_id=_thread_id(runtime),
        component=component,
        user_id=user_identity(runtime),
        network_enabled=network_enabled,
    )
    routes: dict[str, Any] = {
        "/state/": StateBackend(),
        "/memories/user/": StoreBackend(
            namespace=user_memory_namespace,
            file_format="v2",
        ),
    }
    for agent_name in skill_agents:
        routes[f"/memories/agent/{agent_name}/"] = StoreBackend(
            namespace=lambda _rt, name=agent_name: failure_memory_namespace(name),
            file_format="v2",
        )
        # Route at the Agent directory so the remaining StoreBackend key keeps
        # the required /active/... prefix used in MongoDB.
        routes[f"/skills/public/{agent_name}/"] = StoreBackend(
            namespace=lambda _rt, name=agent_name: public_skill_namespace(name),
            file_format="v2",
        )
        routes[f"/skills/user/{agent_name}/"] = StoreBackend(
            namespace=lambda rt, name=agent_name: assigned_skill_namespace(rt, name),
            file_format="v2",
        )

    return CompositeBackend(
        default=sandbox,
        routes=routes,
        artifacts_root="/state",
    )


def create_backend(runtime: Any) -> CompositeBackend:
    """Create the Supervisor backend after its sandbox lifecycle hook runs."""

    return _sandbox_backend(
        runtime,
        component="supervisor",
        skill_agents=("supervisor", "data-analyst"),
        network_enabled=True,
    )


def create_worker_backend(runtime: Any) -> CompositeBackend:
    """Create the crawl-worker backend after its outer graph initializes it."""

    return _sandbox_backend(
        runtime,
        component="crawl-worker",
        skill_agents=("crawl-worker",),
        network_enabled=False,
    )


# DeepAgents evaluates these rules in order. Unmatched paths, such as
# /workspace/** and /skill-manage/**, are handled by the isolated default sandbox.
FILESYSTEM_PERMISSIONS = [
    # HTTP 上传绕过 Agent 文件工具；模型只能读取原始输入，不能用
    # write_file/edit_file 静默覆盖用户文件。
    FilesystemPermission(
        operations=["write"],
        paths=["/workspace/input/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=["/workspace/input/**"],
        mode="allow",
    ),
    FilesystemPermission(
        operations=["write"],
        paths=["/skills/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=["/skills/**"],
        mode="allow",
    ),
    FilesystemPermission(
        operations=["read", "write"],
        paths=["/state/**"],
        mode="allow",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=[
            "/memories/agent/supervisor/archive/**",
            "/memories/agent/data-analyst/archive/**",
            "/memories/agent/crawl-worker/archive/**",
        ],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["write"],
        paths=["/memories/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=["/memories/**"],
        mode="allow",
    ),
]


WORKER_FILESYSTEM_PERMISSIONS = [
    FilesystemPermission(
        operations=["read"],
        paths=["/memories/agent/crawl-worker/archive/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["write"],
        paths=["/skills/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=["/skills/**"],
        mode="allow",
    ),
    FilesystemPermission(
        operations=["read", "write"],
        paths=["/state/**"],
        mode="allow",
    ),
    FilesystemPermission(
        operations=["write"],
        paths=["/memories/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=["/memories/**"],
        mode="allow",
    ),
]
