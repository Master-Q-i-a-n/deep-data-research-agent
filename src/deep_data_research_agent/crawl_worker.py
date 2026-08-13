"""ASGI co-deployed asynchronous crawl-worker graph."""

from __future__ import annotations

import json
import mimetypes
import re
from typing import Any, Literal, NotRequired
from urllib.parse import urlsplit

from deepagents import DeepAgentState, create_deep_agent
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, field_validator

from deep_data_research_agent import sandbox_manager
from deep_data_research_agent.backends import (
    WORKER_FILESYSTEM_PERMISSIONS,
    create_worker_backend,
)
from deep_data_research_agent.config import create_chat_model
from deep_data_research_agent.identity import user_identity_from_config
from deep_data_research_agent.memory import (
    CRAWL_WORKER_FAILURE_TOOL,
    USER_MEMORY_PATH,
    MemoryRefreshMiddleware,
    agent_memory_path,
)
from deep_data_research_agent.model_profile import register_mvp_profile
from deep_data_research_agent.prompts import CRAWL_WORKER_PROMPT
from deep_data_research_agent.skill_middleware import (
    MongoSkillsRestoreMiddleware,
    ReloadableSkillsMiddleware,
)
from deep_data_research_agent.skill_storage import public_skill_root, user_skill_root
from deep_data_research_agent.tavily_tools import CRAWL_TOOLS

register_mvp_profile()

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>\]]+")
_STATUS_RE = re.compile(
    r"^(?:status\s*[:：]\s*)?(success|failed|needs_input)$",
    re.IGNORECASE,
)


class CrawlArtifact(BaseModel):
    """A real file produced by the crawl-worker sandbox."""

    path: str
    type: Literal["report", "source", "data", "chart", "script", "file"]
    mime_type: str
    size: int = Field(ge=0)
    description: str

    @field_validator("path")
    @classmethod
    def validate_workspace_path(cls, value: str) -> str:
        if not value.startswith("/workspace/"):
            raise ValueError("artifact 必须位于 /workspace")
        return value


class CrawlSource(BaseModel):
    """A public HTTP(S) source cited by the crawl-worker."""

    title: str
    url: str

    @field_validator("url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("来源必须是公开 HTTP(S) URL")
        return value


class CrawlTaskResult(BaseModel):
    """Stable result contract returned through ``check_async_task``."""

    status: Literal["success", "failed", "needs_input"]
    summary: str
    artifacts: list[CrawlArtifact]
    sources: list[CrawlSource]
    warnings: list[str]


class CrawlWorkerState(DeepAgentState):
    """Outer graph state used to carry deterministic export metadata."""

    exported_artifacts: NotRequired[list[dict[str, Any]]]

crawl_agent = create_deep_agent(
    name="crawl-worker-agent",
    model=create_chat_model(worker=True),
    tools=[*CRAWL_TOOLS, CRAWL_WORKER_FAILURE_TOOL],
    system_prompt=CRAWL_WORKER_PROMPT,
    middleware=[
        MemoryRefreshMiddleware(
            backend_factory=create_worker_backend,
            sources=[USER_MEMORY_PATH, agent_memory_path("crawl-worker")],
        ),
        MongoSkillsRestoreMiddleware(
            component="crawl-worker",
            agent_name="crawl-worker",
        ),
        ReloadableSkillsMiddleware(
            backend=create_worker_backend,
            sources=[
                (f"{public_skill_root('crawl-worker')}/", "公共"),
                (f"{user_skill_root('crawl-worker')}/", "用户"),
            ],
        ),
    ],
    backend=create_worker_backend,
    permissions=WORKER_FILESYSTEM_PERMISSIONS,
)


async def _ensure_sandbox(
    _state: DeepAgentState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Initialize the task sandbox before DeepAgents middleware resolves it."""

    thread_id = sandbox_manager.thread_id_from_config(config)
    await sandbox_manager.SANDBOX_MANAGER.ensure(
        thread_id,
        user_id=user_identity_from_config(config),
    )
    return {}


async def _export_workspace(
    _state: CrawlWorkerState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Persist the successful sandbox workspace for frontend and later runs."""

    thread_id = sandbox_manager.thread_id_from_config(config)
    artifacts = await sandbox_manager.SANDBOX_MANAGER.export_workspace(thread_id)
    return {"exported_artifacts": artifacts}


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(parts).strip()
    return str(content).strip()


def _artifact_kind(path: str) -> tuple[str, str]:
    """Classify an exported artifact without asking the model to guess."""

    lowered = path.lower()
    if lowered.endswith("crawl_report.md"):
        return "report", "完整网页采集与初步分析报告"
    if "/raw/" in lowered:
        return "source", "Tavily 保存的网页正文或片段"
    if lowered.endswith((".png", ".svg", ".jpg", ".jpeg", ".webp")):
        return "chart", "分析生成的图表或图片"
    if lowered.endswith((".json", ".jsonl", ".csv", ".tsv", ".parquet")):
        return "data", "采集或分析生成的结构化数据"
    if lowered.endswith(".py"):
        return "script", "数据处理或分析脚本"
    return "file", "crawl-worker 生成的任务文件"


def _extract_sources(text: str) -> list[CrawlSource]:
    """Extract cited URLs from the final answer while preserving link labels."""

    candidates: list[tuple[str, str]] = []
    covered_urls: set[str] = set()
    for title, url in _MARKDOWN_LINK_RE.findall(text):
        normalized = url.rstrip(".,;:!?，。；：！？")
        candidates.append((title.strip(), normalized))
        covered_urls.add(normalized)

    for raw_url in _BARE_URL_RE.findall(text):
        normalized = raw_url.rstrip(".,;:!?，。；：！？)")
        if normalized in covered_urls:
            continue
        hostname = urlsplit(normalized).hostname or "来源"
        candidates.append((hostname, normalized))
        covered_urls.add(normalized)

    sources: list[CrawlSource] = []
    for title, url in candidates[:100]:
        try:
            sources.append(CrawlSource(title=title or "来源", url=url))
        except ValueError:
            continue
    return sources


def _business_status(summary: str) -> Literal["success", "failed", "needs_input"]:
    if not summary:
        return "failed"
    first_line = summary.splitlines()[0].strip().strip("`*_# ")
    match = _STATUS_RE.fullmatch(first_line)
    if match:
        return match.group(1).lower()  # type: ignore[return-value]
    return "success"


async def _build_structured_result(
    state: CrawlWorkerState,
) -> dict[str, Any]:
    """Append a validated JSON result as the child thread's final message."""

    summary = ""
    for message in reversed(state.get("messages", [])):
        if isinstance(message, AIMessage):
            summary = _message_text(message)
            if summary:
                break

    artifacts: list[CrawlArtifact] = []
    for item in state.get("exported_artifacts", []):
        path = str(item.get("path", ""))
        kind, description = _artifact_kind(path)
        mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        artifacts.append(
            CrawlArtifact(
                path=path,
                type=kind,
                mime_type=mime_type,
                size=int(item.get("size", 0)),
                description=description,
            )
        )

    warnings: list[str] = []
    status = _business_status(summary)
    if not artifacts:
        warnings.append("crawl-worker 未导出任何工作区文件。")
        status = "failed"
    if not any(artifact.path == "/workspace/crawl_report.md" for artifact in artifacts):
        warnings.append("缺少预期文件 /workspace/crawl_report.md。")
        status = "failed"
    if not summary:
        warnings.append("crawl-worker 未返回可供 Supervisor 使用的摘要。")
        summary = "crawl-worker 未返回摘要。"

    result = CrawlTaskResult(
        status=status,
        summary=summary,
        artifacts=artifacts,
        sources=_extract_sources(summary),
        warnings=warnings,
    )
    return {
        "messages": [
            AIMessage(
                content=json.dumps(
                    result.model_dump(mode="json"),
                    ensure_ascii=False,
                )
            )
        ]
    }


builder = StateGraph(CrawlWorkerState)
builder.add_node("ensure_sandbox", _ensure_sandbox)
builder.add_node("crawl_agent", crawl_agent)
builder.add_node("export_workspace", _export_workspace)
builder.add_node("build_result", _build_structured_result)
builder.add_edge(START, "ensure_sandbox")
builder.add_edge("ensure_sandbox", "crawl_agent")
builder.add_edge("crawl_agent", "export_workspace")
builder.add_edge("export_workspace", "build_result")
builder.add_edge("build_result", END)

graph = builder.compile(name="crawl-worker")
