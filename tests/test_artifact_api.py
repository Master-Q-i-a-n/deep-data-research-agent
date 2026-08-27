import io
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from deep_data_research_agent.api import app as webapp
from deep_data_research_agent.database import repository as database
from deep_data_research_agent.infrastructure.sandbox import manager as sandbox_manager
from deep_data_research_agent.infrastructure.workspace import (
    LocalWorkspaceStore,
    WorkspaceScope,
)


@pytest.mark.asyncio
async def test_artifact_list_and_download_use_owned_workspace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = LocalWorkspaceStore(tmp_path)
    scope = WorkspaceScope(database.DEFAULT_USER_ID, "artifact-thread", "supervisor")
    workspace = store.workspace_path(scope)
    output = workspace / "output"
    charts = output / "charts"
    metrics = output / "metrics"
    raw = workspace / "raw"
    input_root = workspace / "input"
    charts.mkdir(parents=True)
    metrics.mkdir()
    raw.mkdir()
    input_root.mkdir()
    (output / "final_report.md").write_text(
        "# 报告\n\n![价格对比](charts/price.png)",
        encoding="utf-8",
    )
    (output / "final_report.pdf").write_bytes(b"pdf")
    (workspace / "scratch.csv").write_text("temporary", encoding="utf-8")
    (charts / "price.png").write_bytes(b"png")
    (charts / "unreferenced.png").write_bytes(b"other-png")
    (metrics / "orders.csv").write_bytes(b"id\n1")
    (metrics / "summary.json").write_text('{"rows": 1}', encoding="utf-8")
    (metrics / "workbook.xlsx").write_bytes(b"xlsx")
    (raw / "source.md").write_text("raw", encoding="utf-8")
    (input_root / "orders.csv").write_text("id\n1", encoding="utf-8")

    async def owner(_thread_id: str) -> str:
        return database.DEFAULT_USER_ID

    monkeypatch.setattr(database, "get_thread_owner", owner)
    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "workspace_store", store)

    listing = await webapp.list_artifacts("artifact-thread", None)
    assert [item["path"] for item in listing["artifacts"]] == [
        "/workspace/output/final_report.pdf",
        "/workspace/output/final_report.md",
    ]

    response = await webapp.download_artifact(
        "artifact-thread",
        "/workspace/output/final_report.md",
        None,
    )
    downloaded = b"".join([chunk async for chunk in response.body_iterator])
    assert downloaded.decode("utf-8").startswith("# 报告")
    assert response.headers["content-length"] == str(
        (output / "final_report.md").stat().st_size
    )
    assert 'filename="final_report.md"' in response.headers["content-disposition"]

    content, filename = await webapp._markdown_bundle(
        scope,
        "/workspace/output/final_report.md",
    )
    assert filename == "final_report-bundle.zip"
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.namelist() == [
            "final_report.md",
            "charts/price.png",
            "charts/unreferenced.png",
            "metrics/orders.csv",
            "metrics/summary.json",
            "metrics/workbook.xlsx",
        ]
        assert archive.read("charts/price.png") == b"png"
        assert archive.read("metrics/orders.csv") == b"id\n1"


@pytest.mark.asyncio
async def test_artifact_api_hides_other_users_thread(monkeypatch) -> None:
    async def owner(_thread_id: str) -> str:
        return "another-user"

    monkeypatch.setattr(database, "get_thread_owner", owner)

    with pytest.raises(HTTPException) as caught:
        await webapp.list_artifacts("private-thread", None)

    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_download_path_rejects_traversal(monkeypatch, tmp_path: Path) -> None:
    store = LocalWorkspaceStore(tmp_path)
    scope = WorkspaceScope(database.DEFAULT_USER_ID, "traversal-thread", "supervisor")
    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "workspace_store", store)
    with pytest.raises(HTTPException) as caught:
        await webapp._download_object(scope, "/workspace/../secret.md")

    assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_markdown_bundle_rejects_missing_or_unsafe_images(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = LocalWorkspaceStore(tmp_path)
    scope = WorkspaceScope(database.DEFAULT_USER_ID, "bundle-thread", "supervisor")
    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "workspace_store", store)
    report = store.workspace_path(scope) / "output" / "final_report.md"
    report.parent.mkdir(parents=True)
    report.write_text("![缺失](charts/missing.png)", encoding="utf-8")

    with pytest.raises(HTTPException) as missing:
        await webapp._markdown_bundle(scope, "/workspace/output/final_report.md")
    assert missing.value.status_code == 409
    assert "图片不存在" in str(missing.value.detail)

    report.write_text("![越界](../secret.png)", encoding="utf-8")
    with pytest.raises(HTTPException) as unsafe:
        await webapp._markdown_bundle(scope, "/workspace/output/final_report.md")
    assert unsafe.value.status_code == 409
    assert "路径不安全" in str(unsafe.value.detail)
