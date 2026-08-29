from __future__ import annotations

import json

import pytest

from deep_data_research_agent.api import app as webapp
from deep_data_research_agent.api import health


@pytest.mark.asyncio
async def test_liveness_has_no_dependency_calls(monkeypatch) -> None:
    async def forbidden() -> dict[str, str]:
        raise AssertionError("liveness must not run readiness checks")

    monkeypatch.setattr(webapp, "readiness_checks", forbidden)
    assert await webapp.health_live() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readiness_returns_safe_component_status(monkeypatch) -> None:
    async def checks() -> dict[str, str]:
        return {"postgres": "ok", "redis": "timeout", "mongodb": "ok"}

    monkeypatch.setattr(webapp, "readiness_checks", checks)
    response = await webapp.health_ready()

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert json.loads(response.body) == {
        "status": "not_ready",
        "checks": {"postgres": "ok", "redis": "timeout", "mongodb": "ok"},
    }


@pytest.mark.asyncio
async def test_readiness_returns_200_when_all_dependencies_are_ready(monkeypatch) -> None:
    async def checks() -> dict[str, str]:
        return {"postgres": "ok", "redis": "ok", "mongodb": "ok"}

    monkeypatch.setattr(webapp, "readiness_checks", checks)
    response = await webapp.health_ready()

    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "ready"


@pytest.mark.asyncio
async def test_readiness_includes_workspace_storage(monkeypatch) -> None:
    checked: list[str] = []

    async def ready() -> None:
        return None

    async def workspace_ready() -> None:
        checked.append("workspace")

    monkeypatch.setattr(health.database, "check_database_ready", ready)
    monkeypatch.setattr(health, "ping_redis", ready)
    monkeypatch.setattr(health, "ping_mongodb", ready)
    monkeypatch.setattr(health, "check_encryption_ready", lambda: None)
    monkeypatch.setattr(
        health.sandbox_manager.SANDBOX_MANAGER.workspace_store,
        "check_ready",
        workspace_ready,
    )

    checks = await health.readiness_checks()

    assert checks == {
        "postgres": "ok",
        "redis": "ok",
        "mongodb": "ok",
        "workspace_storage": "ok",
        "provider_encryption": "ok",
    }
    assert checked == ["workspace"]


@pytest.mark.asyncio
async def test_probe_failures_do_not_expose_exception_text() -> None:
    async def fail() -> None:
        raise RuntimeError("mongodb://secret-host")

    assert await health._run_check(fail, 1) == "error"
