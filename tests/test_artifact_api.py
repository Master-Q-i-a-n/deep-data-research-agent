import io
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from deep_data_research_agent import database, sandbox_manager, webapp


@pytest.mark.asyncio
async def test_artifact_list_and_download_use_owned_workspace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
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
    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER,
        "local_workspace_path",
        lambda *_args, **_kwargs: workspace,
    )

    listing = await webapp.list_artifacts("thread-a", None)
    assert [item["path"] for item in listing["artifacts"]] == [
        "/workspace/output/final_report.pdf",
        "/workspace/output/final_report.md",
    ]

    response = await webapp.download_artifact(
        "thread-a",
        "/workspace/output/final_report.md",
        None,
    )
    assert Path(response.path) == output / "final_report.md"
    assert response.filename == "final_report.md"

    content, filename = webapp._markdown_bundle(
        workspace,
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


def test_download_path_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(HTTPException) as caught:
        webapp._download_path(tmp_path, "/workspace/../secret.md")

    assert caught.value.status_code == 400


def test_markdown_bundle_rejects_missing_or_unsafe_images(tmp_path: Path) -> None:
    report = tmp_path / "output" / "final_report.md"
    report.parent.mkdir()
    report.write_text("![缺失](charts/missing.png)", encoding="utf-8")

    with pytest.raises(HTTPException) as missing:
        webapp._markdown_bundle(tmp_path, "/workspace/output/final_report.md")
    assert missing.value.status_code == 409
    assert "图片不存在" in str(missing.value.detail)

    report.write_text("![越界](../secret.png)", encoding="utf-8")
    with pytest.raises(HTTPException) as unsafe:
        webapp._markdown_bundle(tmp_path, "/workspace/output/final_report.md")
    assert unsafe.value.status_code == 409
    assert "路径不安全" in str(unsafe.value.detail)
