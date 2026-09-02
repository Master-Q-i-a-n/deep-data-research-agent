import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from deep_data_research_agent.api import app as webapp
from deep_data_research_agent.api import auth as auth_module
from deep_data_research_agent.core.identity import user_identity_from_config
from deep_data_research_agent.database import repository as database


def _request(authorization: str | None = None) -> Request:
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_bearer_token_accepts_empty_and_valid_header() -> None:
    assert auth_module.bearer_token(None) is None
    assert auth_module.bearer_token("Bearer token-value") == "token-value"


def test_bearer_token_rejects_malformed_header() -> None:
    with pytest.raises(Exception, match="登录凭据格式无效"):
        auth_module.bearer_token("Basic abc")


def test_runtime_identity_has_no_development_fallback() -> None:
    with pytest.raises(RuntimeError, match="经过认证的用户身份"):
        user_identity_from_config({})


@pytest.mark.asyncio
async def test_missing_token_requires_login_in_development() -> None:
    with pytest.raises(Exception) as caught:
        await auth_module.authenticate_request(_request())

    assert caught.value.status_code == 401
    assert caught.value.detail == "请先登录"


@pytest.mark.asyncio
async def test_valid_token_returns_registered_user(monkeypatch) -> None:
    async def resolve(_token: str) -> database.UserRecord:
        return database.UserRecord("user-a", "Alice", False)

    monkeypatch.setattr(database, "resolve_login_session", resolve)
    user = await auth_module.authenticate_request(_request("Bearer valid"))

    assert user["identity"] == "user-a"
    assert user["display_name"] == "Alice"
    assert user["is_authenticated"] is True


@pytest.mark.asyncio
async def test_clear_memory_uses_only_authenticated_user_hash(monkeypatch) -> None:
    captured: list[str] = []

    async def resolve(_token: str) -> database.UserRecord:
        return database.UserRecord("user-a", "Alice", False)

    class Queue:
        async def clear_user_memory(self, identity_hash: str) -> int:
            captured.append(identity_hash)
            return 2

    monkeypatch.setattr(database, "resolve_login_session", resolve)
    monkeypatch.setattr(webapp, "MEMORY_QUEUE", Queue())

    result = await webapp.clear_current_user_memory("Bearer valid")

    assert result == {"status": "cleared", "cancelled_jobs": 2}
    assert captured == [hashlib.sha256(b"user-a").hexdigest()]


@pytest.mark.asyncio
async def test_clear_memory_returns_service_unavailable_without_leaking_error(
    monkeypatch,
) -> None:
    async def resolve(_token: str) -> database.UserRecord:
        return database.UserRecord("user-a", "Alice", False)

    class Queue:
        async def clear_user_memory(self, _identity_hash: str) -> int:
            raise RuntimeError("mongodb://secret-host")

    monkeypatch.setattr(webapp, "MEMORY_QUEUE", Queue())
    monkeypatch.setattr(database, "resolve_login_session", resolve)

    with pytest.raises(Exception, match="记忆服务暂不可用") as exc_info:
        await webapp.clear_current_user_memory("Bearer valid")

    assert "secret-host" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_memory_settings_are_scoped_to_authenticated_user(monkeypatch) -> None:
    captured: list[tuple[str, bool | None]] = []

    async def resolve(_token: str) -> database.UserRecord:
        return database.UserRecord("user-a", "Alice", False)

    class Queue:
        async def get_memory_settings(self, identity_hash: str):
            captured.append((identity_hash, None))
            return SimpleNamespace(failure_lesson_saving_enabled=True)

        async def set_failure_lesson_saving(self, identity_hash: str, *, enabled: bool):
            captured.append((identity_hash, enabled))
            return SimpleNamespace(failure_lesson_saving_enabled=enabled), 3

    monkeypatch.setattr(database, "resolve_login_session", resolve)
    monkeypatch.setattr(webapp, "MEMORY_QUEUE", Queue())

    current = await webapp.get_current_memory_settings("Bearer valid")
    updated = await webapp.update_current_memory_settings(
        webapp.MemorySettingsRequest(failure_lesson_saving_enabled=False),
        "Bearer valid",
    )

    identity_hash = hashlib.sha256(b"user-a").hexdigest()
    assert current == {"failure_lesson_saving_enabled": True}
    assert updated == {
        "failure_lesson_saving_enabled": False,
        "cancelled_jobs": 3,
    }
    assert captured == [(identity_hash, None), (identity_hash, False)]


@pytest.mark.asyncio
async def test_email_delivery_status_is_scoped_to_authenticated_user(monkeypatch) -> None:
    async def resolve(_token: str) -> database.UserRecord:
        return database.UserRecord("user-a", "Alice", False)

    async def get_delivery(delivery_id: str, *, user_id: str):
        assert user_id == "user-a"
        return SimpleNamespace(
            idempotency_key=delivery_id,
            status="queued",
            recipient="reader@example.com",
            pdf_filename="final_report.pdf",
            zip_filename="final_report-bundle.zip",
            attempts=0,
            error_summary=None,
            updated_at=datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC),
            finished_at=None,
        )

    monkeypatch.setattr(database, "resolve_login_session", resolve)
    monkeypatch.setattr(database, "get_email_delivery", get_delivery)

    result = await webapp.email_delivery_status("a" * 64, "Bearer valid")

    assert result["status"] == "queued"
    assert result["delivery_id"] == "a" * 64
    assert result["attempts"] == 0


@pytest.mark.asyncio
async def test_thread_create_stamps_and_claims_owner(monkeypatch) -> None:
    claims: list[tuple[str, str]] = []

    async def claim(thread_id: str, user_id: str) -> None:
        claims.append((thread_id, user_id))

    monkeypatch.setattr(database, "claim_thread", claim)
    ctx = SimpleNamespace(user=SimpleNamespace(identity="user-a"))
    value = {"thread_id": "thread-a", "metadata": {}}

    await auth_module.create_thread(ctx, value)

    assert value["metadata"]["owner"] == "user-a"
    assert claims == [("thread-a", "user-a")]


@pytest.mark.asyncio
async def test_async_task_status_queries_child_run_without_model(monkeypatch) -> None:
    async def get_owner(_thread_id: str) -> str:
        return "user-a"

    async def resolve(_token: str) -> database.UserRecord:
        return database.UserRecord("user-a", "Alice", False)

    class Threads:
        async def get(self, *, thread_id: str, headers=None) -> dict[str, object]:
            assert thread_id == "parent-thread"
            assert headers == {"Authorization": "Bearer valid"}
            return {
                "values": {
                    "async_tasks": {
                        "child-thread": {
                            "task_id": "child-thread",
                            "thread_id": "child-thread",
                            "run_id": "run-1",
                            "agent_name": "crawl-worker",
                            "status": "running",
                        }
                    }
                }
            }

    class Runs:
        async def get(self, *, thread_id: str, run_id: str, headers=None) -> dict[str, str]:
            assert (thread_id, run_id) == ("child-thread", "run-1")
            assert headers == {"Authorization": "Bearer valid"}
            return {"status": "success"}

    monkeypatch.setattr(database, "get_thread_owner", get_owner)
    monkeypatch.setattr(database, "resolve_login_session", resolve)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(agent_client=SimpleNamespace(threads=Threads(), runs=Runs()))
        )
    )

    result = await webapp.async_task_status(
        webapp.AsyncTaskStatusRequest(thread_id="parent-thread"),
        request,
        "Bearer valid",
    )

    assert result["tasks"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_async_task_status_returns_sanitized_failure(monkeypatch) -> None:
    async def get_owner(_thread_id: str) -> str:
        return "user-a"

    async def resolve(_token: str) -> database.UserRecord:
        return database.UserRecord("user-a", "Alice", False)

    class Threads:
        async def get(self, *, thread_id: str, headers=None) -> dict[str, object]:
            assert thread_id == "parent-thread"
            assert headers == {"Authorization": "Bearer valid"}
            return {
                "values": {
                    "async_tasks": {
                        "child-thread": {
                            "task_id": "child-thread",
                            "thread_id": "child-thread",
                            "run_id": "run-1",
                            "agent_name": "crawl-worker",
                            "status": "running",
                        }
                    }
                }
            }

    class Runs:
        async def get(self, *, thread_id: str, run_id: str, headers=None) -> dict[str, str]:
            assert (thread_id, run_id) == ("child-thread", "run-1")
            assert headers == {"Authorization": "Bearer valid"}
            return {
                "status": "error",
                "error": (
                    "TypeError: failure in C:\\private\\workspace "
                    "with token secret-value"
                ),
            }

    monkeypatch.setattr(database, "get_thread_owner", get_owner)
    monkeypatch.setattr(database, "resolve_login_session", resolve)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(agent_client=SimpleNamespace(threads=Threads(), runs=Runs()))
        )
    )

    result = await webapp.async_task_status(
        webapp.AsyncTaskStatusRequest(thread_id="parent-thread"),
        request,
        "Bearer valid",
    )

    task = result["tasks"][0]
    assert task["status"] == "error"
    assert task["error_summary"] == "子任务发生内部类型错误，原样重试通常不会成功。"
    assert "private" not in task["error_summary"]
    assert "secret-value" not in task["error_summary"]


@pytest.mark.asyncio
async def test_async_task_status_hides_other_users_thread(monkeypatch) -> None:
    async def get_owner(_thread_id: str) -> str:
        return "another-user"

    async def resolve(_token: str) -> database.UserRecord:
        return database.UserRecord("user-a", "Alice", False)

    monkeypatch.setattr(database, "get_thread_owner", get_owner)
    monkeypatch.setattr(database, "resolve_login_session", resolve)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent_client=SimpleNamespace()))
    )

    with pytest.raises(Exception, match="会话不存在"):
        await webapp.async_task_status(
            webapp.AsyncTaskStatusRequest(thread_id="private-thread"),
            request,
            "Bearer valid",
        )


@pytest.mark.asyncio
async def test_cancel_async_task_directly_waits_and_persists_state(monkeypatch) -> None:
    async def get_owner(_thread_id: str) -> str:
        return "user-a"

    async def resolve(_token: str) -> database.UserRecord:
        return database.UserRecord("user-a", "Alice", False)

    updates: list[tuple[str, dict[str, object]]] = []
    cancellations: list[tuple[str, str, bool, str]] = []

    class Threads:
        async def get(self, *, thread_id: str, headers=None) -> dict[str, object]:
            assert thread_id == "parent-thread"
            assert headers == {"Authorization": "Bearer valid"}
            return {
                "metadata": {"graph_id": "supervisor"},
                "values": {
                    "async_tasks": {
                        "child-thread": {
                            "task_id": "child-thread",
                            "thread_id": "child-thread",
                            "run_id": "child-run",
                            "agent_name": "crawl-worker",
                            "status": "running",
                        }
                    }
                },
            }

        async def update_state(
            self,
            thread_id: str,
            values: dict[str, object],
            *,
            headers=None,
        ) -> None:
            assert headers == {"Authorization": "Bearer valid"}
            updates.append((thread_id, values))

    class Runs:
        async def list(
            self,
            thread_id: str,
            *,
            status: str,
            limit: int,
            headers=None,
        ) -> list[dict[str, str]]:
            assert (thread_id, limit) == ("child-thread", 100)
            assert headers == {"Authorization": "Bearer valid"}
            return [{"run_id": "child-run", "status": "running"}] if status == "running" else []

        async def cancel(
            self,
            thread_id: str,
            run_id: str,
            *,
            wait: bool,
            action: str,
            headers=None,
        ) -> None:
            assert headers == {"Authorization": "Bearer valid"}
            cancellations.append((thread_id, run_id, wait, action))

    monkeypatch.setattr(database, "get_thread_owner", get_owner)
    monkeypatch.setattr(database, "resolve_login_session", resolve)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(agent_client=SimpleNamespace(threads=Threads(), runs=Runs()))
        )
    )

    result = await webapp.cancel_async_task_directly(
        "child-thread",
        webapp.AsyncTaskCancelRequest(thread_id="parent-thread"),
        request,
        "Bearer valid",
    )

    assert cancellations == [("child-thread", "child-run", True, "interrupt")]
    assert result["task"]["status"] == "cancelled"
    assert updates[0][0] == "parent-thread"
    assert updates[0][1]["async_tasks"]["child-thread"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_async_task_directly_hides_other_users_thread(monkeypatch) -> None:
    async def get_owner(_thread_id: str) -> str:
        return "another-user"

    async def resolve(_token: str) -> database.UserRecord:
        return database.UserRecord("user-a", "Alice", False)

    monkeypatch.setattr(database, "get_thread_owner", get_owner)
    monkeypatch.setattr(database, "resolve_login_session", resolve)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent_client=SimpleNamespace()))
    )

    with pytest.raises(Exception, match="会话不存在"):
        await webapp.cancel_async_task_directly(
            "private-child",
            webapp.AsyncTaskCancelRequest(thread_id="private-parent"),
            request,
            "Bearer valid",
        )


@pytest.mark.asyncio
async def test_cancel_thread_execution_cascades_queue_and_children(monkeypatch) -> None:
    async def get_owner(_thread_id: str) -> str:
        return "user-a"

    async def resolve(_token: str) -> database.UserRecord:
        return database.UserRecord("user-a", "Alice", False)

    cancellations: list[tuple[str, str, bool, str]] = []
    updates: list[dict[str, object]] = []

    class Threads:
        async def get(self, *, thread_id: str, headers=None) -> dict[str, object]:
            assert thread_id == "parent-thread"
            assert headers == {"Authorization": "Bearer valid"}
            return {
                "metadata": {"graph_id": "supervisor"},
                "values": {
                    "async_tasks": {
                        "tracked-child": {
                            "task_id": "tracked-child",
                            "thread_id": "tracked-child",
                            "run_id": "tracked-run",
                            "agent_name": "crawl-worker",
                            "status": "running",
                        }
                    }
                },
            }

        async def search(self, **kwargs) -> list[dict[str, str]]:
            assert kwargs["metadata"] == {
                "parent_thread_id": "parent-thread",
                "kind": "async-subagent",
            }
            assert kwargs["status"] == "busy"
            assert kwargs["headers"] == {"Authorization": "Bearer valid"}
            return [{"thread_id": "uncheckpointed-child", "status": "busy"}]

        async def update_state(
            self,
            _thread_id: str,
            values: dict[str, object],
            *,
            headers=None,
        ) -> None:
            assert headers == {"Authorization": "Bearer valid"}
            updates.append(values)

    active = {
        ("parent-thread", "pending"): ["queued-run"],
        ("parent-thread", "running"): ["parent-run"],
        ("tracked-child", "running"): ["tracked-run"],
        ("uncheckpointed-child", "running"): ["uncheckpointed-run"],
    }

    class Runs:
        async def list(
            self,
            thread_id: str,
            *,
            status: str,
            limit: int,
            headers=None,
        ) -> list[dict[str, str]]:
            assert limit == 100
            assert headers == {"Authorization": "Bearer valid"}
            return [
                {"run_id": run_id, "status": status}
                for run_id in active.get((thread_id, status), [])
            ]

        async def cancel(
            self,
            thread_id: str,
            run_id: str,
            *,
            wait: bool,
            action: str,
            headers=None,
        ) -> None:
            assert headers == {"Authorization": "Bearer valid"}
            cancellations.append((thread_id, run_id, wait, action))
            for key, run_ids in active.items():
                if key[0] == thread_id and run_id in run_ids:
                    run_ids.remove(run_id)

    monkeypatch.setattr(database, "get_thread_owner", get_owner)
    monkeypatch.setattr(database, "resolve_login_session", resolve)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(agent_client=SimpleNamespace(threads=Threads(), runs=Runs()))
        )
    )

    result = await webapp.cancel_thread_execution(
        "parent-thread",
        request,
        "Bearer valid",
    )

    assert cancellations[:2] == [
        ("parent-thread", "queued-run", True, "interrupt"),
        ("parent-thread", "parent-run", True, "interrupt"),
    ]
    assert {item[:2] for item in cancellations[2:]} == {
        ("tracked-child", "tracked-run"),
        ("uncheckpointed-child", "uncheckpointed-run"),
    }
    assert result == {
        "status": "cancelled",
        "cancelled_parent_runs": 2,
        "cancelled_child_runs": 2,
    }
    assert updates[0]["async_tasks"]["tracked-child"]["status"] == "cancelled"
