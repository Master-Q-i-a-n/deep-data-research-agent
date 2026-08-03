import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from openpyxl import Workbook

from deep_data_research_agent import database, sandbox_manager, webapp


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


@pytest.fixture
def owned_thread(monkeypatch):
    async def owner(_thread_id: str) -> str:
        return database.DEFAULT_USER_ID

    monkeypatch.setattr(database, "get_thread_owner", owner)


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

    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER,
        "local_workspace_path",
        lambda *_args, **_kwargs: tmp_path / "workspace",
    )

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
    workspace = tmp_path / "workspace"
    (workspace / "input").mkdir(parents=True)
    (workspace / "input" / "Orders.csv").write_text("id\n1", encoding="utf-8")
    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER,
        "local_workspace_path",
        lambda *_args, **_kwargs: workspace,
    )
    duplicate = UploadFile(file=io.BytesIO(b"id\n2"), filename="orders.CSV")

    with pytest.raises(HTTPException) as caught:
        await webapp.upload_files("thread-a", _request(), [duplicate], None)

    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_file_list_hides_foreign_thread(monkeypatch) -> None:
    async def owner(_thread_id: str) -> str:
        return "other-user"

    monkeypatch.setattr(database, "get_thread_owner", owner)

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
            database.DEFAULT_USER_ID,
            SimpleNamespace(threads=WorkerThreads()),
        )

    assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_upload_enforces_file_count_and_size(
    monkeypatch,
    tmp_path: Path,
    owned_thread,
) -> None:
    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER,
        "local_workspace_path",
        lambda *_args, **_kwargs: tmp_path / "workspace",
    )
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
    workspace = tmp_path / "workspace"
    (workspace / "input").mkdir(parents=True)
    (workspace / "input" / "existing.csv").write_bytes(b"123")
    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER,
        "local_workspace_path",
        lambda *_args, **_kwargs: workspace,
    )
    monkeypatch.setattr(webapp, "_MAX_UPLOAD_TOTAL_BYTES", 4)
    extra = UploadFile(file=io.BytesIO(b"12"), filename="extra.csv")

    with pytest.raises(HTTPException) as caught:
        await webapp.upload_files("thread-a", _request(), [extra], None)

    assert caught.value.status_code == 413


@pytest.mark.asyncio
async def test_delete_file_calls_owned_supervisor_sandbox(
    monkeypatch,
    tmp_path: Path,
    owned_thread,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "input").mkdir(parents=True)
    (workspace / "input" / "orders.csv").write_text("id\n1", encoding="utf-8")
    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER,
        "local_workspace_path",
        lambda *_args, **_kwargs: workspace,
    )
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
    assert ensured == [("thread-a", "supervisor", database.DEFAULT_USER_ID)]
    assert deleted == [("thread-a", "/workspace/input/orders.csv", "supervisor")]
