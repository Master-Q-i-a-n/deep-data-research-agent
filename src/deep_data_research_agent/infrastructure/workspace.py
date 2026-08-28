"""Durable storage for sandbox workspace snapshots."""

from __future__ import annotations

import asyncio
import inspect
import logging
import mimetypes
import shutil
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

import aiofiles
import alibabacloud_oss_v2 as oss
import alibabacloud_oss_v2.aio as oss_aio
from alibabacloud_credentials.client import Client as CredentialsClient
from alibabacloud_credentials.models import Config as CredentialsConfig

from deep_data_research_agent.core.config import Settings

logger = logging.getLogger(__name__)
_VIRTUAL_ROOT = PurePosixPath("/workspace")
_STREAM_CHUNK_BYTES = 1024 * 1024


class WorkspaceStorageError(RuntimeError):
    """Raised when durable workspace storage cannot complete an operation."""


class WorkspaceFileNotFound(WorkspaceStorageError):
    """Raised when a requested durable workspace file does not exist."""


@dataclass(frozen=True, slots=True)
class WorkspaceScope:
    """Identify one user-owned component workspace."""

    user_id: str
    thread_id: str
    component: str

    def __post_init__(self) -> None:
        for label, value in (
            ("user_id", self.user_id),
            ("thread_id", self.thread_id),
            ("component", self.component),
        ):
            normalized = str(value).strip()
            if (
                not normalized
                or normalized in {".", ".."}
                or "/" in normalized
                or "\\" in normalized
            ):
                raise ValueError(f"无效的工作区 {label}")
            object.__setattr__(self, label, normalized)


@dataclass(frozen=True, slots=True)
class WorkspaceObject:
    """Content-free metadata for one durable workspace file."""

    path: str
    size: int
    etag: str | None = None
    content_type: str | None = None


def workspace_relative_path(path: str | PurePosixPath) -> PurePosixPath:
    """Validate a virtual workspace path and return its relative path."""

    candidate = PurePosixPath(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(_VIRTUAL_ROOT)
        except ValueError as exc:
            raise ValueError(f"沙箱文件必须位于 /workspace：{path}") from exc
    if not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"无效的沙箱工作区路径：{path}")
    return candidate


def virtual_workspace_path(path: str | PurePosixPath) -> str:
    """Return the canonical ``/workspace`` path for a validated relative path."""

    relative = workspace_relative_path(path)
    return f"/workspace/{relative.as_posix()}"


@runtime_checkable
class WorkspaceStore(Protocol):
    """Minimal durable operations required by workspace consumers."""

    async def list(
        self,
        scope: WorkspaceScope,
        *,
        prefix: str | PurePosixPath | None = None,
    ) -> list[WorkspaceObject]: ...

    async def stat(
        self,
        scope: WorkspaceScope,
        path: str | PurePosixPath,
    ) -> WorkspaceObject: ...

    async def read(
        self,
        scope: WorkspaceScope,
        path: str | PurePosixPath,
    ) -> bytes: ...

    async def stream(
        self,
        scope: WorkspaceScope,
        path: str | PurePosixPath,
    ) -> tuple[WorkspaceObject, AsyncIterator[bytes]]: ...

    async def write_many(
        self,
        scope: WorkspaceScope,
        files: Iterable[tuple[str | PurePosixPath, bytes]],
    ) -> None: ...

    async def delete_file(
        self,
        scope: WorkspaceScope,
        path: str | PurePosixPath,
    ) -> None: ...

    async def delete_thread(self, user_id: str, thread_id: str) -> None: ...

    async def check_ready(self) -> None: ...

    async def close(self) -> None: ...


class LocalWorkspaceStore:
    """Filesystem-backed workspace store used in development and tests."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def workspace_path(self, scope: WorkspaceScope) -> Path:
        return (
            self.root
            / scope.user_id
            / "jobs"
            / scope.thread_id
            / scope.component
            / "workspace"
        )

    def _file_path(
        self,
        scope: WorkspaceScope,
        path: str | PurePosixPath,
    ) -> tuple[PurePosixPath, Path]:
        relative = workspace_relative_path(path)
        root = self.workspace_path(scope).resolve()
        target = (root.joinpath(*relative.parts)).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"工作区路径越界：{path}")
        return relative, target

    async def list(
        self,
        scope: WorkspaceScope,
        *,
        prefix: str | PurePosixPath | None = None,
    ) -> list[WorkspaceObject]:
        root = self.workspace_path(scope).resolve()
        relative_prefix = workspace_relative_path(prefix) if prefix is not None else None

        def collect() -> list[WorkspaceObject]:
            if not root.is_dir():
                return []
            objects: list[WorkspaceObject] = []
            for candidate in sorted(root.rglob("*")):
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                resolved = candidate.resolve()
                if not resolved.is_relative_to(root):
                    continue
                relative = PurePosixPath(resolved.relative_to(root).as_posix())
                if relative_prefix is not None:
                    prefix_text = relative_prefix.as_posix().rstrip("/")
                    relative_text = relative.as_posix()
                    if relative_text != prefix_text and not relative_text.startswith(
                        f"{prefix_text}/"
                    ):
                        continue
                objects.append(
                    WorkspaceObject(
                        path=virtual_workspace_path(relative),
                        size=resolved.stat().st_size,
                    )
                )
            return objects

        return await asyncio.to_thread(collect)

    async def stat(
        self,
        scope: WorkspaceScope,
        path: str | PurePosixPath,
    ) -> WorkspaceObject:
        relative, target = self._file_path(scope, path)

        def stat() -> WorkspaceObject:
            if target.is_symlink() or not target.is_file():
                raise WorkspaceFileNotFound(virtual_workspace_path(relative))
            return WorkspaceObject(
                path=virtual_workspace_path(relative),
                size=target.stat().st_size,
            )

        return await asyncio.to_thread(stat)

    async def read(
        self,
        scope: WorkspaceScope,
        path: str | PurePosixPath,
    ) -> bytes:
        relative, target = self._file_path(scope, path)

        def read() -> bytes:
            if target.is_symlink() or not target.is_file():
                raise WorkspaceFileNotFound(virtual_workspace_path(relative))
            return target.read_bytes()

        return await asyncio.to_thread(read)

    async def stream(
        self,
        scope: WorkspaceScope,
        path: str | PurePosixPath,
    ) -> tuple[WorkspaceObject, AsyncIterator[bytes]]:
        metadata = await self.stat(scope, path)
        _relative, target = self._file_path(scope, path)

        async def chunks() -> AsyncIterator[bytes]:
            async with aiofiles.open(target, "rb") as source:
                while chunk := await source.read(_STREAM_CHUNK_BYTES):
                    yield chunk

        return metadata, chunks()

    async def write_many(
        self,
        scope: WorkspaceScope,
        files: Iterable[tuple[str | PurePosixPath, bytes]],
    ) -> None:
        normalized = [
            (self._file_path(scope, path)[1], content) for path, content in files
        ]

        def write() -> None:
            for target, content in normalized:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)

        await asyncio.to_thread(write)

    async def delete_file(
        self,
        scope: WorkspaceScope,
        path: str | PurePosixPath,
    ) -> None:
        _relative, target = self._file_path(scope, path)
        root = self.workspace_path(scope).resolve()

        def delete() -> None:
            if target.is_file() or target.is_symlink():
                target.unlink()
            parent = target.parent
            while parent != root:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

        await asyncio.to_thread(delete)

    async def delete_thread(self, user_id: str, thread_id: str) -> None:
        scope = WorkspaceScope(user_id=user_id, thread_id=thread_id, component="scope")
        jobs_root = (self.root / scope.user_id / "jobs").resolve()
        target = (jobs_root / scope.thread_id).resolve()
        if not target.is_relative_to(jobs_root):
            raise ValueError("拒绝删除用户任务目录之外的路径")
        await asyncio.to_thread(shutil.rmtree, target, True)

    async def check_ready(self) -> None:
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)

    async def close(self) -> None:
        return None


class OSSWorkspaceStore:
    """Alibaba Cloud OSS-backed workspace store for production."""

    def __init__(
        self,
        *,
        region: str,
        endpoint: str,
        bucket: str,
        prefix: str,
        role_name: str,
    ) -> None:
        self.region = region
        self.endpoint = endpoint
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if not self.prefix or any(part in {"", ".", ".."} for part in self.prefix.split("/")):
            raise ValueError("OSS_PREFIX 无效")

        credential_config = CredentialsConfig(
            type="ecs_ram_role",
            role_name=role_name,
            # Production must never fall back to the less secure IMDSv1 flow.
            disable_imds_v1=True,
        )
        credential_client = CredentialsClient(credential_config)

        def load_credentials() -> oss.credentials.Credentials:
            credential = credential_client.get_credential()
            return oss.credentials.Credentials(
                access_key_id=credential.access_key_id,
                access_key_secret=credential.access_key_secret,
                security_token=credential.security_token,
            )

        config = oss.config.load_default()
        config.credentials_provider = oss.credentials.CredentialsProviderFunc(
            func=load_credentials
        )
        config.region = region
        config.endpoint = endpoint
        self._client = oss_aio.AsyncClient(config)

    def _workspace_prefix(self, scope: WorkspaceScope) -> str:
        return (
            f"{self.prefix}/{scope.user_id}/jobs/{scope.thread_id}/"
            f"{scope.component}/workspace/"
        )

    def _thread_prefix(self, user_id: str, thread_id: str) -> str:
        scope = WorkspaceScope(user_id=user_id, thread_id=thread_id, component="scope")
        return f"{self.prefix}/{scope.user_id}/jobs/{scope.thread_id}/"

    def _object_key(
        self,
        scope: WorkspaceScope,
        path: str | PurePosixPath,
    ) -> tuple[PurePosixPath, str]:
        relative = workspace_relative_path(path)
        return relative, f"{self._workspace_prefix(scope)}{relative.as_posix()}"

    @staticmethod
    def _raise_storage_error(exc: Exception, *, path: str | None = None) -> None:
        if isinstance(exc, WorkspaceStorageError):
            raise exc
        if isinstance(exc, oss.exceptions.ServiceError) and exc.code in {
            "NoSuchKey",
            "NoSuchObject",
        }:
            raise WorkspaceFileNotFound(path or "OSS Object 不存在") from exc
        request_id = getattr(exc, "request_id", None)
        detail = f"OSS 请求失败（request_id={request_id}）" if request_id else "OSS 请求失败"
        raise WorkspaceStorageError(detail) from exc

    async def _list_keys(self, prefix: str, *, max_keys: int = 1000) -> list[object]:
        contents: list[object] = []
        continuation_token: str | None = None
        try:
            while True:
                result = await self._client.list_objects_v2(
                    oss.ListObjectsV2Request(
                        bucket=self.bucket,
                        prefix=prefix,
                        max_keys=max_keys,
                        continuation_token=continuation_token,
                    )
                )
                contents.extend(result.contents or [])
                if not result.is_truncated:
                    break
                continuation_token = result.next_continuation_token
                if not continuation_token:
                    raise WorkspaceStorageError("OSS 分页响应缺少 continuation token")
        except Exception as exc:  # noqa: BLE001 - normalize all SDK/transport failures.
            self._raise_storage_error(exc)
        return contents

    async def list(
        self,
        scope: WorkspaceScope,
        *,
        prefix: str | PurePosixPath | None = None,
    ) -> list[WorkspaceObject]:
        workspace_prefix = self._workspace_prefix(scope)
        object_prefix = workspace_prefix
        if prefix is not None:
            object_prefix += f"{workspace_relative_path(prefix).as_posix().rstrip('/')}/"
        contents = await self._list_keys(object_prefix)
        objects: list[WorkspaceObject] = []
        for item in contents:
            key = str(getattr(item, "key", ""))
            if not key.startswith(workspace_prefix) or key.endswith("/"):
                continue
            relative_text = key[len(workspace_prefix) :]
            try:
                relative = workspace_relative_path(relative_text)
            except ValueError:
                logger.warning("忽略 OSS 中无效的工作区 Object：bucket=%s key=%s", self.bucket, key)
                continue
            objects.append(
                WorkspaceObject(
                    path=virtual_workspace_path(relative),
                    size=int(getattr(item, "size", 0) or 0),
                    etag=getattr(item, "etag", None),
                )
            )
        return sorted(objects, key=lambda item: item.path)

    async def stat(
        self,
        scope: WorkspaceScope,
        path: str | PurePosixPath,
    ) -> WorkspaceObject:
        relative, key = self._object_key(scope, path)
        try:
            result = await self._client.head_object(
                oss.HeadObjectRequest(bucket=self.bucket, key=key)
            )
        except Exception as exc:  # noqa: BLE001 - normalize SDK failures.
            self._raise_storage_error(exc, path=virtual_workspace_path(relative))
        return WorkspaceObject(
            path=virtual_workspace_path(relative),
            size=int(result.content_length or 0),
            etag=result.etag,
            content_type=result.content_type,
        )

    async def read(
        self,
        scope: WorkspaceScope,
        path: str | PurePosixPath,
    ) -> bytes:
        relative, key = self._object_key(scope, path)
        try:
            result = await self._client.get_object(
                oss.GetObjectRequest(bucket=self.bucket, key=key)
            )
            if result.body is None:
                raise WorkspaceStorageError("OSS 返回空响应体")
            async with result.body as body:
                return await body.read()
        except Exception as exc:  # noqa: BLE001 - normalize SDK failures.
            self._raise_storage_error(exc, path=virtual_workspace_path(relative))

    async def stream(
        self,
        scope: WorkspaceScope,
        path: str | PurePosixPath,
    ) -> tuple[WorkspaceObject, AsyncIterator[bytes]]:
        relative, key = self._object_key(scope, path)
        try:
            result = await self._client.get_object(
                oss.GetObjectRequest(bucket=self.bucket, key=key)
            )
            if result.body is None:
                raise WorkspaceStorageError("OSS 返回空响应体")
        except Exception as exc:  # noqa: BLE001 - normalize SDK failures.
            self._raise_storage_error(exc, path=virtual_workspace_path(relative))

        metadata = WorkspaceObject(
            path=virtual_workspace_path(relative),
            size=int(result.content_length or 0),
            etag=result.etag,
            content_type=result.content_type,
        )

        async def chunks() -> AsyncIterator[bytes]:
            assert result.body is not None
            async with result.body as body:
                iterator = body.iter_bytes(block_size=_STREAM_CHUNK_BYTES)
                # OSS v2 的 AsyncStreamBodyReader 先返回 coroutine，而部分兼容
                # 实现会直接返回 AsyncIterator；两种 SDK 形态都需要支持。
                if inspect.isawaitable(iterator):
                    iterator = await iterator
                async for chunk in iterator:
                    yield chunk

        return metadata, chunks()

    async def write_many(
        self,
        scope: WorkspaceScope,
        files: Iterable[tuple[str | PurePosixPath, bytes]],
    ) -> None:
        for path, content in files:
            relative, key = self._object_key(scope, path)
            try:
                await self._client.put_object(
                    oss.PutObjectRequest(
                        bucket=self.bucket,
                        key=key,
                        body=content,
                        content_length=len(content),
                        content_type=(
                            mimetypes.guess_type(relative.name)[0]
                            or "application/octet-stream"
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - normalize SDK failures.
                self._raise_storage_error(exc, path=virtual_workspace_path(relative))

    async def delete_file(
        self,
        scope: WorkspaceScope,
        path: str | PurePosixPath,
    ) -> None:
        relative, key = self._object_key(scope, path)
        try:
            await self._client.delete_object(
                oss.DeleteObjectRequest(bucket=self.bucket, key=key)
            )
        except Exception as exc:  # noqa: BLE001 - normalize SDK failures.
            self._raise_storage_error(exc, path=virtual_workspace_path(relative))

    async def delete_thread(self, user_id: str, thread_id: str) -> None:
        prefix = self._thread_prefix(user_id, thread_id)
        contents = await self._list_keys(prefix)
        keys = [str(getattr(item, "key", "")) for item in contents]
        keys = [key for key in keys if key.startswith(prefix)]
        try:
            for offset in range(0, len(keys), 1000):
                batch = keys[offset : offset + 1000]
                await self._client.delete_multiple_objects(
                    oss.DeleteMultipleObjectsRequest(
                        bucket=self.bucket,
                        delete=oss.Delete(
                            objects=[oss.ObjectIdentifier(key=key) for key in batch],
                            quiet=True,
                        ),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - normalize SDK failures.
            self._raise_storage_error(exc)

    async def check_ready(self) -> None:
        try:
            await self._client.list_objects_v2(
                oss.ListObjectsV2Request(
                    bucket=self.bucket,
                    prefix=f"{self.prefix}/",
                    max_keys=1,
                )
            )
        except Exception as exc:  # noqa: BLE001 - normalize SDK failures.
            self._raise_storage_error(exc)

    async def close(self) -> None:
        await self._client.close()


def create_workspace_store(
    settings: Settings,
    *,
    local_root: Path | None = None,
) -> WorkspaceStore:
    """Create the configured workspace store without accepting static AKs."""

    if settings.workspace_storage_backend == "local":
        return LocalWorkspaceStore(local_root or settings.artifact_root)
    return OSSWorkspaceStore(
        region=settings.oss_region,
        endpoint=settings.oss_endpoint,
        bucket=settings.oss_bucket_name,
        prefix=settings.oss_prefix,
        role_name=settings.oss_ecs_ram_role,
    )


__all__ = [
    "LocalWorkspaceStore",
    "OSSWorkspaceStore",
    "WorkspaceFileNotFound",
    "WorkspaceObject",
    "WorkspaceScope",
    "WorkspaceStorageError",
    "WorkspaceStore",
    "create_workspace_store",
    "virtual_workspace_path",
    "workspace_relative_path",
]
