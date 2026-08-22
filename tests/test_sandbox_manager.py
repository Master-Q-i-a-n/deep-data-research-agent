import asyncio
import os
import threading
import time
from types import SimpleNamespace

import pytest
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
)
from deepagents_opensandbox import OpensandboxBackend
from opensandbox.exceptions import SandboxError, SandboxException

from deep_data_research_agent import backends, sandbox_manager
from deep_data_research_agent.config import Settings


class FakeSandbox:
    def __init__(self, sandbox_id: str, *, healthy: bool = True) -> None:
        self.id = sandbox_id
        self.healthy = healthy
        self.renew_count = 0
        self.closed = False
        self.destroyed = False

    def is_healthy(self) -> bool:
        return self.healthy

    def renew(self, _timeout) -> None:
        self.renew_count += 1

    def close(self) -> None:
        self.closed = True

    def destroy(self) -> None:
        self.destroyed = True


class FakeBackend:
    def __init__(self, sandbox_id: str) -> None:
        self.id = sandbox_id
        self.files: dict[str, bytes] = {}

    async def aexecute(self, _command: str) -> ExecuteResponse:
        return ExecuteResponse(output="", exit_code=0)

    async def aupload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        self.files.update(files)
        return [
            FileUploadResponse(path=path, error=None)
            for path, _content in files
        ]

    async def aglob(self, _pattern: str, path: str | None = None) -> GlobResult:
        assert path == "/workspace"
        return GlobResult(
            matches=[
                {
                    "path": file_path.removeprefix("/workspace/"),
                    "is_dir": False,
                }
                for file_path in self.files
            ]
        )

    async def adownload_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        return [
            FileDownloadResponse(
                path=path,
                content=self.files.get(path),
                error=None if path in self.files else "file_not_found",
            )
            for path in paths
        ]


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        artifact_root=tmp_path,
        open_sandbox_domain="127.0.0.1:8080",
        open_sandbox_api_key="test-key",
        open_sandbox_image="python:3.13-slim",
    )


def _handle(sandbox_id: str, *, healthy: bool = True):
    return sandbox_manager._SandboxHandle(
        sandbox=FakeSandbox(sandbox_id, healthy=healthy),
        backend=FakeBackend(sandbox_id),
    )


def test_first_thread_binding_requires_explicit_user_id(tmp_path) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))

    with pytest.raises(RuntimeError, match="显式 user_id"):
        manager.local_workspace_path("thread-a", "supervisor")

    assert "thread-a" not in manager._thread_users


def test_explicit_thread_binding_is_reused_but_cannot_be_reassigned(tmp_path) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))

    explicit = manager.local_workspace_path(
        "thread-a",
        "supervisor",
        user_id="user-a",
    )
    reused = manager.local_workspace_path("thread-a", "supervisor")

    assert explicit == reused
    assert manager._thread_users == {"thread-a": "user-a"}
    with pytest.raises(RuntimeError, match="不能绑定到不同用户"):
        manager.local_workspace_path(
            "thread-a",
            "supervisor",
            user_id="user-b",
        )


def test_opensandbox_backend_preserves_multiline_json_output() -> None:
    """DeepAgents glob must receive one parseable JSON object per line."""

    class FakeCommands:
        def run(self, _command, *, opts):
            assert opts is not None
            return SimpleNamespace(
                id="command-1",
                logs=SimpleNamespace(
                    stdout=[
                        SimpleNamespace(
                            text='{"path": "report.md", "is_dir": false}'
                        ),
                        SimpleNamespace(
                            text='{"path": "raw", "is_dir": true}'
                        ),
                    ],
                    stderr=[],
                ),
            )

        def get_command_status(self, _command_id):
            return SimpleNamespace(exit_code=0)

    sandbox = SimpleNamespace(id="sandbox-lines", commands=FakeCommands())
    backend = sandbox_manager._LinePreservingOpensandboxBackend(sandbox=sandbox)

    result = backend.glob("**/*", path="/workspace")

    assert result.error is None
    assert result.matches == [
        # DeepAgents 0.7 normalizes sandbox glob results to absolute paths.
        {"path": "/workspace/report.md", "is_dir": False},
        {"path": "/workspace/raw", "is_dir": True},
    ]


def test_runtime_thread_id_prefers_execution_info() -> None:
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="graph-thread"),
        config={"configurable": {"thread_id": "tool-thread"}},
    )

    assert sandbox_manager.thread_id_from_runtime(runtime) == "graph-thread"


def test_runtime_thread_id_falls_back_to_tool_config() -> None:
    runtime = SimpleNamespace(
        config={"configurable": {"thread_id": "tool-thread"}},
    )

    assert sandbox_manager.thread_id_from_runtime(runtime) == "tool-thread"


@pytest.mark.asyncio
async def test_missing_opensandbox_configuration_fails_explicitly(
    tmp_path,
) -> None:
    manager = sandbox_manager.SandboxManager(
        settings=Settings(_env_file=None, artifact_root=tmp_path),
    )

    with pytest.raises(RuntimeError, match="OPEN_SANDBOX_DOMAIN"):
        await manager.ensure("thread-a", user_id="user-a")


@pytest.mark.asyncio
async def test_same_thread_reuses_one_sandbox_under_concurrency(
    tmp_path,
    monkeypatch,
) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    created: list[str] = []

    async def fake_create(
        thread_id: str,
        component: str = "crawl-worker",
        network_enabled: bool = False,
    ):
        created.append(thread_id)
        await asyncio.sleep(0.01)
        handle = _handle(f"sandbox-{thread_id}")
        handle.component = component
        handle.network_enabled = network_enabled
        return handle

    monkeypatch.setattr(manager, "_create_handle", fake_create)
    manager.local_workspace_path("thread-a", "crawl-worker", user_id="user-a")

    first, second = await asyncio.gather(
        manager.ensure("thread-a"),
        manager.ensure("thread-a"),
    )

    assert first is second
    assert created == ["thread-a"]
    assert manager._get_handle("thread-a").sandbox.renew_count == 1


@pytest.mark.asyncio
async def test_different_threads_and_unhealthy_replacement_are_isolated(
    tmp_path,
    monkeypatch,
) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    created: list[str] = []

    async def fake_create(
        thread_id: str,
        component: str = "crawl-worker",
        network_enabled: bool = False,
    ):
        created.append(thread_id)
        handle = _handle(f"sandbox-{thread_id}-{len(created)}")
        handle.component = component
        handle.network_enabled = network_enabled
        return handle

    monkeypatch.setattr(manager, "_create_handle", fake_create)

    first = await manager.ensure("thread-a", user_id="user-a")
    second = await manager.ensure("thread-b", user_id="user-a")
    stale = manager._get_handle("thread-a")
    stale.sandbox.healthy = False
    replacement = await manager.ensure("thread-a")

    assert first.id != second.id
    assert replacement.id != first.id
    assert stale.sandbox.closed is True
    assert created == ["thread-a", "thread-b", "thread-a"]


@pytest.mark.asyncio
async def test_sync_sandbox_creation_is_offloaded_from_event_loop(
    tmp_path,
    monkeypatch,
) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    events: list[str] = []

    def blocking_create(
        _thread_id: str,
        _component: str,
        _network_enabled: bool,
    ):
        events.append("create-start")
        time.sleep(0.05)
        events.append("create-end")
        return FakeSandbox("sandbox-offloaded")

    async def ignore_directories(_handle, _directories) -> None:
        return None

    async def ignore_restore(
        _thread_id,
        _handle=None,
        *,
        component="crawl-worker",
    ) -> int:
        assert component == "crawl-worker"
        return 0

    async def fake_base_distributions(_handle) -> frozenset[str]:
        return frozenset({"pip"})

    async def heartbeat() -> None:
        await asyncio.sleep(0.01)
        events.append("event-loop-tick")

    monkeypatch.setattr(manager, "_create_sandbox_sync", blocking_create)
    monkeypatch.setattr(manager, "_ensure_directories", ignore_directories)
    monkeypatch.setattr(
        manager,
        "_capture_base_distributions",
        fake_base_distributions,
    )
    monkeypatch.setattr(manager, "restore_workspace", ignore_restore)

    await asyncio.gather(manager._create_handle("thread-a"), heartbeat())

    assert events.index("event-loop-tick") < events.index("create-end")


@pytest.mark.asyncio
async def test_sandbox_cold_starts_are_serialized_process_wide(
    tmp_path,
    monkeypatch,
) -> None:
    """不同 manager 的 SandboxSync.create 也必须经过同一个进程级信号量。"""

    monkeypatch.setattr(
        sandbox_manager,
        "_SANDBOX_CREATE_SEMAPHORE",
        asyncio.Semaphore(1),
    )
    managers = [
        sandbox_manager.SandboxManager(settings=_settings(tmp_path)),
        sandbox_manager.SandboxManager(settings=_settings(tmp_path)),
    ]
    active = 0
    peak = 0
    counter_lock = threading.Lock()

    def blocking_create(thread_id, _component, _network_enabled):
        nonlocal active, peak
        with counter_lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with counter_lock:
            active -= 1
        return FakeSandbox(f"sandbox-{thread_id}")

    async def ignore_directories(_handle, _directories) -> None:
        return None

    async def fake_base_distributions(_handle) -> frozenset[str]:
        return frozenset()

    async def ignore_restore(
        _thread_id,
        _handle=None,
        *,
        component="crawl-worker",
    ) -> int:
        assert component == "crawl-worker"
        return 0

    for manager in managers:
        monkeypatch.setattr(manager, "_create_sandbox_sync", blocking_create)
        monkeypatch.setattr(manager, "_ensure_directories", ignore_directories)
        monkeypatch.setattr(
            manager,
            "_capture_base_distributions",
            fake_base_distributions,
        )
        monkeypatch.setattr(manager, "restore_workspace", ignore_restore)

    await asyncio.gather(
        managers[0]._create_handle("thread-a"),
        managers[1]._create_handle("thread-b"),
    )

    assert peak == 1


@pytest.mark.asyncio
async def test_sandbox_start_failure_retries_twice(
    tmp_path,
    monkeypatch,
) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    attempts = 0
    delays: list[float] = []

    def flaky_create(_thread_id, _component, _network_enabled):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise SandboxException(
                "Create sandbox failed: Egress sidecar container failed to start.",
                error=SandboxError(
                    "DOCKER::SANDBOX_START_FAILED",
                    "Failed to start egress sidecar",
                ),
            )
        return FakeSandbox("sandbox-retried")

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async def ignore_directories(_handle, _directories) -> None:
        return None

    async def fake_base_distributions(_handle) -> frozenset[str]:
        return frozenset()

    async def ignore_restore(
        _thread_id,
        _handle=None,
        *,
        component="crawl-worker",
    ) -> int:
        return 0

    monkeypatch.setattr(
        sandbox_manager,
        "_SANDBOX_CREATE_SEMAPHORE",
        asyncio.Semaphore(1),
    )
    monkeypatch.setattr(manager, "_create_sandbox_sync", flaky_create)
    monkeypatch.setattr(manager, "_ensure_directories", ignore_directories)
    monkeypatch.setattr(
        manager,
        "_capture_base_distributions",
        fake_base_distributions,
    )
    monkeypatch.setattr(manager, "restore_workspace", ignore_restore)
    monkeypatch.setattr(sandbox_manager.asyncio, "sleep", fake_sleep)

    handle = await manager._create_handle("thread-a")

    assert handle.backend.id == "sandbox-retried"
    assert attempts == 3
    assert delays == [1.0, 3.0]


@pytest.mark.asyncio
async def test_sandbox_create_does_not_retry_nontransient_errors(
    tmp_path,
    monkeypatch,
) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    attempts = 0

    def invalid_create(_thread_id, _component, _network_enabled):
        nonlocal attempts
        attempts += 1
        raise ValueError("镜像名称无效")

    monkeypatch.setattr(
        sandbox_manager,
        "_SANDBOX_CREATE_SEMAPHORE",
        asyncio.Semaphore(1),
    )
    monkeypatch.setattr(manager, "_create_sandbox_sync", invalid_create)

    with pytest.raises(RuntimeError, match="镜像名称无效"):
        await manager._create_handle("thread-a")

    assert attempts == 1


@pytest.mark.asyncio
async def test_export_and_restore_workspace(tmp_path) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    handle = _handle("sandbox-1")
    handle.backend.files = {
        "/workspace/report.md": "中文报告".encode(),
        "/workspace/raw/page.md": b"raw page",
    }
    manager._handles[
        manager._key("thread-a", "crawl-worker", "user-a")
    ] = handle

    exported = await manager.export_workspace("thread-a")

    assert len(exported) == 2
    assert {item["path"] for item in exported} == {
        "/workspace/report.md",
        "/workspace/raw/page.md",
    }
    assert (
        tmp_path
        / "user-a"
        / "jobs"
        / "thread-a"
        / "crawl-worker"
        / "workspace"
        / "report.md"
    ).read_text(encoding="utf-8") == "中文报告"

    restored_handle = _handle("sandbox-2")
    restored = await manager.restore_workspace("thread-a", restored_handle)

    assert restored == 2
    assert restored_handle.backend.files["/workspace/raw/page.md"] == b"raw page"


@pytest.mark.asyncio
async def test_component_workspaces_do_not_collide(tmp_path) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    crawl_handle = _handle("crawl-box")
    crawl_handle.backend.files = {"/workspace/report.md": b"crawl"}
    supervisor_handle = _handle("supervisor-box")
    supervisor_handle.backend.files = {"/workspace/report.md": b"supervisor"}
    manager._handles[
        manager._key("shared", "crawl-worker", "user-a")
    ] = crawl_handle
    manager._handles[manager._key("shared", "supervisor")] = supervisor_handle

    await manager.export_workspace("shared", component="crawl-worker")
    await manager.export_workspace("shared", component="supervisor")

    assert (
        tmp_path
        / "user-a"
        / "jobs"
        / "shared"
        / "crawl-worker"
        / "workspace"
        / "report.md"
    ).read_bytes() == b"crawl"
    assert (
        tmp_path
        / "user-a"
        / "jobs"
        / "shared"
        / "supervisor"
        / "workspace"
        / "report.md"
    ).read_bytes() == b"supervisor"


@pytest.mark.asyncio
async def test_replace_directory_uploads_physical_skill_copy(tmp_path) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    handle = _handle("sandbox-1")
    manager._handles[
        manager._key("thread-a", "supervisor", "user-a")
    ] = handle

    count = await manager.replace_directory_files(
        "thread-a",
        "/skills/user/supervisor/active",
        [("demo/SKILL.md", b"demo")],
        component="supervisor",
    )

    assert count == 1
    assert handle.backend.files[
        "/skills/user/supervisor/active/demo/SKILL.md"
    ] == b"demo"


def test_worker_backend_uses_sandbox_as_default(tmp_path, monkeypatch) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    sandbox = FakeSandbox("sandbox-1")
    manager._handles[
        manager._key("thread-a", "crawl-worker", "user-a")
    ] = sandbox_manager._SandboxHandle(
        sandbox=sandbox,
        backend=OpensandboxBackend(sandbox=sandbox),
    )
    monkeypatch.setattr(sandbox_manager, "SANDBOX_MANAGER", manager)

    backend = backends.create_worker_backend(
        SimpleNamespace(
            config={
                "configurable": {
                    "thread_id": "thread-a",
                    "langgraph_auth_user_id": "user-a",
                }
            },
        )
    )

    assert isinstance(backend.default, backends.RestartSafeSandboxBackend)
    assert backend.default._backend().id == "sandbox-1"
    assert set(backend.routes) == {
        "/state/",
        "/memories/agent/crawl-worker/",
        "/memories/user/",
        "/skills/public/crawl-worker/",
        "/skills/user/crawl-worker/",
    }
    assert backend.artifacts_root == "/state"


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_OPENSANDBOX_INTEGRATION") != "1",
    reason="需要显式启用真实 OpenSandbox 集成测试",
)
async def test_real_opensandbox_write_execute_and_export(tmp_path) -> None:
    manager = sandbox_manager.SandboxManager(artifact_root=tmp_path)
    thread_id = "integration-thread"
    try:
        backend = await manager.ensure(thread_id, user_id="local-user")
        await manager.upload_workspace_files(
            thread_id,
            [("/workspace/hello.py", b"print('sandbox-ok')")],
        )
        result = await backend.aexecute("cd /workspace && python hello.py")
        assert result.exit_code == 0
        assert "sandbox-ok" in result.output
        assert len(await manager.export_workspace(thread_id)) == 1
        assert (
            tmp_path
            / "local-user"
            / "jobs"
            / thread_id
            / "crawl-worker"
            / "workspace"
            / "hello.py"
        ).is_file()
    finally:
        handle = manager._handles.get(manager._key(thread_id, "crawl-worker"))
        if handle is not None:
            await asyncio.to_thread(handle.sandbox.destroy)


@pytest.mark.asyncio
async def test_upload_can_persist_exact_file_into_local_snapshot(tmp_path) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    key = manager._key("thread-upload", "supervisor", "user-a")
    handle = _handle("sandbox-upload")
    manager._handles[key] = handle

    await manager.upload_workspace_files(
        "thread-upload",
        [("/workspace/input/orders.csv", b"id,amount\n001,20\n")],
        component="supervisor",
        persist=True,
    )

    snapshot = manager.local_workspace_path(
        "thread-upload",
        "supervisor",
        user_id="user-a",
    )
    assert (snapshot / "input" / "orders.csv").read_bytes() == b"id,amount\n001,20\n"
    assert handle.backend.files["/workspace/input/orders.csv"] == b"id,amount\n001,20\n"


@pytest.mark.asyncio
async def test_delete_workspace_file_removes_local_snapshot(tmp_path) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    key = manager._key("thread-delete-file", "supervisor", "user-a")
    manager._handles[key] = _handle("sandbox-delete-file")
    snapshot = manager.local_workspace_path(
        "thread-delete-file",
        "supervisor",
        user_id="user-a",
    )
    target = snapshot / "input" / "orders.csv"
    target.parent.mkdir(parents=True)
    target.write_text("id\n1\n", encoding="utf-8")

    await manager.delete_workspace_file(
        "thread-delete-file",
        "/workspace/input/orders.csv",
        component="supervisor",
    )

    assert not target.exists()


@pytest.mark.asyncio
async def test_delete_thread_resources_is_user_and_thread_scoped(tmp_path) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    target_key = manager._key("thread-a", "supervisor", "user-a")
    sibling_key = manager._key("thread-b", "supervisor", "user-a")
    target = _handle("target")
    sibling = _handle("sibling")
    manager._handles[target_key] = target
    manager._handles[sibling_key] = sibling
    target_job = tmp_path / "user-a" / "jobs" / "thread-a"
    sibling_job = tmp_path / "user-a" / "jobs" / "thread-b"
    (target_job / "supervisor" / "workspace").mkdir(parents=True)
    (sibling_job / "supervisor" / "workspace").mkdir(parents=True)

    await manager.delete_thread_resources("thread-a", user_id="user-a")

    assert target.sandbox.destroyed is True
    assert sibling.sandbox.destroyed is False
    assert target_key not in manager._handles
    assert sibling_key in manager._handles
    assert not target_job.exists()
    assert sibling_job.exists()


@pytest.mark.asyncio
async def test_worker_and_supervisor_components_use_separate_network_profiles(
    tmp_path,
    monkeypatch,
) -> None:
    manager = sandbox_manager.SandboxManager(settings=_settings(tmp_path))
    created: list[tuple[str, str, bool]] = []

    async def fake_create(
        thread_id: str,
        component: str = "crawl-worker",
        network_enabled: bool = False,
    ):
        created.append((thread_id, component, network_enabled))
        handle = _handle(f"{component}-{thread_id}")
        handle.component = component
        handle.network_enabled = network_enabled
        return handle

    monkeypatch.setattr(manager, "_create_handle", fake_create)

    crawl = await manager.ensure("shared-thread", user_id="user-a")
    supervisor = await manager.ensure(
        "shared-thread",
        component="supervisor",
        network_enabled=True,
    )

    assert crawl is not supervisor
    assert created == [
        ("shared-thread", "crawl-worker", False),
        ("shared-thread", "supervisor", True),
    ]
