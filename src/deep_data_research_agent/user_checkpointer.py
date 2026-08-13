"""PostgreSQL checkpointer with application-level thread ownership checks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from deep_data_research_agent import database, sandbox_manager
from deep_data_research_agent.config import get_settings


def configured_user_id(config: RunnableConfig | None) -> str | None:
    """Extract the authenticated identity injected by LangGraph Server."""

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


class UserOwnedPostgresCheckpointer(BaseCheckpointSaver):
    """Delegate checkpoints to PostgreSQL after enforcing thread ownership."""

    def __init__(self, saver: AsyncPostgresSaver) -> None:
        # Keep this wrapper's serializer aligned with the official saver.
        super().__init__(serde=saver.serde)
        self._saver = saver

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
        await self._user_for_config(config)
        return await self._saver.aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        await self._user_for_config(config)
        async for item in self._saver.alist(
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
        await self._user_for_config(config, claim=True)
        return await self._saver.aput(
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
        await self._user_for_config(config, claim=True)
        await self._saver.aput_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        owner = await database.get_thread_owner(thread_id)
        if owner is None:
            return
        await self._saver.adelete_thread(thread_id)
        # Explicit thread deletion also removes its artifacts and live sandbox.
        await sandbox_manager.SANDBOX_MANAGER.delete_thread_resources(
            thread_id,
            user_id=owner,
        )
        await database.delete_thread_claim(thread_id, owner)

    async def adelete_for_runs(self, run_ids: Sequence[str]) -> None:
        # Run IDs alone do not carry a trusted owner, so do not bypass isolation.
        raise NotImplementedError("按 run_id 删除缺少可信的 thread 归属信息")

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        owner = await database.get_thread_owner(source_thread_id)
        if owner is None:
            raise RuntimeError("源会话不存在")
        target_owner = await database.get_thread_owner(target_thread_id)
        await database.claim_thread(target_thread_id, owner)
        try:
            await self._saver.acopy_thread(source_thread_id, target_thread_id)
        except Exception:
            # Only roll back a claim created by this copy attempt. An existing
            # target claim must survive a delegate failure.
            if target_owner is None:
                await database.delete_thread_claim(target_thread_id, owner)
            raise

    async def aprune(
        self,
        thread_ids: Sequence[str],
        *,
        strategy: str = "keep_latest",
    ) -> None:
        # Only forward threads already registered in the ownership table.
        owned_threads = [
            thread_id
            for thread_id in thread_ids
            if await database.get_thread_owner(thread_id) is not None
        ]
        if owned_threads:
            await self._saver.aprune(owned_threads, strategy=strategy)


@asynccontextmanager
async def create_user_checkpointer() -> AsyncIterator[UserOwnedPostgresCheckpointer]:
    """Create one production PostgreSQL saver for the Agent Server lifespan."""

    settings = get_settings()
    uri = settings.postgres_uri.strip()
    if not uri:
        raise RuntimeError("POSTGRES_URI 未配置，无法初始化 PostgreSQL 检查点")
    if settings.postgres_checkpoint_pool_max_size < (
        settings.postgres_checkpoint_pool_min_size
    ):
        raise RuntimeError("PostgreSQL 检查点连接池最大值不能小于最小值")

    pool = AsyncConnectionPool(
        conninfo=database.psycopg_postgres_uri(uri),
        min_size=settings.postgres_checkpoint_pool_min_size,
        max_size=settings.postgres_checkpoint_pool_max_size,
        timeout=settings.postgres_pool_timeout_seconds,
        open=False,
        kwargs={
            # AsyncPostgresSaver setup and migrations require autocommit.
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
    )
    await pool.open(wait=True)
    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    try:
        yield UserOwnedPostgresCheckpointer(saver)
    finally:
        await pool.close()
