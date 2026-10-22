import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from opensandbox import AcquirePolicy
from opensandbox.exceptions import PoolEmptyException
from test_sandbox_manager import (
    FakeBackend,
    FakeCoordinationRedis,
    FakeSandbox,
    _settings,
)

from deep_data_research_agent.infrastructure.redis.lock import (
    DistributedLockUnavailable,
)
from deep_data_research_agent.infrastructure.sandbox import manager as sandbox_manager
from deep_data_research_agent.infrastructure.sandbox.pool import (
    BASELINE_PATH,
    WarmSandboxPool,
)


class PooledSandbox(FakeSandbox):
    def __init__(self, sandbox_id="warm-1"):
        super().__init__(sandbox_id)
        self.metadata = {}

    def patch_metadata(self, patch):
        self.metadata.update(patch)


def install_pool(manager, monkeypatch, sandbox=None):
    sandbox = sandbox or PooledSandbox()
    backend = FakeBackend(sandbox.id)

    async def execute(command):
        assert command == "cat " + BASELINE_PATH
        return SimpleNamespace(output='["pip", "pandas"]', exit_code=0)

    backend.aexecute = execute
    monkeypatch.setattr(
        sandbox_manager, "_LinePreservingOpensandboxBackend", lambda **kw: backend
    )
    monkeypatch.setattr(manager, "restore_workspace", AsyncMock(return_value=0))
    manager._pool = SimpleNamespace(
        sdk=SimpleNamespace(acquire=Mock(return_value=sandbox))
    )
    manager._user_for_thread("thread-a", "user-a")
    return sandbox, backend


@pytest.mark.asyncio
async def test_warm_hit_binds_metadata_without_cold_creation(tmp_path, monkeypatch):
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    sandbox, backend = install_pool(manager, monkeypatch)
    cold = Mock(side_effect=AssertionError("cold create on pool hit"))
    monkeypatch.setattr(manager, "_create_sandbox_sync", cold)
    result = await manager.ensure("thread-a", component="supervisor")
    assert result is backend
    assert sandbox.metadata["component"] == "supervisor"
    assert sandbox.metadata["thread_id"] == "thread-a"
    assert sandbox.metadata["ddra_ready"] == "true"
    assert manager.base_distributions("thread-a", component="supervisor") == {
        "pip",
        "pandas",
    }
    assert (
        manager._pool.sdk.acquire.call_args.kwargs["policy"]
        is AcquirePolicy.RETRY_NEXT_IDLE
    )
    cold.assert_not_called()
    assert await manager.ensure("thread-a", component="supervisor") is backend
    assert manager._pool.sdk.acquire.call_count == 1


@pytest.mark.asyncio
async def test_empty_pool_falls_back_to_normal_creation(tmp_path, monkeypatch):
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    manager._pool = SimpleNamespace(
        sdk=SimpleNamespace(acquire=Mock(side_effect=PoolEmptyException("empty")))
    )
    sandbox = FakeSandbox("cold")
    cold = Mock(return_value=sandbox)
    monkeypatch.setattr(manager, "_create_sandbox_sync", cold)
    monkeypatch.setattr(manager, "_ensure_directories", AsyncMock())
    monkeypatch.setattr(
        manager,
        "_capture_base_distributions",
        AsyncMock(return_value=frozenset({"pip"})),
    )
    monkeypatch.setattr(manager, "restore_workspace", AsyncMock(return_value=0))
    backend = await manager.ensure("thread-a", user_id="user-a")
    assert backend.id == "cold"
    cold.assert_called_once_with("thread-a", "crawl-worker", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["metadata", "restore", "baseline"])
async def test_warm_initialization_failure_destroys_borrowed_instance(
    tmp_path, monkeypatch, failure
):
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    sandbox, backend = install_pool(manager, monkeypatch)
    if failure == "metadata":
        sandbox.patch_metadata = Mock(side_effect=RuntimeError("metadata"))
    elif failure == "restore":
        manager.restore_workspace.side_effect = RuntimeError("restore")
    else:
        backend.aexecute = AsyncMock(
            return_value=SimpleNamespace(output="null", exit_code=0)
        )
    with pytest.raises(RuntimeError):
        await manager.ensure("thread-a")
    assert sandbox.destroyed
    assert not manager._handles


@pytest.mark.asyncio
async def test_cancellation_waits_for_acquire_then_disposes(tmp_path, monkeypatch):
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    sandbox, _ = install_pool(manager, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def acquire(**kwargs):
        entered.set()
        assert release.wait(3)
        return sandbox

    manager._pool.sdk.acquire.side_effect = acquire
    task = asyncio.create_task(manager.ensure("thread-a"))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sandbox.destroyed
    assert not manager._handles


@pytest.mark.asyncio
async def test_redis_lock_failure_never_acquires_from_pool(tmp_path, monkeypatch):
    manager = sandbox_manager.SandboxManager(
        settings=_settings(tmp_path), coordination_enabled=True
    )
    install_pool(manager, monkeypatch)
    monkeypatch.setattr(
        sandbox_manager,
        "initialize_redis",
        AsyncMock(side_effect=RuntimeError("redis")),
    )
    with pytest.raises(DistributedLockUnavailable):
        await manager.ensure("thread-a")
    manager._pool.sdk.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_registration_failure_destroys_warm_instance(tmp_path, monkeypatch):
    manager = sandbox_manager.SandboxManager(
        settings=_settings(tmp_path), coordination_enabled=True
    )
    sandbox, _ = install_pool(manager, monkeypatch)

    @asynccontextmanager
    async def lock(*args, **kwargs):
        yield SimpleNamespace(ensure_owned=lambda: None)

    monkeypatch.setattr(sandbox_manager, "distributed_lock", lock)
    monkeypatch.setattr(sandbox_manager, "initialize_redis", AsyncMock())
    monkeypatch.setattr(sandbox_manager, "get_redis", lambda: FakeCoordinationRedis())
    monkeypatch.setattr(manager, "_list_sandboxes_sync", lambda _: [])
    monkeypatch.setattr(
        manager, "_write_registry", AsyncMock(side_effect=RuntimeError("registry"))
    )
    with pytest.raises(RuntimeError, match="registry"):
        await manager.ensure("thread-a")
    assert sandbox.destroyed
    assert not manager._handles


@pytest.mark.asyncio
async def test_export_of_unused_thread_never_creates(tmp_path, monkeypatch):
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    monkeypatch.setattr(
        manager, "_create_handle", AsyncMock(side_effect=AssertionError("allocated"))
    )
    assert await manager.export_existing_workspace("unused", user_id="user-a") == []


@pytest.mark.asyncio
async def test_pool_shutdown_keeps_shared_idle_and_active_instances(tmp_path):
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    close = Mock()
    manager._pool = SimpleNamespace(close=close)
    await manager.close_pool()
    close.assert_called_once_with()
    assert manager._pool is None


@pytest.mark.asyncio
async def test_pool_startup_failure_keeps_lazy_cold_creation_available(
    tmp_path, monkeypatch
):
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    pool = SimpleNamespace(
        start=Mock(side_effect=RuntimeError("pool unavailable")), close=Mock()
    )
    monkeypatch.setattr(sandbox_manager, "WarmSandboxPool", Mock(return_value=pool))
    await manager.start_pool()
    assert manager._pool is None
    pool.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_cancelled_pool_startup_closes_background_scheduler(
    tmp_path, monkeypatch
):
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    entered, release = threading.Event(), threading.Event()

    def start():
        entered.set()
        assert release.wait(3)

    pool = SimpleNamespace(start=start, close=Mock())
    monkeypatch.setattr(sandbox_manager, "WarmSandboxPool", Mock(return_value=pool))
    task = asyncio.create_task(manager.start_pool())
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    pool.close.assert_called_once_with()
    assert manager._pool is None


@pytest.mark.asyncio
async def test_unused_thread_export_does_not_contact_provider(tmp_path, monkeypatch):
    manager = sandbox_manager.SandboxManager(
        settings=_settings(tmp_path), coordination_enabled=True
    )
    monkeypatch.setattr(sandbox_manager, "initialize_redis", AsyncMock())
    monkeypatch.setattr(sandbox_manager, "get_redis", lambda: FakeCoordinationRedis())
    find = Mock(side_effect=AssertionError("unused chat contacted sandbox server"))
    monkeypatch.setattr(manager, "_list_sandboxes_sync", find)
    assert await manager.export_existing_workspace("pure", user_id="user-a") == []
    find.assert_not_called()


@pytest.mark.asyncio
async def test_two_managers_borrow_distinct_instances_from_same_pool(
    tmp_path, monkeypatch
):
    instances = [PooledSandbox("shared-1"), PooledSandbox("shared-2")]
    sdk = SimpleNamespace(acquire=Mock(side_effect=instances))
    managers = [
        sandbox_manager.SandboxManager(settings=_settings(tmp_path)) for _ in range(2)
    ]

    def backend(*, sandbox):
        result = FakeBackend(sandbox.id)
        result.aexecute = AsyncMock(
            return_value=SimpleNamespace(output='["pip"]', exit_code=0)
        )
        return result

    monkeypatch.setattr(sandbox_manager, "_LinePreservingOpensandboxBackend", backend)
    for manager in managers:
        manager._pool = SimpleNamespace(sdk=sdk)
        monkeypatch.setattr(manager, "restore_workspace", AsyncMock(return_value=0))
    first, second = await asyncio.gather(
        managers[0].ensure(
            "thread-supervisor", component="supervisor", user_id="user-a"
        ),
        managers[1].ensure("thread-crawl", component="crawl-worker", user_id="user-a"),
    )
    assert first.id != second.id
    assert {box.metadata["component"] for box in instances} == {
        "supervisor",
        "crawl-worker",
    }
    assert all(
        manager._get_handle(thread, component).network_enabled
        for manager, thread, component in (
            (managers[0], "thread-supervisor", "supervisor"),
            (managers[1], "thread-crawl", "crawl-worker"),
        )
    )


@pytest.mark.asyncio
async def test_repeated_cancel_during_acquire_still_disposes(tmp_path, monkeypatch):
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    sandbox, _ = install_pool(manager, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def acquire(**kwargs):
        entered.set()
        assert release.wait(3)
        return sandbox

    manager._pool.sdk.acquire.side_effect = acquire
    task = asyncio.create_task(manager.ensure("thread-a"))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sandbox.destroyed


def test_pool_identity_and_network_are_shared_by_components(tmp_path, monkeypatch):
    from deep_data_research_agent.infrastructure.sandbox import pool

    monkeypatch.setattr(pool, "redis_password", lambda _: "x" * 32)
    redis = pool.Redis()
    monkeypatch.setattr(pool.Redis, "from_url", Mock(return_value=redis))
    sdk = Mock()
    monkeypatch.setattr(pool, "SandboxPoolSync", sdk)
    settings = _settings(tmp_path)
    first = WarmSandboxPool(settings, Mock())
    second = WarmSandboxPool(settings, Mock())
    assert first.name == second.name
    config = sdk.call_args.kwargs
    assert config["max_idle"] == 2
    assert config["warmup_concurrency"] == 1
    assert config["creation_spec"].network_policy.default_action == "allow"
    assert config["creation_spec"].metadata == {"ddra_pool": first.name}
    assert "user-a" not in json.dumps(config["creation_spec"].metadata)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_OPENSANDBOX_INTEGRATION") != "1",
    reason="需要显式启用真实 OpenSandbox 集成测试",
)
async def test_real_warm_pool_metadata_network_and_isolation(tmp_path, monkeypatch):
    from uuid import uuid4

    from opensandbox import InMemoryPoolStateStore

    from deep_data_research_agent.core.config import get_settings
    from deep_data_research_agent.infrastructure.sandbox import pool as sandbox_pool

    # 使用独立内存池状态，避免集成测试领取或销毁应用共享池中的空闲容器。
    monkeypatch.setattr(
        sandbox_pool, "RedisPoolStateStore", lambda *a, **kw: InMemoryPoolStateStore()
    )
    settings = get_settings().model_copy(
        update={
            "sandbox_pool_enabled": True,
            "sandbox_pool_max_idle": 2,
            "sandbox_pool_reconcile_interval_seconds": 1,
            "artifact_root": tmp_path,
        }
    )
    manager = sandbox_manager.SandboxManager(settings=settings)
    thread_id = "warm-integration-" + uuid4().hex
    pool = None
    try:
        await manager.start_pool()
        pool = manager._pool
        assert pool is not None
        deadline = time.monotonic() + 90
        while (await asyncio.to_thread(pool.sdk.snapshot)).idle_count < 2:
            assert time.monotonic() < deadline, "预热池未按时准备完成"
            await asyncio.sleep(0.2)
        first = await manager.ensure(
            thread_id, component="supervisor", user_id="integration-user"
        )
        second = await manager.ensure(thread_id, component="crawl-worker")
        assert first.id != second.id
        for component in ("supervisor", "crawl-worker"):
            handle = manager._get_handle(thread_id, component)
            info = await asyncio.to_thread(handle.sandbox.get_info)
            assert info.metadata["thread_id"] == thread_id
            assert info.metadata["component"] == component
            assert info.metadata["ddra_ready"] == "true"
            policy = await asyncio.to_thread(handle.sandbox.get_egress_policy)
            assert policy.default_action == "allow"
            result = await handle.backend.aexecute("python -c \"print('warm-ok')\"")
            assert result.exit_code == 0
            assert "warm-ok" in result.output
    finally:
        await manager.close_pool()
        if pool is not None:
            await asyncio.to_thread(pool.sdk.release_all_idle)
        for handle in manager._handles.values():
            await asyncio.to_thread(handle.sandbox.destroy)
        await manager.workspace_store.close()
