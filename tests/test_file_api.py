import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile
from openpyxl import Workbook

from deep_data_research_agent.api import app as webapp
from deep_data_research_agent.database import repository as database
from deep_data_research_agent.infrastructure.sandbox import manager as sandbox_manager
from deep_data_research_agent.infrastructure.workspace import (
    LocalWorkspaceStore,
    WorkspaceScope,
    WorkspaceStorageError,
)

TEST_USER_ID = "user-a"


class _ThreadClient:
    async def get(self, *, thread_id: str):
        return {"thread_id": thread_id, "metadata": {"graph_id": "supervisor"}}


def _request():
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                agent_client=SimpleNamespace(threads=_ThreadClient()),
            )
        )
    )


def _install_workspace_store(monkeypatch, tmp_path: Path) -> Path:
    store = LocalWorkspaceStore(tmp_path)
    scope = WorkspaceScope(TEST_USER_ID, "thread-a", "supervisor")
    sandbox_manager.SANDBOX_MANAGER._thread_users.pop("thread-a", None)
    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "workspace_store", store)
    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "_coordination_enabled", False)
    return store.workspace_path(scope)


@pytest.fixture
def owned_thread(monkeypatch):
    async def owner(_thread_id: str) -> str:
        return TEST_USER_ID

    async def authenticated_user(_authorization: str | None) -> str:
        return TEST_USER_ID

    monkeypatch.setattr(database, "get_thread_owner", owner)
    monkeypatch.setattr(webapp, "_authenticated_user_id", authenticated_user)


def _xlsx_bytes() -> bytes:
    output = io.BytesIO()
    workbook = Workbook()
    workbook.active.append(["id", "amount"])
    workbook.active.append(["001", 10])
    workbook.save(output)
    return output.getvalue()


def test_upload_name_rejects_paths_reserved_names_and_unknown_types() -> None:
    for filename in (
        "../orders.csv",
        "CON.csv",
        "orders:backup.csv",
        "orders.json",
        "orders.csv.",
    ):
        with pytest.raises(HTTPException):
            webapp._validated_upload_name(filename)

    assert webapp._validated_upload_name("订单.XLSX") == "订单.XLSX"


def test_xlsx_validation_rejects_renamed_zip() -> None:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("readme.txt", "not a workbook")

    with pytest.raises(HTTPException) as caught:
        webapp._validate_xlsx(content.getvalue())

    assert caught.value.status_code == 400


def test_xlsx_validation_rejects_malformed_ooxml() -> None:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types>")
        archive.writestr("xl/workbook.xml", "<workbook>")

    with pytest.raises(HTTPException) as caught:
        webapp._validate_xlsx(content.getvalue())

    assert caught.value.status_code == 400


def test_xlsx_validation_enforces_archive_limits(monkeypatch) -> None:
    content = _xlsx_bytes()
    monkeypatch.setattr(webapp, "_MAX_XLSX_ENTRIES", 1)
    with pytest.raises(HTTPException) as entry_error:
        webapp._validate_xlsx(content)
    assert entry_error.value.status_code == 400

    monkeypatch.setattr(webapp, "_MAX_XLSX_ENTRIES", 10_000)
    monkeypatch.setattr(webapp, "_MAX_XLSX_UNCOMPRESSED_BYTES", 1)
    with pytest.raises(HTTPException) as size_error:
        webapp._validate_xlsx(content)
    assert size_error.value.status_code == 400


@pytest.mark.asyncio
async def test_upload_files_returns_sandbox_paths(
    monkeypatch,
    tmp_path: Path,
    owned_thread,
) -> None:
    uploaded: list[tuple[str, bytes]] = []

    _install_workspace_store(monkeypatch, tmp_path)

    async def upload(_thread_id: str, _user_id: str, prepared):
        uploaded.extend(prepared)

    monkeypatch.setattr(webapp, "_upload_to_supervisor_workspace", upload)
    files = [
        UploadFile(file=io.BytesIO(b"id,amount\n001,10\n"), filename="orders.csv"),
        UploadFile(file=io.BytesIO(_xlsx_bytes()), filename="summary.xlsx"),
    ]

    result = await webapp.upload_files("thread-a", _request(), files, None)

    assert [item["path"] for item in result["files"]] == [
        "/workspace/input/orders.csv",
        "/workspace/input/summary.xlsx",
    ]
    assert [name for name, _content in uploaded] == ["orders.csv", "summary.xlsx"]


@pytest.mark.asyncio
async def test_upload_rejects_duplicate_existing_filename(
    monkeypatch,
    tmp_path: Path,
    owned_thread,
) -> None:
    workspace = _install_workspace_store(monkeypatch, tmp_path)
    (workspace / "input").mkdir(parents=True)
    (workspace / "input" / "Orders.csv").write_text("id\n1", encoding="utf-8")
    duplicate = UploadFile(file=io.BytesIO(b"id\n2"), filename="orders.CSV")

    with pytest.raises(HTTPException) as caught:
        await webapp.upload_files("thread-a", _request(), [duplicate], None)

    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_file_list_hides_foreign_thread(monkeypatch) -> None:
    async def owner(_thread_id: str) -> str:
        return "other-user"

    async def authenticated_user(_authorization: str | None) -> str:
        return TEST_USER_ID

    monkeypatch.setattr(database, "get_thread_owner", owner)
    monkeypatch.setattr(webapp, "_authenticated_user_id", authenticated_user)

    with pytest.raises(HTTPException) as caught:
        await webapp.list_uploaded_files("thread-a", _request(), None)

    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_file_api_rejects_crawl_worker_thread(monkeypatch, owned_thread) -> None:
    class WorkerThreads:
        async def get(self, *, thread_id: str):
            return {"thread_id": thread_id, "metadata": {"graph_id": "crawl-worker"}}

    with pytest.raises(HTTPException) as caught:
        await webapp._require_owned_supervisor_thread(
            "thread-a",
            TEST_USER_ID,
            SimpleNamespace(threads=WorkerThreads()),
        )

    assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_upload_enforces_file_count_and_size(
    monkeypatch,
    tmp_path: Path,
    owned_thread,
) -> None:
    _install_workspace_store(monkeypatch, tmp_path)
    too_many = [
        UploadFile(file=io.BytesIO(b"id\n1"), filename=f"part-{index}.csv")
        for index in range(6)
    ]
    with pytest.raises(HTTPException) as count_error:
        await webapp.upload_files("thread-a", _request(), too_many, None)
    assert count_error.value.status_code == 400

    monkeypatch.setattr(webapp, "_MAX_UPLOAD_FILE_BYTES", 2)
    oversized = UploadFile(file=io.BytesIO(b"123"), filename="large.csv")
    with pytest.raises(HTTPException) as size_error:
        await webapp.upload_files("thread-a", _request(), [oversized], None)
    assert size_error.value.status_code == 413


@pytest.mark.asyncio
async def test_upload_enforces_per_thread_total_size(
    monkeypatch,
    tmp_path: Path,
    owned_thread,
) -> None:
    workspace = _install_workspace_store(monkeypatch, tmp_path)
    (workspace / "input").mkdir(parents=True)
    (workspace / "input" / "existing.csv").write_bytes(b"123")
    monkeypatch.setattr(webapp, "_MAX_UPLOAD_TOTAL_BYTES", 4)
    extra = UploadFile(file=io.BytesIO(b"12"), filename="extra.csv")

    with pytest.raises(HTTPException) as caught:
        await webapp.upload_files("thread-a", _request(), [extra], None)

    assert caught.value.status_code == 413


@pytest.mark.asyncio
async def test_delete_file_does_not_allocate_supervisor_sandbox(
    monkeypatch,
    tmp_path: Path,
    owned_thread,
) -> None:
    workspace = _install_workspace_store(monkeypatch, tmp_path)
    (workspace / "input").mkdir(parents=True)
    (workspace / "input" / "orders.csv").write_text("id\n1", encoding="utf-8")
    ensured: list[tuple[str, str, str]] = []
    deleted: list[tuple[str, str, str]] = []

    async def ensure(
        thread_id: str, *, component: str, network_enabled: bool, user_id: str
    ):
        assert network_enabled is True
        ensured.append((thread_id, component, user_id))

    async def delete(thread_id: str, path: str, *, component: str):
        deleted.append((thread_id, path, component))

    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "ensure", ensure)
    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER, "delete_workspace_file", delete
    )

    result = await webapp.delete_uploaded_file(
        "thread-a",
        _request(),
        "/workspace/input/orders.csv",
        None,
    )

    assert result == {"status": "deleted", "path": "/workspace/input/orders.csv"}
    assert ensured == []
    assert deleted == []
    assert not (workspace / "input" / "orders.csv").exists()


@pytest.mark.asyncio
async def test_upload_persists_without_allocating_sandbox(monkeypatch, tmp_path, owned_thread):
    workspace = _install_workspace_store(monkeypatch, tmp_path)
    ensure = AsyncMock(side_effect=AssertionError("upload allocated sandbox"))
    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "ensure", ensure)
    result = await webapp.upload_files(
        "thread-a", _request(), [UploadFile(filename="orders.csv", file=io.BytesIO(b"id\n1\n"))], None
    )
    assert result["files"][0]["path"] == "/workspace/input/orders.csv"
    assert (workspace / "input" / "orders.csv").read_bytes() == b"id\n1\n"
    ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_persist_failure_rolls_back_only_new_batch(monkeypatch, tmp_path, owned_thread):
    workspace = _install_workspace_store(monkeypatch, tmp_path)
    (workspace / "input").mkdir(parents=True)
    (workspace / "input" / "existing.csv").write_bytes(b"old")
    store = sandbox_manager.SANDBOX_MANAGER.workspace_store
    original = store.write_many

    async def partial(scope, files):
        await original(scope, files[:1])
        raise WorkspaceStorageError("partial")

    monkeypatch.setattr(store, "write_many", partial)
    with pytest.raises(HTTPException) as exc:
        await webapp.upload_files(
            "thread-a", _request(), [
                UploadFile(filename="one.csv", file=io.BytesIO(b"1")),
                UploadFile(filename="two.csv", file=io.BytesIO(b"2")),
            ], None,
        )
    assert exc.value.status_code == 503
    assert sorted(p.name for p in (workspace / "input").iterdir()) == ["existing.csv"]
