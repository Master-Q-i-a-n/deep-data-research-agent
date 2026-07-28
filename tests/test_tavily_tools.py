import asyncio
import json

import pytest

from deep_data_research_agent import tavily_tools
from deep_data_research_agent.tavily_tools import _persist_result, _public_url


def test_public_url_normalizes_fragment_and_rejects_localhost() -> None:
    assert _public_url("https://example.com/docs#intro") == "https://example.com/docs"

    with pytest.raises(ValueError, match="本机地址"):
        _public_url("http://localhost:8000/private")


@pytest.mark.asyncio
async def test_persist_result_writes_manifest_pages_and_content(
    tmp_path,
    monkeypatch,
) -> None:
    real_to_thread = asyncio.to_thread
    offloaded_functions: list[str] = []

    async def record_to_thread(function, /, *args, **kwargs):
        offloaded_functions.append(function.__name__)
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(tavily_tools.asyncio, "to_thread", record_to_thread)

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
        root=tmp_path,
    )

    assert manifest["page_count"] == 1
    assert "mkdir" in offloaded_functions
    assert manifest["pages"][0]["content_path"].startswith("/workspace/raw/")
    assert (tmp_path / "crawl_pages.jsonl").is_file()
    assert (tmp_path / "crawl_result.json").is_file()

    saved = json.loads((tmp_path / "crawl_result.json").read_text(encoding="utf-8"))
    content_name = saved["pages"][0]["content_path"].split("/")[-1]
    assert (tmp_path / "raw" / content_name).read_text(encoding="utf-8") == (
        "# Example\n\n42 rows"
    )
