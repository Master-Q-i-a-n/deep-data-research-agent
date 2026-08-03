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
    charts = workspace / "charts"
    raw = workspace / "raw"
    input_root = workspace / "input"
    charts.mkdir(parents=True)
    raw.mkdir()
    input_root.mkdir()
    (workspace / "final_report.md").write_text("# 报告", encoding="utf-8")
    (workspace / "scratch.csv").write_text("temporary", encoding="utf-8")
    (charts / "price.png").write_bytes(b"png")
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
        "/workspace/final_report.md",
        "/workspace/charts/price.png",
    ]

    response = await webapp.download_artifact(
        "thread-a",
        "/workspace/final_report.md",
        None,
    )
    assert Path(response.path) == workspace / "final_report.md"
    assert response.filename == "final_report.md"


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
