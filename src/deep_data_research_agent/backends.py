"""DeepAgents filesystem backend configuration."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from deepagents import FilesystemPermission
from deepagents.backends import (
    BackendProtocol,
    CompositeBackend,
    FilesystemBackend,
    StateBackend,
)
from deepagents.backends.protocol import (
    EditResult,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

from deep_data_research_agent.config import get_settings

# These operations intentionally run once while the graph module is imported,
# before LangGraph starts handling requests on the ASGI event loop.
PACKAGE_ROOT = Path(__file__).resolve().parent
SKILLS_ROOT = PACKAGE_ROOT / "skills"
ARTIFACT_ROOT = get_settings().artifact_root.expanduser().resolve()
ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)

_WORKSPACE_FILESYSTEM = FilesystemBackend(
    root_dir=ARTIFACT_ROOT,
    virtual_mode=True,
)
_SKILLS_FILESYSTEM = FilesystemBackend(
    root_dir=SKILLS_ROOT,
    virtual_mode=True,
)


def _thread_id(runtime: Any) -> str:
    """Read and sanitize the LangGraph thread ID used for local isolation."""

    config = getattr(runtime, "config", {}) or {}
    value = config.get("configurable", {}).get("thread_id", "local")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value)) or "local"


def workspace_root(runtime: Any) -> Path:
    """Return the pre-resolved physical workspace for the current graph thread."""

    return ARTIFACT_ROOT / _thread_id(runtime) / "workspace"


class _ThreadWorkspaceBackend(BackendProtocol):
    """Expose one thread directory through a shared, pre-initialized filesystem."""

    def __init__(self, backend: FilesystemBackend, thread_id: str) -> None:
        self._backend = backend
        self._prefix = f"/{thread_id}/workspace"

    def _physical_path(self, path: str | None) -> str:
        if path is None or path in {"", "/"}:
            return self._prefix
        return f"{self._prefix}/{path.lstrip('/')}"

    def _visible_path(self, path: str) -> str:
        if path == self._prefix:
            return "/"
        if path.startswith(f"{self._prefix}/"):
            return path[len(self._prefix) :]
        return path

    def _visible_error(self, error: str | None) -> str | None:
        if error is None:
            return None
        return error.replace(f"{self._prefix}/", "/").replace(self._prefix, "/")

    def _visible_file(self, file_info: FileInfo) -> FileInfo:
        return {**file_info, "path": self._visible_path(file_info["path"])}

    def ls(self, path: str) -> LsResult:
        result = self._backend.ls(self._physical_path(path))
        entries = None
        if result.entries is not None:
            entries = [self._visible_file(entry) for entry in result.entries]
        return LsResult(error=self._visible_error(result.error), entries=entries)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        result = self._backend.read(self._physical_path(file_path), offset, limit)
        result.error = self._visible_error(result.error)
        return result

    def write(self, file_path: str, content: str) -> WriteResult:
        result = self._backend.write(self._physical_path(file_path), content)
        result.error = self._visible_error(result.error)
        if result.path is not None:
            result.path = self._visible_path(result.path)
        return result

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        result = self._backend.edit(
            self._physical_path(file_path),
            old_string,
            new_string,
            replace_all=replace_all,
        )
        result.error = self._visible_error(result.error)
        if result.path is not None:
            result.path = self._visible_path(result.path)
        return result

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        result = self._backend.grep(
            pattern,
            path=self._physical_path(path),
            glob=glob,
        )
        matches = None
        if result.matches is not None:
            matches = [
                {**match, "path": self._visible_path(match["path"])}
                for match in result.matches
            ]
        return GrepResult(error=self._visible_error(result.error), matches=matches)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        result = self._backend.glob(pattern, path=self._physical_path(path))
        matches = None
        if result.matches is not None:
            matches = [self._visible_file(match) for match in result.matches]
        return GlobResult(error=self._visible_error(result.error), matches=matches)

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        mapped = [(self._physical_path(path), content) for path, content in files]
        responses = self._backend.upload_files(mapped)
        for response in responses:
            response.path = self._visible_path(response.path)
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses = self._backend.download_files(
            [self._physical_path(path) for path in paths]
        )
        for response in responses:
            response.path = self._visible_path(response.path)
        return responses


def create_backend(runtime: Any) -> CompositeBackend:
    """Create request-local routing without performing filesystem operations."""

    return CompositeBackend(
        default=StateBackend(),
        routes={
            "/workspace/": _ThreadWorkspaceBackend(
                _WORKSPACE_FILESYSTEM,
                _thread_id(runtime),
            ),
            "/skills/": _SKILLS_FILESYSTEM,
        },
    )


# Rules are first-match-wins, so the specific skill/workspace permissions must
# precede the catch-all deny rule.
FILESYSTEM_PERMISSIONS = [
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
        paths=["/workspace/**"],
        mode="allow",
    ),
    FilesystemPermission(
        operations=["read", "write"],
        paths=["/**"],
        mode="deny",
    ),
]
