"""Agent Server checkpointer that routes each user to a separate SQLite file."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any

import aiosqlite
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from deep_data_research_agent import database
from deep_data_research_agent.config import get_settings

_SAFE_DIRECTORY = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def storage_user_id(user_id: str) -> str:
    """Keep trusted IDs readable and hash any future provider-specific identity."""

    value = user_id.strip()
    if _SAFE_DIRECTORY.fullmatch(value):
        return value
    return sha256(value.encode("utf-8")).hexdigest()


def configured_user_id(config: RunnableConfig | None) -> str | None:
    configurable = (config or {}).get("configurable", {})
    value = configurable.get("langgraph_auth_user_id")
    if value:
        return str(value)

    user = configurable.get("langgraph_auth_user")
    if isinstance(user, dict):
        value = user.get("identity")
    else:
        value = getattr(user, "identity", None)
    return str(value) if value else None


class UserScopedSqliteCheckpointer(BaseCheckpointSaver):
    """Lazily route LangGraph checkpoint calls by authenticated thread owner."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._root = root.expanduser().resolve()
        self._savers: dict[str, AsyncSqliteSaver] = {}
        self._connections: dict[str, aiosqlite.Connection] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, user_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(user_id, asyncio.Lock())

    async def _saver(self, user_id: str) -> AsyncSqliteSaver:
        if saver := self._savers.get(user_id):
            return saver
        lock = await self._lock_for(user_id)
        async with lock:
            if saver := self._savers.get(user_id):
                return saver
            user_root = self._root / storage_user_id(user_id)
            await asyncio.to_thread(user_root.mkdir, parents=True, exist_ok=True)
            connection = await aiosqlite.connect(user_root / "checkpoints.sqlite")
            await connection.execute("PRAGMA busy_timeout=5000")
            await connection.execute("PRAGMA foreign_keys=ON")
            saver = AsyncSqliteSaver(connection, serde=self.serde)
            await saver.setup()  # setup also enables WAL.
            self._connections[user_id] = connection
            self._savers[user_id] = saver
            return saver

    @staticmethod
    def _thread_id(config: RunnableConfig | None) -> str:
        thread_id = (config or {}).get("configurable", {}).get("thread_id")
        if not thread_id:
            raise ValueError("检查点配置缺少 thread_id")
        return str(thread_id)

    async def _user_for_config(
        self,
        config: RunnableConfig | None,
        *,
        claim: bool = False,
    ) -> str:
        thread_id = self._thread_id(config)
        user_id = configured_user_id(config)
        if user_id is not None:
            if claim:
                await database.claim_thread(thread_id, user_id)
            else:
                owner = await database.get_thread_owner(thread_id)
                if owner is not None and owner != user_id:
                    raise database.ThreadOwnershipError("该会话不属于当前用户")
            return user_id

        owner = await database.get_thread_owner(thread_id)
        if owner is None:
            raise RuntimeError(f"会话 {thread_id} 尚未登记用户归属")
        return owner

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        user_id = await self._user_for_config(config)
        return await (await self._saver(user_id)).aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        user_id = await self._user_for_config(config)
        saver = await self._saver(user_id)
        async for item in saver.alist(
            config,
            filter=filter,
            before=before,
            limit=limit,
        ):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        user_id = await self._user_for_config(config, claim=True)
        return await (await self._saver(user_id)).aput(
            config,
            checkpoint,
            metadata,
            new_versions,
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        user_id = await self._user_for_config(config, claim=True)
        await (await self._saver(user_id)).aput_writes(
            config,
            writes,
            task_id,
            task_path,
        )

    async def adelete_thread(self, thread_id: str) -> None:
        owner = await database.get_thread_owner(thread_id)
        if owner is None:
            return
        await (await self._saver(owner)).adelete_thread(thread_id)
        await database.delete_thread_claim(thread_id, owner)

    async def adelete_for_runs(self, run_ids: Sequence[str]) -> None:
        # AsyncSqliteSaver 3.x does not expose run-to-thread routing.
        raise NotImplementedError("SQLite 检查点暂不支持按 run_id 删除")

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        owner = await database.get_thread_owner(source_thread_id)
        if owner is None:
            raise RuntimeError("源会话不存在")
        await database.claim_thread(target_thread_id, owner)
        await (await self._saver(owner)).acopy_thread(
            source_thread_id,
            target_thread_id,
        )

    async def aprune(
        self,
        thread_ids: Sequence[str],
        *,
        strategy: str = "keep_latest",
    ) -> None:
        owners: dict[str, list[str]] = {}
        for thread_id in thread_ids:
            owner = await database.get_thread_owner(thread_id)
            if owner is not None:
                owners.setdefault(owner, []).append(thread_id)
        for owner, owned_threads in owners.items():
            await (await self._saver(owner)).aprune(
                owned_threads,
                strategy=strategy,
            )

    async def aclose(self) -> None:
        connections = list(self._connections.values())
        self._savers.clear()
        self._connections.clear()
        self._locks.clear()
        for connection in connections:
            await connection.close()


@asynccontextmanager
async def create_user_checkpointer() -> AsyncIterator[UserScopedSqliteCheckpointer]:
    """Create the Agent Server-level checkpointer; graphs must not embed one."""

    checkpointer = UserScopedSqliteCheckpointer(get_settings().artifact_root)
    try:
        yield checkpointer
    finally:
        await checkpointer.aclose()
