"""Tavily tools used exclusively by the asynchronous crawl worker."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from langchain.tools import ToolRuntime, tool
from tavily import AsyncTavilyClient

from deep_data_research_agent.core.config import get_settings
from deep_data_research_agent.infrastructure.sandbox import manager as sandbox_manager


def _public_url(url: str) -> str:
    """Validate and normalize a public HTTP(S) URL."""

    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"仅支持公开的 HTTP(S) URL：{url}")
    if parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("不允许采集本机地址")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


async def _persist_result(
    *,
    response: dict[str, Any],
    mode: str,
    subject: str,
    runtime: ToolRuntime,
) -> dict[str, Any]:
    """Persist Tavily content in OpenSandbox and return a compact summary."""

    pages: list[dict[str, Any]] = []
    files: list[tuple[str, bytes]] = []
    for result in response.get("results", []):
        url = str(result.get("url", ""))
        content = result.get("raw_content") or result.get("content") or ""
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        content_path = f"/workspace/raw/{digest}.md"
        files.append((content_path, str(content).encode("utf-8")))

        pages.append(
            {
                "title": result.get("title") or url,
                "url": url,
                "score": result.get("score"),
                "content_path": content_path,
            }
        )

    pages_path = f"/workspace/{mode}_pages.jsonl"
    pages_content = "".join(
        json.dumps(page, ensure_ascii=False) + "\n" for page in pages
    )
    files.append((pages_path, pages_content.encode("utf-8")))

    failed = response.get("failed_results", [])
    manifest = {
        "mode": mode,
        "subject": subject,
        "created_at": datetime.now(UTC).isoformat(),
        "page_count": len(pages),
        "failed_count": len(failed),
        "failed_results": failed,
        "request_id": response.get("request_id"),
        "usage": response.get("usage", {}),
        "pages_file": pages_path,
        "pages": pages,
    }
    files.append(
        (
            f"/workspace/{mode}_result.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    )

    thread_id = sandbox_manager.thread_id_from_runtime(runtime)
    await sandbox_manager.SANDBOX_MANAGER.upload_workspace_files(thread_id, files)
    return manifest


def _require_tavily_key() -> tuple[str, str | None]:
    settings = get_settings()
    if not settings.tavily_api_key:
        raise ValueError("缺少 TAVILY_API_KEY，请先在 .env 中配置")
    return settings.tavily_api_key, settings.tavily_project


@tool("tavily_search")
async def tavily_search(
    query: str,
    runtime: ToolRuntime,
    topic: Literal["general", "news", "finance"] = "general",
    max_results: int = 8,
) -> str:
    """按主题搜索公开网页，并把精简结果保存到当前任务目录。"""

    api_key, project_id = _require_tavily_key()
    async with AsyncTavilyClient(api_key=api_key, project_id=project_id) as client:
        response = await client.search(
            query=query,
            topic=topic,
            search_depth="basic",
            max_results=max(1, min(max_results, 8)),
            include_raw_content=False,
            include_answer=False,
            include_usage=True,
        )
    summary = await _persist_result(
        response=response,
        mode="search",
        subject=query,
        runtime=runtime,
    )
    return json.dumps(summary, ensure_ascii=False)


@tool("tavily_crawl")
async def tavily_crawl(
    url: str,
    instructions: str,
    runtime: ToolRuntime,
    limit: int = 20,
    max_depth: int = 2,
) -> str:
    """根据自然语言采集要求爬取一个公开网站。"""

    normalized_url = _public_url(url)
    api_key, project_id = _require_tavily_key()
    async with AsyncTavilyClient(api_key=api_key, project_id=project_id) as client:
        response = await client.crawl(
            url=normalized_url,
            instructions=instructions,
            max_depth=max(1, min(max_depth, 2)),
            max_breadth=10,
            limit=max(1, min(limit, 30)),
            allow_external=False,
            extract_depth="basic",
            chunks_per_source=3,
            include_images=False,
            format="markdown",
            include_usage=True,
        )
    summary = await _persist_result(
        response=response,
        mode="crawl",
        subject=normalized_url,
        runtime=runtime,
    )
    return json.dumps(summary, ensure_ascii=False)


@tool("tavily_extract")
async def tavily_extract(
    urls: list[str],
    query: str,
    runtime: ToolRuntime,
) -> str:
    """从最多十个公开 URL 提取与查询相关的 Markdown 内容片段。"""

    normalized_urls = [_public_url(url) for url in urls[:10]]
    if not normalized_urls:
        raise ValueError("urls 至少需要一个 URL")

    api_key, project_id = _require_tavily_key()
    async with AsyncTavilyClient(api_key=api_key, project_id=project_id) as client:
        response = await client.extract(
            urls=normalized_urls,
            query=query,
            extract_depth="basic",
            chunks_per_source=3,
            include_images=False,
            format="markdown",
            include_usage=True,
        )
    summary = await _persist_result(
        response=response,
        mode="extract",
        subject=query,
        runtime=runtime,
    )
    return json.dumps(summary, ensure_ascii=False)


CRAWL_TOOLS = [tavily_search, tavily_crawl, tavily_extract]
