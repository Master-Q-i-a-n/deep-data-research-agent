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
        "/skills/",
        "/persisted-skills/",
    }
    assert set(crawl.routes) == {
        "/state/",
        "/skills/",
        "/persisted-skills/",
    }
    assert supervisor.routes["/skills/"] is crawl.routes["/skills/"]
    # /skill-manage/ 不配置路由，直接由默认 OpenSandbox 处理。
    assert "/skill-manage/" not in supervisor.routes
    assert "/skill-manage/" not in crawl.routes
    assert all(
        backend.artifacts_root == "/state"
        for backend in (supervisor, crawl)
    )


def test_persisted_skill_route_uses_store_backend(initialized_backends) -> None:
    backend = backends.create_backend(_runtime("thread-a"))

    assert isinstance(backend.routes["/persisted-skills/"], StoreBackend)
    assert "/user-skills/" not in backend.routes
    assert "/memories/" not in backend.routes


@pytest.mark.asyncio
async def test_async_skill_read_does_not_block_event_loop(
    initialized_backends,
) -> None:
    with blockbuster_ctx(scanned_modules=["deep_data_research_agent"]):
        # 回归：backend factory 在 before_agent 中执行，不能重新解析本地路径。
        backend = backends.create_backend(_runtime("thread-async"))
        result = await backend.als("/skills/supervisor/")

    assert result.error is None
    assert result.entries
    assert result.entries[0]["path"].endswith("/evidence-reporting/")


def test_backend_exposes_worker_and_supervisor_skills(
    initialized_backends,
) -> None:
    backend = backends.create_backend(_runtime("thread-1"))

    assert backend.ls("/skills/worker/").entries[0]["path"].endswith(
        "/tavily-crawling/"
    )
    assert backend.ls("/skills/supervisor/").entries[0]["path"].endswith(
        "/evidence-reporting/"
    )
