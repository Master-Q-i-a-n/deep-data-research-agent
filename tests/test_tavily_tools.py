import json
from types import SimpleNamespace

import pytest

from deep_data_research_agent.tools import tavily as tavily_tools
from deep_data_research_agent.tools.tavily import _persist_result, _public_url


def test_public_url_normalizes_fragment_and_rejects_localhost() -> None:
    assert _public_url("https://example.com/docs#intro") == "https://example.com/docs"

    with pytest.raises(ValueError, match="本机地址"):
        _public_url("http://localhost:8000/private")


@pytest.mark.asyncio
async def test_persist_result_writes_manifest_pages_and_content(
    monkeypatch,
) -> None:
    uploaded: list[tuple[str, bytes]] = []

    class FakeManager:
        async def upload_workspace_files(self, thread_id, files) -> None:
            assert thread_id == "thread-1"
            uploaded.extend(files)

    monkeypatch.setattr(
        tavily_tools.sandbox_manager,
        "SANDBOX_MANAGER",
        FakeManager(),
    )

    response = {
        "results": [
            {
                "title": "Example",
                "url": "https://example.com/data",
                "raw_content": "# Example\n\n42 rows",
                "score": 0.9,
            }
        ],
        "failed_results": [],
        "request_id": "request-1",
        "usage": {"credits": 1},
    }

    manifest = await _persist_result(
        response=response,
        mode="crawl",
        subject="example",
        runtime=SimpleNamespace(
            config={"configurable": {"thread_id": "thread-1"}},
        ),
    )

    assert manifest["page_count"] == 1
    assert manifest["pages"][0]["content_path"].startswith("/workspace/raw/")
    saved_files = {path: content for path, content in uploaded}
    assert "/workspace/crawl_pages.jsonl" in saved_files
    assert "/workspace/crawl_result.json" in saved_files

    saved = json.loads(saved_files["/workspace/crawl_result.json"])
    content_name = saved["pages"][0]["content_path"].split("/")[-1]
    assert saved_files[f"/workspace/raw/{content_name}"].decode("utf-8") == (
        "# Example\n\n42 rows"
    )
