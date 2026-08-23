from types import SimpleNamespace

import pytest
from blockbuster import blockbuster_ctx
from deepagents.backends import StoreBackend
from deepagents_opensandbox import OpensandboxBackend

from deep_data_research_agent.agents import backends
from deep_data_research_agent.infrastructure.sandbox import manager as sandbox_manager


def _runtime(thread_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        server_info=SimpleNamespace(
            user=SimpleNamespace(identity="test-user"),
        ),
        state={},
    )


def _sandbox_backend(sandbox_id: str) -> OpensandboxBackend:
    return OpensandboxBackend(sandbox=SimpleNamespace(id=sandbox_id))


@pytest.fixture
def initialized_backends(monkeypatch):
    values = {
        "supervisor": _sandbox_backend("supervisor-box"),
        "crawl-worker": _sandbox_backend("crawl-box"),
    }

    def get_backend(
        _thread_id: str,
        *,
        component: str = "crawl-worker",
        user_id: str | None = None,
    ):
        assert user_id == "test-user"
        return values[component]

    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER,
        "get_backend",
        get_backend,
    )
    return values


def test_agent_backends_use_component_sandbox(initialized_backends) -> None:
    runtime = _runtime("thread-a")

    supervisor = backends.create_backend(runtime)
    crawl = backends.create_worker_backend(runtime)

    assert isinstance(supervisor.default, backends.RestartSafeSandboxBackend)
    assert isinstance(crawl.default, backends.RestartSafeSandboxBackend)
    assert supervisor.default._backend() is initialized_backends["supervisor"]
    assert crawl.default._backend() is initialized_backends["crawl-worker"]
    assert set(supervisor.routes) == {
        "/state/",
        "/memories/user/",
        "/memories/agent/supervisor/",
        "/memories/agent/data-analyst/",
        "/skills/public/supervisor/",
        "/skills/user/supervisor/",
        "/skills/public/data-analyst/",
        "/skills/user/data-analyst/",
    }
    assert set(crawl.routes) == {
        "/state/",
        "/memories/agent/crawl-worker/",
        "/memories/user/",
        "/skills/public/crawl-worker/",
        "/skills/user/crawl-worker/",
    }
    # /skill-manage/ 不配置路由，直接由默认 OpenSandbox 处理。
    assert "/skill-manage/" not in supervisor.routes
    assert "/skill-manage/" not in crawl.routes
    assert all(
        backend.artifacts_root == "/state"
        for backend in (supervisor, crawl)
    )


def test_agent_skill_routes_use_isolated_store_backends(initialized_backends) -> None:
    backend = backends.create_backend(_runtime("thread-a"))

    for root in (
        "/skills/public/supervisor/",
        "/skills/user/supervisor/",
        "/skills/public/data-analyst/",
        "/skills/user/data-analyst/",
    ):
        assert isinstance(backend.routes[root], StoreBackend)
        assert callable(backend.routes[root]._namespace)
    public_route = backend.routes["/skills/public/supervisor/"]
    assert public_route._namespace(_runtime("thread-a")) == (
        "public",
        "skills",
        "supervisor",
    )
    user_route = backend.routes["/skills/user/data-analyst/"]
    assert user_route._namespace(_runtime("thread-a"))[1:] == (
        "skills",
        "data-analyst",
    )
    assert backend.routes["/memories/agent/supervisor/"]._namespace(
        _runtime("thread-a")
    ) == ("public", "memories", "supervisor")
    assert backend.routes["/memories/agent/data-analyst/"]._namespace(
        _runtime("thread-a")
    ) == ("public", "memories", "data-analyst")
    assert backend.routes["/memories/user/"]._namespace(
        _runtime("thread-a")
    )[1:] == ("memories", "user")
    assert isinstance(backend.routes["/memories/user/"], StoreBackend)


def test_backend_construction_does_not_read_seed_files(
    initialized_backends,
) -> None:
    with blockbuster_ctx(scanned_modules=["deep_data_research_agent"]):
        backends.create_backend(_runtime("thread-1"))
        backends.create_worker_backend(_runtime("thread-2"))


@pytest.mark.asyncio
async def test_backend_recreates_sandbox_after_process_restart(monkeypatch) -> None:
    restored = _sandbox_backend("restored-box")
    calls: list[dict[str, object]] = []

    def missing_backend(*_args, **_kwargs):
        raise sandbox_manager.SandboxNotInitializedError("not initialized")

    async def ensure(*_args, **kwargs):
        calls.append(kwargs)
        return restored

    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER,
        "get_backend",
        missing_backend,
    )
    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "ensure", ensure)

    backend = backends.create_backend(_runtime("resumed-thread"))
    assert await backend.default._abackend() is restored
    assert calls == [
        {
            "component": "supervisor",
            "network_enabled": True,
            "user_id": "test-user",
        }
    ]


def test_supervisor_file_tools_cannot_overwrite_uploaded_inputs() -> None:
    backend = backends.create_backend(_runtime("thread-input-protection"))

    write_result = backend.default.write("/workspace/input/orders.csv", "changed")
    edit_result = backend.default.edit(
        "/workspace/input/orders.csv",
        "old",
        "new",
    )

    assert "read-only" in str(write_result.error)
    assert "read-only" in str(edit_result.error)


def test_persisted_skill_and_memory_routes_are_read_only() -> None:
    backend = backends.create_backend(_runtime("thread-store-protection"))

    for root in (
        "/memories/user/",
        "/memories/agent/supervisor/",
        "/skills/public/supervisor/",
        "/skills/user/data-analyst/",
    ):
        route = backend.routes[root]
        assert isinstance(route, backends.ReadOnlyStoreBackend)
        assert "read-only" in str(route.write("/blocked.md", "content").error)
