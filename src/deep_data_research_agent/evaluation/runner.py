"""Single-run evaluation harness for the Supervisor graph.

The harness deliberately keeps credentials in memory, creates one fresh thread per
case, exports evidence before cleanup, and never approves the email tool.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import re
import secrets
import shutil
import statistics
import sys
import time
import uuid
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import yaml
from dotenv import load_dotenv
from langchain_core.messages import AIMessage
from langgraph_sdk import get_client
from langsmith import Client as LangSmithClient
from pydantic import BaseModel, Field, ValidationError, model_validator
from pymongo import MongoClient

from deep_data_research_agent.agents.model_profile import DATA_ANALYST_MODEL_PROFILE
from deep_data_research_agent.core.config import create_chat_model, get_settings
from deep_data_research_agent.database import repository as database

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CASES_PATH = REPO_ROOT / "evals" / "cases.yaml"
DEFAULT_DATA_ROOT = REPO_ROOT / "data"
DEFAULT_OUTPUT_ROOT = DEFAULT_DATA_ROOT / "evaluations"
EXPECTED_FILE_HASHES = {
    "dry_bean_dataset.csv": "4C0DC65987B30C725A72E3076FE601E3CB3EA6CC2E203201D9D8E0BD57ED15E9",
    "test.csv": "56023B9948236F3C7A1C9448FCF418B283E109EF177FA8C7E069158DD7DD52B2",
    "winequality-red.csv": "F3369D57793DE153647A77D183F360D8F8D646A67D937787F75300D3B7A73E00",
}
TERMINAL_ASYNC_STATUSES = {"success", "error", "cancelled", "timeout", "interrupted"}
SECRET_KEY_RE = re.compile(
    r"(?:authorization|password|token|api[_-]?key|secret)", re.IGNORECASE
)
SECRET_TEXT_RE = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+|"
    r"((?:api[_-]?key|password|token|secret)\s*[:=]\s*)[^\s,;]+"
)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))")


class EvalCase(BaseModel):
    """Validated manifest entry for one non-repeated evaluation case."""

    id: str = Field(pattern=r"^[A-Z][0-9]{2}$")
    category: str
    prompt: str = Field(min_length=1)
    input_files: list[str] = Field(default_factory=list)
    report_required: bool
    email_approval_expected: bool
    async_expected: bool
    expected_terminal: Literal[
        "answer",
        "report",
        "email_rejected",
        "ask_user",
        "honest_failure",
        "skill_assigned",
    ]
    gold: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(ge=30, le=7200)

    @model_validator(mode="after")
    def validate_flags(self) -> EvalCase:
        if self.email_approval_expected and not self.report_required:
            raise ValueError("邮件审批题必须同时要求报告")
        if self.expected_terminal == "email_rejected" and not self.email_approval_expected:
            raise ValueError("email_rejected 题必须声明 email_approval_expected")
        return self


class CaseManifest(BaseModel):
    version: int = 1
    cases: list[EvalCase]

    @model_validator(mode="after")
    def validate_unique_cases(self) -> CaseManifest:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("评测题 ID 重复")
        return self


class JudgeResult(BaseModel):
    """Small JSON contract requested from the same-model evaluator."""

    tool_selection_accuracy: float = Field(ge=0, le=1)
    tool_argument_accuracy: float = Field(ge=0, le=1)
    answer_correct: bool | None = None
    report_quality: dict[str, float] | None = None
    missing_required_actions: list[str] = Field(default_factory=list)
    unnecessary_actions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _as_utc(value: datetime) -> datetime:
    """Normalize LangSmith's occasionally naive first-token timestamp."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    return str(value)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def _redact(value: Any, *, max_text: int = 12_000) -> Any:
    """Remove credentials while retaining enough tool evidence for judging."""

    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SECRET_KEY_RE.search(str(key)) else _redact(item, max_text=max_text)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, max_text=max_text) for item in value[:200]]
    if isinstance(value, bytes):
        return f"[bytes:{len(value)}]"
    if isinstance(value, str):
        text = SECRET_TEXT_RE.sub(lambda match: (match.group(1) or match.group(2) or "") + "[REDACTED]", value)
        return text if len(text) <= max_text else text[:max_text] + "…[truncated]"
    return value


def load_manifest(path: Path = DEFAULT_CASES_PATH) -> CaseManifest:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return CaseManifest.model_validate(raw)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _endpoint_host_port(raw: str, default_port: int) -> tuple[str, int]:
    candidate = raw if "://" in raw else f"http://{raw}"
    parsed = urlsplit(candidate)
    if not parsed.hostname:
        raise ValueError(f"无法解析服务地址：{raw}")
    return parsed.hostname, parsed.port or default_port


async def _tcp_probe(raw: str, default_port: int, name: str) -> None:
    host, port = _endpoint_host_port(raw, default_port)
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
        del reader
        writer.close()
        await writer.wait_closed()
    except Exception as exc:
        raise RuntimeError(f"{name} 不可用：{host}:{port}") from exc


async def preflight(cases: list[EvalCase], agent_url: str, data_root: Path) -> dict[str, Any]:
    """Fail before account creation when required infrastructure is unavailable."""

    settings = get_settings()
    checks: dict[str, Any] = {"checked_at": _iso(_utc_now())}
    for name, expected in EXPECTED_FILE_HASHES.items():
        path = data_root / name
        if not path.is_file():
            raise RuntimeError(f"缺少评测文件：{path}")
        actual = await asyncio.to_thread(_sha256, path)
        if actual != expected:
            raise RuntimeError(f"评测文件哈希不匹配：{name}")
    checks["csv_hashes"] = "matched"

    async with httpx.AsyncClient(base_url=agent_url, timeout=10) as client:
        response = await client.get("/auth/me")
        if response.status_code not in {200, 401}:
            raise RuntimeError(f"Agent Server 预检失败：HTTP {response.status_code}")
    checks["agent_server"] = "available"

    if not settings.openai_api_key.strip():
        raise RuntimeError("OPENAI_API_KEY 未配置")
    if not settings.mongodb_uri.strip() or not settings.postgres_uri.strip():
        raise RuntimeError("MongoDB 或 PostgreSQL 应用连接未配置")
    if not settings.mongodb_database.strip():
        raise RuntimeError("MONGODB_DATABASE 未配置")
    checks["model_and_persistence"] = "configured"

    if any(case.category.startswith("database") for case in cases):
        if not settings.postgres_mcp_enabled:
            raise RuntimeError("数据库题需要 POSTGRES_MCP_ENABLED=true")
        await _tcp_probe(settings.postgres_mcp_url, 8000, "PostgreSQL MCP")
        checks["postgres_mcp"] = "available"

    # Every Supervisor run initializes its sandbox, including direct Q&A.
    await _tcp_probe(settings.open_sandbox_domain, 8080, "OpenSandbox")
    checks["open_sandbox"] = "available"
    if any(case.async_expected for case in cases):
        if not settings.tavily_api_key.strip():
            raise RuntimeError("网页题需要 TAVILY_API_KEY")
        checks["tavily"] = "configured"

    # LANGSMITH_* is owned by LangChain rather than the application Settings model.
    if not str(__import__("os").environ.get("LANGSMITH_API_KEY", "")).strip():
        raise RuntimeError("LANGSMITH_API_KEY 未配置")
    if str(__import__("os").environ.get("LANGSMITH_TRACING", "")).lower() not in {"1", "true", "yes"}:
        raise RuntimeError("LANGSMITH_TRACING 未开启")
    langsmith = LangSmithClient()
    project = __import__("os").environ.get("LANGSMITH_PROJECT", "default")
    await asyncio.to_thread(lambda: next(iter(langsmith.list_runs(project_name=project, limit=1)), None))
    checks["langsmith_project"] = project
    return checks


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(content or "")


def _last_ai_message(values: Any) -> str:
    if not isinstance(values, dict):
        return ""
    messages = values.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict):
            kind = str(message.get("type") or message.get("role") or "").lower()
            if kind in {"ai", "assistant"}:
                return _message_text(message.get("content"))
        elif isinstance(message, AIMessage):
            return _message_text(message.content)
    return ""


def _interrupt_actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for interrupt in state.get("interrupts") or []:
        value = interrupt.get("value") if isinstance(interrupt, dict) else getattr(interrupt, "value", None)
        if not isinstance(value, dict):
            continue
        for action in value.get("action_requests") or []:
            if isinstance(action, dict):
                normalized = dict(action)
                if "args" not in normalized and "arguments" in normalized:
                    normalized["args"] = normalized["arguments"]
                actions.append(normalized)
    return actions


def _state_dict(state: Any) -> dict[str, Any]:
    return dict(state) if isinstance(state, dict) else state.model_dump(mode="json")


async def _submit_run(
    graph_client: Any,
    thread_id: str,
    *,
    timeout_seconds: float,
    metadata: dict[str, Any],
    message: str | None = None,
    command: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any], str | None]:
    created: dict[str, str] = {}

    def on_created(item: Any) -> None:
        if isinstance(item, dict):
            created.update({key: str(value) for key, value in item.items() if value is not None})
        else:
            run_id = getattr(item, "run_id", None)
            if run_id is not None:
                created["run_id"] = str(run_id)

    kwargs: dict[str, Any] = {
        "metadata": metadata,
        "on_disconnect": "continue",
        "on_run_created": on_created,
        "raise_error": False,
    }
    if message is not None:
        kwargs["input"] = {"messages": [{"type": "human", "content": message}]}
    if command is not None:
        kwargs["command"] = command
    try:
        async with asyncio.timeout(timeout_seconds):
            result = await graph_client.runs.wait(thread_id, "supervisor", **kwargs)
    except TimeoutError:
        run_id = created.get("run_id")
        if run_id:
            await graph_client.runs.cancel(thread_id, run_id, wait=False, action="interrupt")
        raise
    state = _state_dict(await graph_client.threads.get_state(thread_id))
    return result, state, created.get("run_id")


async def _poll_async_tasks(
    http: httpx.AsyncClient,
    thread_id: str,
    *,
    deadline: float,
) -> list[dict[str, Any]]:
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        response = await http.post("/async-tasks/status", json={"thread_id": thread_id})
        response.raise_for_status()
        last = [dict(item) for item in response.json().get("tasks", []) if isinstance(item, dict)]
        if last and all(str(item.get("status")) in TERMINAL_ASYNC_STATUSES for item in last):
            return last
        await asyncio.sleep(5)
    raise TimeoutError("异步 crawl-worker 在 case 时限内未结束")


def _safe_extract_zip(content: bytes, destination: Path) -> list[str]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for info in archive.infolist():
            pure = PurePosixPath(info.filename)
            if pure.is_absolute() or ".." in pure.parts:
                raise RuntimeError(f"产物 ZIP 包含不安全路径：{info.filename}")
            target = (destination / Path(*pure.parts)).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError(f"产物 ZIP 路径越界：{info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
            extracted.append(target.relative_to(destination).as_posix())
    return extracted


async def _export_artifacts(
    http: httpx.AsyncClient,
    thread_id: str,
    case_dir: Path,
) -> dict[str, Any]:
    response = await http.get(f"/artifacts/{thread_id}")
    response.raise_for_status()
    cards = response.json().get("artifacts", [])
    artifact_dir = case_dir / "artifacts"
    manifest: dict[str, Any] = {"cards": cards, "files": []}
    for item in cards:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        virtual_path = item["path"]
        suffix = PurePosixPath(virtual_path).suffix.lower()
        if suffix == ".md":
            bundle = await http.get(
                f"/artifacts/{thread_id}/bundle",
                params={"path": virtual_path},
            )
            if bundle.is_success:
                manifest["files"].extend(_safe_extract_zip(bundle.content, artifact_dir))
                continue
        download = await http.get(
            f"/artifacts/{thread_id}/download",
            params={"path": virtual_path},
        )
        download.raise_for_status()
        relative = PurePosixPath(virtual_path.removeprefix("/workspace/"))
        target = artifact_dir / Path(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(download.content)
        manifest["files"].append(target.relative_to(artifact_dir).as_posix())
    _write_json(case_dir / "artifacts.json", manifest)
    return manifest


def _validate_report(case_dir: Path) -> tuple[bool, list[str], str]:
    artifact_dir = case_dir / "artifacts"
    markdowns = sorted(artifact_dir.rglob("*.md")) if artifact_dir.exists() else []
    pdfs = sorted(artifact_dir.rglob("*.pdf")) if artifact_dir.exists() else []
    warnings: list[str] = []
    if not markdowns:
        warnings.append("缺少 Markdown 报告")
    if not pdfs:
        warnings.append("缺少 PDF 报告")
    valid_pdfs = [path for path in pdfs if path.stat().st_size > 4 and path.read_bytes()[:5] == b"%PDF-"]
    if pdfs and not valid_pdfs:
        warnings.append("PDF 文件头无效")
    report_text = ""
    if markdowns:
        preferred = next((path for path in markdowns if path.name == "final_report.md"), markdowns[0])
        report_text = preferred.read_text(encoding="utf-8", errors="replace")
        if len(report_text.strip()) < 200:
            warnings.append("Markdown 报告内容过短")
        for match in MARKDOWN_IMAGE_RE.finditer(report_text):
            source = (match.group(1) or match.group(2) or "").split("#", 1)[0].split("?", 1)[0]
            if not source or source.startswith(("http://", "https://", "data:")):
                continue
            pure = PurePosixPath(source)
            if pure.is_absolute() or ".." in pure.parts:
                warnings.append(f"报告图片路径不安全：{source}")
                continue
            if not (preferred.parent / Path(*pure.parts)).is_file():
                warnings.append(f"报告图片不存在：{source}")
    return not warnings, warnings, report_text


def _run_metadata(run: Any) -> dict[str, Any]:
    extra = run.extra or {}
    metadata = extra.get("metadata") if isinstance(extra, dict) else None
    return metadata if isinstance(metadata, dict) else {}


def _trace_io(value: Any, *, max_chars: int) -> Any:
    """Redact tool IO and cap the serialized payload without losing small arguments."""
    redacted = _redact(value, max_text=max_chars)
    serialized = json.dumps(redacted, ensure_ascii=False, default=_json_default)
    if len(serialized) <= max_chars:
        return redacted
    return {"preview": serialized[:max_chars], "truncated": True}


def _trace_record(run: Any, *, record_kind: Literal["root", "llm", "tool"]) -> dict[str, Any]:
    metadata = _run_metadata(run)
    compact_metadata = {
        key: metadata[key]
        for key in (
            "eval_run_id",
            "eval_case_id",
            "thread_id",
            "lc_agent_name",
            "langgraph_node",
            "graph_id",
            "kind",
        )
        if key in metadata
    }
    record = {
        "id": str(run.id),
        "trace_id": str(run.trace_id),
        "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
        "record_kind": record_kind,
        "name": run.name,
        "run_type": run.run_type,
        "start_time": _iso(run.start_time),
        "end_time": _iso(run.end_time),
        "first_token_time": _iso(run.first_token_time),
        "status": run.status,
        "error": _redact(run.error),
        "prompt_tokens": run.prompt_tokens,
        "completion_tokens": run.completion_tokens,
        "total_tokens": run.total_tokens,
        "prompt_token_details": run.prompt_token_details or {},
        "completion_token_details": run.completion_token_details or {},
        "tags": list(run.tags or []),
        "metadata": _redact(compact_metadata),
    }
    if record_kind == "tool":
        record["inputs"] = _trace_io(run.inputs, max_chars=12_000)
        record["outputs"] = _trace_io(run.outputs, max_chars=6_000)
    return record


def _metadata_filter(eval_run_id: str, eval_case_id: str) -> str:
    """Build an exact LangSmith server-side metadata filter for one case."""
    run_value = json.dumps({"eval_run_id": eval_run_id}, ensure_ascii=False, separators=(",", ":"))
    case_value = json.dumps({"eval_case_id": eval_case_id}, ensure_ascii=False, separators=(",", ":"))
    return f"and(has(metadata, '{run_value}'), has(metadata, '{case_value}'))"


async def _collect_trace(
    langsmith: LangSmithClient,
    project: str,
    eval_run_id: str,
    eval_case_id: str,
    started_at: datetime,
    case_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    deadline = time.monotonic() + 120
    filter_expression = _metadata_filter(eval_run_id, eval_case_id)
    root_filter = (
        f"and({filter_expression}, "
        "has(metadata, '{\"graph_id\":\"supervisor\"}'))"
    )
    common_select = [
        "id", "trace_id", "parent_run_id", "name", "run_type", "start_time", "end_time",
        "first_token_time", "status", "error", "prompt_tokens", "completion_tokens", "total_tokens",
        "prompt_token_details", "completion_token_details", "tags", "extra",
    ]
    roots: list[Any] = []
    llm_runs: list[Any] = []
    tool_runs: list[Any] = []
    while time.monotonic() < deadline:
        roots, llm_runs, tool_runs = await asyncio.gather(
            asyncio.to_thread(
                lambda: list(
                    langsmith.list_runs(
                        project_name=project,
                        start_time=started_at,
                        filter=root_filter,
                        is_root=True,
                        select=common_select,
                    )
                )
            ),
            asyncio.to_thread(
                lambda: list(
                    langsmith.list_runs(
                        project_name=project,
                        start_time=started_at,
                        filter=filter_expression,
                        run_type="llm",
                        select=common_select,
                    )
                )
            ),
            asyncio.to_thread(
                lambda: list(
                    langsmith.list_runs(
                        project_name=project,
                        start_time=started_at,
                        filter=filter_expression,
                        run_type="tool",
                        select=[*common_select, "inputs", "outputs"],
                    )
                )
            ),
        )
        observed = [*roots, *llm_runs, *tool_runs]
        if llm_runs and all(run.end_time is not None for run in observed):
            break
        await asyncio.sleep(3)

    tagged = [(run, "root") for run in roots]
    tagged.extend((run, "llm") for run in llm_runs)
    tagged.extend((run, "tool") for run in tool_runs)
    # A synthetic root can also be typed as llm/tool; retain its root representation once.
    unique: dict[str, tuple[Any, Literal["root", "llm", "tool"]]] = {}
    for run, kind in tagged:
        unique.setdefault(str(run.id), (run, kind))
    records = [
        _trace_record(run, record_kind=kind)
        for run, kind in sorted(unique.values(), key=lambda item: item[0].start_time)
    ]
    _write_json(case_dir / "trace.json", records)
    excluded_tags = {"eval-judge", "memory-internal", "failure-review"}
    llms = [
        run
        for run in llm_runs
        if not excluded_tags.intersection(run.tags or [])
    ]
    prompt_tokens = sum(int(run.prompt_tokens or 0) for run in llms)
    completion_tokens = sum(int(run.completion_tokens or 0) for run in llms)
    cache_read = sum(int((run.prompt_token_details or {}).get("cache_read", 0) or 0) for run in llms)
    supervisors = sorted(
        (
            run
            for run in llms
            if _run_metadata(run).get("lc_agent_name") == "supervisor"
        ),
        key=lambda item: item.start_time,
    )
    ttft_ms: float | None = None
    if supervisors and supervisors[0].first_token_time is not None:
        ttft_ms = (
            _as_utc(supervisors[0].first_token_time)
            - _as_utc(supervisors[0].start_time)
        ).total_seconds() * 1000
    components = Counter(str(_run_metadata(run).get("lc_agent_name") or "unknown") for run in llms)
    tools = [record for record in records if record["record_kind"] == "tool"]
    metrics = {
        "llm_call_count": len(llms) if llms else None,
        "llm_component_counts": dict(components),
        "supervisor_ttft_ms": ttft_ms,
        "input_tokens": prompt_tokens if llms else None,
        "output_tokens": completion_tokens if llms else None,
        "total_tokens": prompt_tokens + completion_tokens if llms else None,
        "cache_read_tokens": cache_read if llms else None,
        "cache_hit_rate": cache_read / prompt_tokens if prompt_tokens else None,
        "tool_calls": tools,
        "trace_run_count": len(records),
        "trace_warnings": [],
    }
    if not llms:
        metrics["trace_warnings"].append(
            "LangSmith 未在 120 秒内返回本 case 的叶级 LLM trace，相关调用与 token 指标记为 N/A。"
        )
    elif not supervisors or ttft_ms is None:
        metrics["trace_warnings"].append(
            "LangSmith 未返回首个 Supervisor LLM 的 first_token_time，TTFT 记为 N/A。"
        )
    return records, metrics


def _contains_required_values(text: str, required: list[Any]) -> bool:
    normalized = text.casefold().replace(",", "").replace("，", "")
    return all(str(value).casefold().replace(",", "") in normalized for value in required)


def _tool_completed(trace: list[dict[str, Any]], name: str) -> bool:
    for run in trace:
        if run.get("run_type") != "tool" or run.get("name") != name or run.get("error"):
            continue
        output = json.dumps(run.get("outputs"), ensure_ascii=False, default=_json_default)
        if '"status": "error"' not in output and "failed" not in output.casefold():
            return True
    return False


def _skill_persisted(user_hash: str, skill_name: str, target: str) -> bool:
    settings = get_settings()
    client = MongoClient(settings.mongodb_uri)
    try:
        collection = client[settings.mongodb_database][settings.mongodb_skill_collection]
        return collection.count_documents(
            {
                "namespace": [user_hash, "skills", target],
                "key": f"/manifests/{skill_name}.json",
            },
            limit=1,
        ) == 1
    finally:
        client.close()


def _hard_success(
    case: EvalCase,
    *,
    final_response: str,
    report_valid: bool,
    actions: list[dict[str, Any]],
    email_approval_valid: bool,
    trace: list[dict[str, Any]],
    async_task_ids: set[str],
    user_hash: str,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if case.report_required and not report_valid:
        reasons.append("正式报告硬校验失败")
    if case.async_expected and not async_task_ids:
        reasons.append("未观察到异步 crawl-worker 任务")
    if case.email_approval_expected and not email_approval_valid:
        reasons.append("未到达参数正确的邮件审批中断")
    if case.expected_terminal == "ask_user":
        if not any(action.get("name") == "ask_user" for action in actions):
            reasons.append("未进入 ask_user 中断")
        if async_task_ids:
            reasons.append("需求澄清前启动了异步任务")
    elif case.expected_terminal == "honest_failure":
        honest = any(term in final_response.casefold() for term in ("不存在", "未找到", "无法", "缺少", "重新上传", "needs_input", "failed"))
        if not honest or report_valid:
            reasons.append("未诚实报告缺失文件，或生成了虚假报告")
    elif case.expected_terminal == "skill_assigned":
        skill_name = str(case.gold.get("skill_name"))
        target = str(case.gold.get("target"))
        persisted = _skill_persisted(user_hash, skill_name, target)
        if not persisted or not _tool_completed(trace, "assign_skill"):
            reasons.append("Skill 未正确持久化或 assign_skill 未成功")
        if not _tool_completed(trace, "execute"):
            reasons.append("未观察到成功的功能测试 execute")
    required = case.gold.get("required_values")
    if isinstance(required, list) and not _contains_required_values(final_response, required):
        reasons.append("简答未命中全部 Gold 值")
    if case.expected_terminal in {"answer", "report", "email_rejected"} and not final_response.strip():
        reasons.append("缺少 Supervisor 最终回复")
    return not reasons, reasons


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Judge 未返回 JSON 对象")
    return json.loads(stripped[start : end + 1])


async def _judge_case(case: EvalCase, result: dict[str, Any], case_dir: Path, run_id: str) -> tuple[JudgeResult | None, dict[str, int]]:
    report_text = result.get("report_text") or ""
    tool_trace = [
        {
            "name": item.get("name"),
            "inputs": item.get("inputs"),
            "outputs": item.get("outputs"),
            "error": item.get("error"),
        }
        for item in result.get("tool_calls", [])
    ]
    schema = {
        "tool_selection_accuracy": "0到1",
        "tool_argument_accuracy": "0到1",
        "answer_correct": "true/false/null",
        "report_quality": {
            "correctness": "0到5",
            "completeness": "0到5",
            "evidence_traceability": "0到5",
            "analysis_actionability": "0到5",
            "writing_quality": "0到5",
            "overall": "0到100，按35/20/20/15/10加权",
        },
        "missing_required_actions": [],
        "unnecessary_actions": [],
        "notes": [],
    }
    prompt = (
        "你是同模型独立评测 Judge。不得假设存在参考工具轨迹；根据用户任务、系统职责边界、"
        "实际工具调用和产物判断工具选择与参数是否准确。没有报告时 report_quality 必须为 null。"
        "有报告时按正确性35%、完整性20%、证据可追溯性20%、分析价值15%、表达10%评分。"
        "只返回一个 JSON 对象，不要代码围栏。\n\n"
        f"输出结构：{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"CASE：{case.id}\n任务：{case.prompt}\nGold：{json.dumps(case.gold, ensure_ascii=False)}\n"
        f"最终回复：{str(result.get('final_response') or '')[:20000]}\n"
        f"工具轨迹：{json.dumps(tool_trace, ensure_ascii=False, default=_json_default)[:60000]}\n"
        f"报告：{report_text[:80000]}"
    )
    model = create_chat_model(
        harness_provider=DATA_ANALYST_MODEL_PROFILE.harness_provider,
        streaming=False,
    ).with_config(
        tags=["eval-judge"],
        metadata={"eval_run_id": run_id, "eval_case_id": case.id, "kind": "eval-judge"},
    )
    response = await model.ainvoke(prompt)
    usage = response.usage_metadata or {}
    try:
        judged = JudgeResult.model_validate(_extract_json_object(_message_text(response.content)))
    except (ValueError, json.JSONDecodeError, ValidationError) as exc:
        _write_json(case_dir / "judge_error.json", {"error": str(exc), "raw": _redact(_message_text(response.content))})
        return None, {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
    _write_json(case_dir / "judge.json", judged.model_dump(mode="json"))
    return judged, {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


async def _run_case(
    case: EvalCase,
    *,
    run_id: str,
    user_hash: str,
    graph_client: Any,
    http: httpx.AsyncClient,
    langsmith: LangSmithClient,
    project: str,
    data_root: Path,
    run_dir: Path,
) -> dict[str, Any]:
    case_dir = run_dir / "cases" / case.id
    case_dir.mkdir(parents=True, exist_ok=True)
    thread_id = str(uuid.uuid4())
    metadata = {
        "graph_id": "supervisor",
        "kind": "evaluation",
        "title": f"Eval {case.id}",
        "eval_run_id": run_id,
        "eval_case_id": case.id,
    }
    await graph_client.threads.create(thread_id=thread_id, graph_id="supervisor", metadata=metadata)
    uploaded_paths: list[str] = []
    for name in case.input_files:
        path = data_root / name
        with path.open("rb") as handle:
            response = await http.post(
                f"/files/{thread_id}",
                files={"files": (name, handle, "text/csv")},
            )
        response.raise_for_status()
        uploaded_paths.extend(str(item["path"]) for item in response.json().get("files", []))
    message = case.prompt
    if uploaded_paths:
        message += "\n\n已上传文件：\n" + "\n".join(f"- {path}" for path in uploaded_paths)

    result: dict[str, Any] = {
        "case_id": case.id,
        "category": case.category,
        "thread_id": thread_id,
        "warnings": [],
        "infrastructure_error": None,
        "email_approval_valid": False,
        "time_to_email_approval_ms": None,
        "async_task_ids": [],
    }
    state: dict[str, Any] = {}
    all_actions: list[dict[str, Any]] = []
    async_ids: set[str] = set()
    checked_ids: set[str] = set()
    # Thread creation and uploads are setup; E2E starts before the first Supervisor run.
    started_at = _utc_now()
    started_clock = time.monotonic()
    result["started_at"] = _iso(started_at)
    deadline = started_clock + case.timeout_seconds
    try:
        _raw, state, _run = await _submit_run(
            graph_client,
            thread_id,
            timeout_seconds=max(1, deadline - time.monotonic()),
            metadata=metadata,
            message=message,
        )
        for _round in range(6):
            actions = _interrupt_actions(state)
            all_actions.extend(actions)
            if actions:
                if any(action.get("name") == "ask_user" for action in actions):
                    break
                decisions: list[dict[str, str]] = []
                for action in actions:
                    if action.get("name") == "send_report_email":
                        args = action.get("args") if isinstance(action.get("args"), dict) else {}
                        valid = str(args.get("recipient") or "").casefold() == "eval@example.com"
                        valid = valid and bool(args.get("pdf_path")) and bool(args.get("markdown_path"))
                        result["email_approval_valid"] = bool(valid)
                        result["time_to_email_approval_ms"] = (time.monotonic() - started_clock) * 1000
                    decisions.append({"type": "reject", "message": "评测策略禁止执行外部副作用。"})
                _raw, state, _run = await _submit_run(
                    graph_client,
                    thread_id,
                    timeout_seconds=max(1, deadline - time.monotonic()),
                    metadata=metadata,
                    command={"resume": {"decisions": decisions}},
                )
                continue

            status_response = await http.post("/async-tasks/status", json={"thread_id": thread_id})
            status_response.raise_for_status()
            tasks = [dict(item) for item in status_response.json().get("tasks", []) if isinstance(item, dict)]
            async_ids.update(str(item.get("task_id")) for item in tasks if item.get("task_id"))
            child_threads = {
                str(item.get("thread_id") or item.get("task_id"))
                for item in tasks
                if item.get("thread_id") or item.get("task_id")
            }
            result.setdefault("child_thread_ids", []).extend(sorted(child_threads - set(result.get("child_thread_ids", []))))
            unchecked = async_ids - checked_ids
            if unchecked:
                tasks = await _poll_async_tasks(http, thread_id, deadline=deadline)
                ids = [str(item.get("task_id")) for item in tasks if item.get("task_id") in unchecked]
                checked_ids.update(ids)
                follow_up = "请调用 check_async_task 依次检查以下任务；读取业务 status，并继续完成原始任务：" + "、".join(ids)
                _raw, state, _run = await _submit_run(
                    graph_client,
                    thread_id,
                    timeout_seconds=max(1, deadline - time.monotonic()),
                    metadata=metadata,
                    message=follow_up,
                )
                continue
            break
    except TimeoutError as exc:
        result["infrastructure_error"] = str(exc) or "case timeout"
    except Exception as exc:  # noqa: BLE001 - preserve remaining benchmark cases.
        result["infrastructure_error"] = f"{type(exc).__name__}: {exc}"

    # Freeze the terminal boundary before exporting local artifacts or traces.
    terminal_at = _utc_now()
    result["e2e_ms"] = (time.monotonic() - started_clock) * 1000
    result["finished_at"] = _iso(terminal_at)
    result["async_task_ids"] = sorted(async_ids)
    values = state.get("values") if isinstance(state, dict) else {}
    result["final_response"] = _last_ai_message(values)
    (case_dir / "final_response.md").write_text(result["final_response"], encoding="utf-8")
    result["interrupt_actions"] = _redact(all_actions)
    _write_json(case_dir / "interrupts.json", result["interrupt_actions"])
    artifact_export_started = time.monotonic()
    try:
        await _export_artifacts(http, thread_id, case_dir)
    except Exception as exc:  # noqa: BLE001 - artifact failure is a case result.
        result["warnings"].append(f"产物导出失败：{type(exc).__name__}: {exc}")
    finally:
        result["artifact_export_ms"] = (time.monotonic() - artifact_export_started) * 1000
    report_valid, report_warnings, report_text = _validate_report(case_dir)
    result["report_valid"] = report_valid
    result["report_text"] = report_text
    result["warnings"].extend(report_warnings if case.report_required else [])

    trace_export_started = time.monotonic()
    try:
        trace, metrics = await _collect_trace(langsmith, project, run_id, case.id, started_at, case_dir)
    except Exception as exc:  # noqa: BLE001 - trace lag must not erase case output.
        trace, metrics = [], {
            "llm_call_count": None,
            "llm_component_counts": {},
            "supervisor_ttft_ms": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cache_read_tokens": None,
            "cache_hit_rate": None,
            "tool_calls": [],
            "trace_run_count": 0,
            "trace_warnings": [],
        }
        result["warnings"].append(f"LangSmith trace 导出失败：{type(exc).__name__}: {exc}")
    finally:
        result["trace_export_ms"] = (time.monotonic() - trace_export_started) * 1000
    result["warnings"].extend(metrics.pop("trace_warnings", []))
    result.update(metrics)
    success, reasons = await asyncio.to_thread(
        _hard_success,
        case,
        final_response=result["final_response"],
        report_valid=report_valid,
        actions=all_actions,
        email_approval_valid=bool(result["email_approval_valid"]),
        trace=trace,
        async_task_ids=async_ids,
        user_hash=user_hash,
    )
    result["task_success"] = success and result["infrastructure_error"] is None
    result["success_reasons"] = reasons
    # Large report text is already stored as an artifact and omitted from result.json.
    serializable = {key: value for key, value in result.items() if key not in {"report_text", "tool_calls"}}
    _write_json(case_dir / "result.json", serializable)
    return result


def _purge_mongo_user_state(user_hash: str) -> dict[str, int]:
    settings = get_settings()
    client = MongoClient(settings.mongodb_uri)
    try:
        mongo = client[settings.mongodb_database]
        skills = mongo[settings.mongodb_skill_collection].delete_many({"namespace.0": user_hash}).deleted_count
        memories = mongo[settings.mongodb_memory_collection].delete_many({"namespace.0": user_hash}).deleted_count
        jobs = mongo[settings.mongodb_memory_job_collection].delete_many(
            {"$or": [{"scope": user_hash}, {"source_user_hash": user_hash}]}
        ).deleted_count
        return {"skills": int(skills), "memories": int(memories), "memory_jobs": int(jobs)}
    finally:
        client.close()


def _mongo_user_counts(user_hash: str) -> dict[str, int]:
    settings = get_settings()
    client = MongoClient(settings.mongodb_uri)
    try:
        mongo = client[settings.mongodb_database]
        return {
            "skills": mongo[settings.mongodb_skill_collection].count_documents({"namespace.0": user_hash}),
            "memories": mongo[settings.mongodb_memory_collection].count_documents({"namespace.0": user_hash}),
            "memory_jobs": mongo[settings.mongodb_memory_job_collection].count_documents(
                {"$or": [{"scope": user_hash}, {"source_user_hash": user_hash}]}
            ),
        }
    finally:
        client.close()


def _remove_exact_user_artifact_root(user_id: str) -> bool:
    root = get_settings().artifact_root.resolve()
    target = (root / user_id).resolve()
    if target.parent != root or target.name != user_id:
        raise RuntimeError("拒绝删除未通过边界校验的用户产物目录")
    if target.exists():
        shutil.rmtree(target)
        return True
    return False


async def _cleanup_account(
    *,
    graph_client: Any,
    user_id: str,
    user_hash: str,
    username: str,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "username": username,
        "user_id": user_id,
        "started_at": _iso(_utc_now()),
        "errors": [],
        "deleted_threads": [],
    }
    try:
        threads: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await graph_client.threads.search(limit=100, offset=offset)
            threads.extend(dict(item) for item in page)
            if len(page) < 100:
                break
            offset += len(page)
        for thread in threads:
            thread_id = str(thread.get("thread_id"))
            try:
                for run in await graph_client.runs.list(thread_id, limit=100):
                    if str(run.get("status")) in {"pending", "running", "queued"}:
                        await graph_client.runs.cancel(thread_id, str(run["run_id"]), wait=True, action="interrupt")
                await graph_client.threads.delete(thread_id)
                receipt["deleted_threads"].append(thread_id)
            except Exception as exc:  # noqa: BLE001 - continue purging other resources.
                receipt["errors"].append(f"thread {thread_id}: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        receipt["errors"].append(f"thread search: {type(exc).__name__}: {exc}")

    try:
        receipt["mongo_deleted"] = await asyncio.to_thread(_purge_mongo_user_state, user_hash)
    except Exception as exc:  # noqa: BLE001
        receipt["errors"].append(f"mongo purge: {type(exc).__name__}: {exc}")
    try:
        receipt["artifact_root_deleted"] = await asyncio.to_thread(_remove_exact_user_artifact_root, user_id)
    except Exception as exc:  # noqa: BLE001
        receipt["errors"].append(f"artifact purge: {type(exc).__name__}: {exc}")
    try:
        receipt["postgres_user_deleted"] = await database.delete_user(user_id)
    except Exception as exc:  # noqa: BLE001
        receipt["errors"].append(f"postgres purge: {type(exc).__name__}: {exc}")

    try:
        receipt["remaining_mongo"] = await asyncio.to_thread(_mongo_user_counts, user_hash)
        receipt["remaining_threads"] = await database.list_user_thread_ids(user_id)
        receipt["user_exists"] = await database.get_user_by_id(user_id) is not None
        receipt["artifact_root_exists"] = (get_settings().artifact_root.resolve() / user_id).exists()
    except Exception as exc:  # noqa: BLE001
        receipt["errors"].append(f"cleanup verification: {type(exc).__name__}: {exc}")
    receipt["cleanup_verified"] = (
        not receipt["errors"]
        and not receipt.get("user_exists", True)
        and not receipt.get("remaining_threads", ["unknown"])
        and not receipt.get("artifact_root_exists", True)
        and all(value == 0 for value in receipt.get("remaining_mongo", {"unknown": 1}).values())
    )
    receipt["finished_at"] = _iso(_utc_now())
    return receipt


def _mean(values: list[float | int | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return statistics.fmean(usable) if usable else None


def _percentile(values: list[float | int | None], fraction: float) -> float | None:
    usable = sorted(float(value) for value in values if value is not None)
    if not usable:
        return None
    index = min(len(usable) - 1, max(0, int((len(usable) - 1) * fraction + 0.999999)))
    return usable[index]


def _build_summary(results: list[dict[str, Any]], judge_tokens: dict[str, int]) -> dict[str, Any]:
    core = [item for item in results if item["category"] != "diagnostic"]
    diagnostics = [item for item in results if item["category"] == "diagnostic"]
    report_scores = [item.get("report_quality_score") for item in results]
    total_input = sum(int(item.get("input_tokens") or 0) for item in results)
    total_cache = sum(int(item.get("cache_read_tokens") or 0) for item in results)
    return {
        "case_count": len(results),
        "core_success": sum(bool(item["task_success"]) for item in core),
        "core_total": len(core),
        "core_success_rate": sum(bool(item["task_success"]) for item in core) / len(core) if core else None,
        "diagnostic_success": sum(bool(item["task_success"]) for item in diagnostics),
        "diagnostic_total": len(diagnostics),
        "diagnostic_success_rate": sum(bool(item["task_success"]) for item in diagnostics) / len(diagnostics) if diagnostics else None,
        "llm_calls_total": sum(int(item.get("llm_call_count") or 0) for item in results),
        "llm_calls_mean": _mean([item.get("llm_call_count") for item in results]),
        "supervisor_ttft_ms_mean": _mean([item.get("supervisor_ttft_ms") for item in results]),
        "supervisor_ttft_ms_p95": _percentile([item.get("supervisor_ttft_ms") for item in results], 0.95),
        "e2e_ms_mean": _mean([item.get("e2e_ms") for item in results]),
        "e2e_ms_p95": _percentile([item.get("e2e_ms") for item in results], 0.95),
        "e2e_ms_total": sum(float(item.get("e2e_ms") or 0) for item in results),
        "artifact_export_ms_total": sum(float(item.get("artifact_export_ms") or 0) for item in results),
        "artifact_export_ms_mean": _mean([item.get("artifact_export_ms") for item in results]),
        "trace_export_ms_total": sum(float(item.get("trace_export_ms") or 0) for item in results),
        "trace_export_ms_mean": _mean([item.get("trace_export_ms") for item in results]),
        "input_tokens": total_input,
        "output_tokens": sum(int(item.get("output_tokens") or 0) for item in results),
        "total_tokens": sum(int(item.get("total_tokens") or 0) for item in results),
        "cache_read_tokens": total_cache,
        "cache_hit_rate": total_cache / total_input if total_input else None,
        "tool_selection_accuracy": _mean([item.get("tool_selection_accuracy") for item in results]),
        "tool_argument_accuracy": _mean([item.get("tool_argument_accuracy") for item in results]),
        "report_quality_score": _mean(report_scores),
        "report_quality_coverage": sum(value is not None for value in report_scores),
        "judge_tokens": judge_tokens,
        "single_run_baseline": True,
    }


def _write_metrics_csv(path: Path, results: list[dict[str, Any]]) -> None:
    fields = [
        "case_id", "category", "task_success", "llm_call_count", "supervisor_ttft_ms", "e2e_ms",
        "artifact_export_ms", "trace_export_ms",
        "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_hit_rate",
        "tool_selection_accuracy", "tool_argument_accuracy", "report_quality_score", "infrastructure_error",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)


def _write_report(path: Path, summary: dict[str, Any], results: list[dict[str, Any]], cleanup: dict[str, Any]) -> None:
    def percent(value: Any) -> str:
        return "N/A" if value is None else f"{float(value) * 100:.1f}%"

    def milliseconds(value: Any) -> str:
        return "N/A" if value is None else f"{float(value):.0f} ms"

    lines = [
        "# Agent 单次基线评测报告",
        "",
        "> 每题只执行一次，本报告不提供置信区间。邮件仅验证到审批边界并统一拒绝，未执行 SMTP。",
        "",
        "## 汇总",
        "",
        f"- 核心任务成功率：{summary['core_success']}/{summary['core_total']}（{percent(summary['core_success_rate'])}）",
        f"- 诊断通过率：{summary['diagnostic_success']}/{summary['diagnostic_total']}（{percent(summary['diagnostic_success_rate'])}）",
        f"- LLM 调用总数：{summary['llm_calls_total']}",
        f"- Supervisor 平均 TTFT：{milliseconds(summary['supervisor_ttft_ms_mean'])}",
        f"- 端到端延时：平均 {summary['e2e_ms_mean'] or 0:.0f} ms，总计 {summary['e2e_ms_total']:.0f} ms",
        f"- 产物导出耗时：平均 {summary['artifact_export_ms_mean'] or 0:.0f} ms，总计 {summary['artifact_export_ms_total']:.0f} ms",
        f"- Trace 导出耗时：平均 {summary['trace_export_ms_mean'] or 0:.0f} ms，总计 {summary['trace_export_ms_total']:.0f} ms",
        f"- Agent 总 token：{summary['total_tokens']}",
        f"- 缓存命中率：{percent(summary['cache_hit_rate'])}",
        f"- 工具选择准确率：{percent(summary['tool_selection_accuracy'])}",
        f"- 工具参数准确率：{percent(summary['tool_argument_accuracy'])}",
        f"- 报告质量平均分：{summary['report_quality_score'] if summary['report_quality_score'] is not None else 'N/A'}",
        f"- 临时账号清理：{'通过' if cleanup.get('cleanup_verified') else '失败'}",
        "",
        "## 分题结果",
        "",
        "| Case | 类型 | 成功 | LLM调用 | TTFT(ms) | E2E(ms) | 产物导出(ms) | Trace导出(ms) | Token | 工具选择 | 工具参数 | 报告质量 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in results:
        lines.append(
            "| {case_id} | {category} | {success} | {calls} | {ttft} | {e2e:.0f} | {artifact:.0f} | {trace:.0f} | {tokens} | {selection} | {arguments} | {quality} |".format(
                case_id=item["case_id"],
                category=item["category"],
                success="是" if item["task_success"] else "否",
                calls="N/A" if item.get("llm_call_count") is None else item["llm_call_count"],
                ttft="N/A" if item.get("supervisor_ttft_ms") is None else f"{item['supervisor_ttft_ms']:.0f}",
                e2e=float(item.get("e2e_ms") or 0),
                artifact=float(item.get("artifact_export_ms") or 0),
                trace=float(item.get("trace_export_ms") or 0),
                tokens="N/A" if item.get("total_tokens") is None else item["total_tokens"],
                selection="N/A" if item.get("tool_selection_accuracy") is None else f"{item['tool_selection_accuracy'] * 100:.0f}%",
                arguments="N/A" if item.get("tool_argument_accuracy") is None else f"{item['tool_argument_accuracy'] * 100:.0f}%",
                quality="N/A" if item.get("report_quality_score") is None else f"{item['report_quality_score']:.1f}",
            )
        )
    failed = [item for item in results if not item["task_success"]]
    if failed:
        lines.extend(["", "## 失败与警告", ""])
        for item in failed:
            details = item.get("success_reasons") or item.get("warnings") or [item.get("infrastructure_error") or "未提供原因"]
            lines.append(f"- **{item['case_id']}**：{'；'.join(str(value) for value in details)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run_evaluation(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.cases))
    selected_ids = set(args.case or [])
    cases = [case for case in manifest.cases if not selected_ids or case.id in selected_ids]
    if selected_ids - {case.id for case in cases}:
        raise RuntimeError(f"未知 case：{sorted(selected_ids - {case.id for case in cases})}")
    run_id = f"eval-{_utc_now().strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
    run_dir = Path(args.output_root).resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    checks = await preflight(cases, args.agent_url, Path(args.data_root))
    _write_json(run_dir / "preflight.json", checks)
    print(f"[eval] run_id={run_id} cases={len(cases)} output={run_dir}", flush=True)

    username = f"eval-{_utc_now().strftime('%m%d%H%M%S')}-{secrets.token_hex(3)}"
    password = secrets.token_urlsafe(32)
    user_id: str | None = None
    user_hash: str | None = None
    graph_client: Any = None
    results: list[dict[str, Any]] = []
    cleanup: dict[str, Any] = {"cleanup_verified": False, "errors": ["账号尚未创建"]}
    langsmith = LangSmithClient()
    project = __import__("os").environ.get("LANGSMITH_PROJECT", "default")
    try:
        async with httpx.AsyncClient(base_url=args.agent_url, timeout=120) as anonymous:
            response = await anonymous.post(
                "/auth/register",
                json={"username": username, "password": password, "confirm_password": password},
            )
            response.raise_for_status()
            payload = response.json()
            token = str(payload["token"])
            user_id = str(payload["user"]["id"])
            user_hash = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
        headers = {"Authorization": f"Bearer {token}"}
        graph_client = get_client(url=args.agent_url, headers=headers, timeout=120)
        async with httpx.AsyncClient(base_url=args.agent_url, headers=headers, timeout=120) as http:
            memory = await http.patch(
                "/memories/settings",
                json={"failure_lesson_saving_enabled": False},
            )
            memory.raise_for_status()
            if memory.json().get("failure_lesson_saving_enabled") is not False:
                raise RuntimeError("未能关闭失败经验保存")
            for index, case in enumerate(cases, start=1):
                print(f"[eval] {index}/{len(cases)} {case.id} started", flush=True)
                case_result = await _run_case(
                    case,
                    run_id=run_id,
                    user_hash=user_hash,
                    graph_client=graph_client,
                    http=http,
                    langsmith=langsmith,
                    project=project,
                    data_root=Path(args.data_root),
                    run_dir=run_dir,
                )
                results.append(case_result)
                print(
                    f"[eval] {case.id} done success={case_result['task_success']} "
                    f"llm_calls={case_result['llm_call_count']} e2e_ms={case_result['e2e_ms']:.0f}",
                    flush=True,
                )
    finally:
        if graph_client is not None and user_id is not None and user_hash is not None:
            cleanup = await _cleanup_account(
                graph_client=graph_client,
                user_id=user_id,
                user_hash=user_hash,
                username=username,
            )
            await graph_client.aclose()
        _write_json(run_dir / "cleanup_receipt.json", cleanup)
        await database.close_database()

    judge_tokens = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    case_by_id = {case.id: case for case in cases}
    for index, result in enumerate(results, start=1):
        print(f"[judge] {index}/{len(results)} {result['case_id']}", flush=True)
        judged, usage = await _judge_case(
            case_by_id[result["case_id"]],
            result,
            run_dir / "cases" / result["case_id"],
            run_id,
        )
        for key in judge_tokens:
            judge_tokens[key] += usage[key]
        if judged is None:
            result["tool_selection_accuracy"] = None
            result["tool_argument_accuracy"] = None
            result["report_quality_score"] = None
            result["report_quality_dimensions"] = None
            result["warnings"].append("Judge 输出不可解析")
        else:
            result["tool_selection_accuracy"] = judged.tool_selection_accuracy
            result["tool_argument_accuracy"] = judged.tool_argument_accuracy
            quality = judged.report_quality if case_by_id[result["case_id"]].report_required else None
            result["report_quality_dimensions"] = quality
            result["report_quality_score"] = quality.get("overall") if quality else None
        serializable = {key: value for key, value in result.items() if key not in {"report_text", "tool_calls"}}
        _write_json(run_dir / "cases" / result["case_id"] / "result.json", serializable)

    summary = _build_summary(results, judge_tokens)
    summary.update(
        {
            "run_id": run_id,
            "model": get_settings().openai_model,
            "langsmith_project": project,
            "cleanup_verified": cleanup.get("cleanup_verified", False),
        }
    )
    _write_json(run_dir / "summary.json", summary)
    _write_metrics_csv(run_dir / "metrics.csv", results)
    _write_report(run_dir / "evaluation_report.md", summary, results, cleanup)
    print(f"[eval] complete report={run_dir / 'evaluation_report.md'}", flush=True)
    return 0 if cleanup.get("cleanup_verified") else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the single-pass Supervisor evaluation suite.")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--case", action="append", help="Run selected case IDs; repeat the option as needed.")
    parser.add_argument("--agent-url", default="http://127.0.0.1:2024")
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    return parser


def main() -> None:
    try:
        # LangSmith reads configuration from environment variables rather than
        # the application's Settings model. Keep this out of module import so
        # importing the evaluator cannot contaminate other tests.
        load_dotenv(REPO_ROOT / ".env", override=False)
        # Psycopg's Windows async implementation requires a selector loop.
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        code = asyncio.run(run_evaluation(_parser().parse_args()))
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:  # noqa: BLE001 - CLI must leave a clear terminal error.
        print(f"[eval] fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
