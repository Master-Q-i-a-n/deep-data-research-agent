from types import SimpleNamespace

import pytest
from blockbuster import blockbuster_ctx
from deepagents.backends import StoreBackend
from deepagents_opensandbox import OpensandboxBackend

from deep_data_research_agent import backends, sandbox_manager


def _runtime(thread_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
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

    def get_backend(_thread_id: str, *, component: str = "crawl-worker"):
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

    assert supervisor.default is initialized_backends["supervisor"]
    assert crawl.default is initialized_backends["crawl-worker"]
    assert set(supervisor.routes) == {
        "/state/",
        "/memories/agent/",
        "/memories/user/",
        "/skills/public/supervisor/",
        "/skills/user/supervisor/",
        "/skills/public/data-analyst/",
        "/skills/user/data-analyst/",
    }
    assert set(crawl.routes) == {
        "/state/",
        "/memories/agent/",
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
    assert "/memories/agent/" in backend.routes
    assert isinstance(backend.routes["/memories/user/"], StoreBackend)


def test_backend_construction_does_not_read_seed_files(
    initialized_backends,
) -> None:
    with blockbuster_ctx(scanned_modules=["deep_data_research_agent"]):
        backends.create_backend(_runtime("thread-1"))
        backends.create_worker_backend(_runtime("thread-2"))


def test_supervisor_file_tools_cannot_overwrite_uploaded_inputs() -> None:
    assert backends.FILESYSTEM_PERMISSIONS[0].__dict__ == {
        "operations": ["write"],
        "paths": ["/workspace/input/**"],
        "mode": "deny",
    }
    assert backends.FILESYSTEM_PERMISSIONS[1].__dict__ == {
        "operations": ["read"],
        "paths": ["/workspace/input/**"],
        "mode": "allow",
    }
