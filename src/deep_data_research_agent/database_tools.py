"""Stable Supervisor tools backed by the standalone PostgreSQL MCP server."""

from __future__ import annotations

import ast
import asyncio
import csv
import hashlib
import io
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langsmith import traceable, tracing_context

from deep_data_research_agent import sandbox_manager
from deep_data_research_agent.config import get_settings

_REQUIRED_REMOTE_TOOLS = {
    "list_schemas",
    "list_objects",
    "get_object_details",
    "execute_sql",
}
_FORBIDDEN_SQL = re.compile(
    r"\b(?:insert|update|delete|merge|alter|create|drop|truncate|grant|revoke|"
    r"copy|call|do|vacuum|refresh|reindex|cluster|lock|set|reset|into)\b",
    re.IGNORECASE,
)
_LEADING_SQL_COMMENTS = re.compile(
    r"^(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)+",
    re.DOTALL,
)
_DATABASE_URI = re.compile(r"(?i)(?:postgres(?:ql)?://)[^\s@]+@")

_client: MultiServerMCPClient | None = None
_remote_tools: dict[str, BaseTool] | None = None
_tools_lock = asyncio.Lock()


class DatabaseToolError(RuntimeError):
    """Expected configuration, validation, or remote database failure."""


def _json_result(status: str, **payload: Any) -> str:
    return json.dumps({"status": status, **payload}, ensure_ascii=False)


def _safe_error(exc: BaseException) -> str:
    """Remove credentials and keep errors compact enough for the model."""

    message = _DATABASE_URI.sub("postgresql://***@", str(exc)).strip()
    if len(message) > 500:
        message = message[:497] + "..."
    return message or exc.__class__.__name__


def _trace_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    arguments = dict(inputs.get("arguments") or {})
    sql = str(arguments.pop("sql", ""))
    return {
        "tool_name": inputs.get("tool_name"),
        "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest() if sql else None,
        "argument_names": sorted(arguments),
    }


def _trace_output(output: Any) -> dict[str, Any]:
    """Do not copy database rows into the explicit MCP lifecycle trace."""

    try:
        text = _extract_text(output)
    except Exception:  # noqa: BLE001  # pragma: no cover - trace processor
        text = ""
    return {
        "content_type": type(output).__name__,
        "content_chars": len(text),
        "is_error": text.lstrip().lower().startswith("error:"),
    }


async def _load_remote_tools() -> dict[str, BaseTool]:
    """Discover MCP tools lazily; failed discovery is never cached."""

    global _client, _remote_tools
    if _remote_tools is not None:
        return _remote_tools

    settings = get_settings()
    if not settings.postgres_mcp_enabled:
        raise DatabaseToolError("PostgreSQL MCP 未启用，请配置 POSTGRES_MCP_ENABLED=true")
    if not settings.postgres_mcp_url.strip():
        raise DatabaseToolError("POSTGRES_MCP_URL 不能为空")

    async with _tools_lock:
        if _remote_tools is not None:
            return _remote_tools
        client = MultiServerMCPClient(
            {
                "postgres": {
                    "transport": "sse",
                    "url": settings.postgres_mcp_url,
                    "timeout": settings.postgres_mcp_connect_timeout_seconds,
                    "sse_read_timeout": settings.postgres_mcp_tool_timeout_seconds + 5,
                }
            },
            handle_tool_errors=True,
        )
        async with asyncio.timeout(settings.postgres_mcp_connect_timeout_seconds):
            discovered = await client.get_tools(server_name="postgres")
        by_name = {remote_tool.name: remote_tool for remote_tool in discovered}
        missing = sorted(_REQUIRED_REMOTE_TOOLS - by_name.keys())
        if missing:
            raise DatabaseToolError(
                "PostgreSQL MCP 缺少必要工具：" + "、".join(missing)
            )
        _client = client
        _remote_tools = by_name
        return by_name


def _extract_text(result: Any) -> str:
    """Normalize LangChain MCP content blocks without assuming one SDK shape."""

    if isinstance(result, ToolMessage):
        return _extract_text(result.content)
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts: list[str] = []
        for block in result:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        if parts:
            return "\n".join(parts)
    raise DatabaseToolError("PostgreSQL MCP 返回了无法识别的内容格式")


@traceable(
    name="database.mcp_call",
    run_type="tool",
    process_inputs=_trace_inputs,
    process_outputs=_trace_output,
)
async def _invoke_remote_tool(
    tool_name: str,
    arguments: dict[str, Any],
    runtime: ToolRuntime,
) -> Any:
    tools = await _load_remote_tools()
    remote_tool = tools[tool_name]
    settings = get_settings()
    # The surrounding trace records only size/status metadata.  Disabling the
    # adapter's automatic child trace prevents large exported rows being copied
    # into LangSmith while keeping the MCP lifecycle observable.
    with tracing_context(enabled=False):
        async with asyncio.timeout(settings.postgres_mcp_tool_timeout_seconds):
            return await remote_tool.ainvoke(arguments, config=runtime.config)


async def _invoke_text(
    tool_name: str,
    arguments: dict[str, Any],
    runtime: ToolRuntime,
) -> str:
    """Retry only transport/session failures; server SQL errors are deterministic."""

    last_error: BaseException | None = None
    for attempt in range(2):
        try:
            text = _extract_text(
                await _invoke_remote_tool(tool_name, arguments, runtime)
            ).strip()
        except DatabaseToolError:
            raise
        except Exception as exc:  # transport implementations raise varied SDK errors
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(0.2)
                continue
            raise DatabaseToolError(
                f"PostgreSQL MCP 连接或调用失败：{_safe_error(exc)}"
            ) from exc
        if text.lower().startswith("error:"):
            raise DatabaseToolError("数据库查询失败：" + text[6:].strip())
        return text
    raise DatabaseToolError(_safe_error(last_error or RuntimeError("未知错误")))


def _normalize_readonly_sql(sql: str) -> str:
    """Apply a conservative local guard in addition to MCP and role restrictions."""

    normalized = sql.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    if not normalized or ";" in normalized:
        raise DatabaseToolError("只允许一条只读 SQL，不能包含多条语句")
    statement = _LEADING_SQL_COMMENTS.sub("", normalized).lstrip()
    if not re.match(r"(?i)^(select|with)\b", statement):
        raise DatabaseToolError("只允许 SELECT 或 WITH 查询")
    # This deliberately errs on the safe side.  The restricted MCP mode and
    # read-only database role remain the authoritative security boundaries.
    if _FORBIDDEN_SQL.search(statement):
        raise DatabaseToolError("SQL 包含写入、管理或锁定关键字，已拒绝执行")
    return normalized


def _bridge_sql(sql: str, row_limit: int) -> str:
    """Make the MCP's stringified TextContent carry strict inner JSON."""

    return f"""
SELECT COALESCE(jsonb_agg(to_jsonb(\"__mcp_row\")), '[]'::jsonb)::text
       AS \"__bridge_json\"
FROM (
    SELECT *
    FROM ({sql}) AS \"__mcp_source\"
    LIMIT {row_limit + 1}
) AS \"__mcp_row\"
""".strip()


def _parse_bridge_rows(text: str) -> list[dict[str, Any]]:
    """Parse the server's Python repr safely, then parse its inner JSON payload."""

    try:
        outer = ast.literal_eval(text)
        if not isinstance(outer, list) or len(outer) != 1:
            raise ValueError("outer result must contain one row")
        row = outer[0]
        if not isinstance(row, dict) or "__bridge_json" not in row:
            raise ValueError("bridge column missing")
        payload = json.loads(row["__bridge_json"])
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise ValueError("bridge payload is not a row list")
        return payload
    except (SyntaxError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise DatabaseToolError(
            "PostgreSQL MCP 查询结果格式不兼容，无法安全解析"
        ) from exc


async def _query_rows(
    sql: str,
    row_limit: int,
    runtime: ToolRuntime,
) -> tuple[list[dict[str, Any]], bool, str]:
    normalized = _normalize_readonly_sql(sql)
    text = await _invoke_text(
        "execute_sql",
        {"sql": _bridge_sql(normalized, row_limit)},
        runtime,
    )
    rows = _parse_bridge_rows(text)
    return rows[:row_limit], len(rows) > row_limit, normalized


def _database_filename(value: str) -> str:
    name = value.strip()
    if name.lower().endswith(".csv"):
        name = name[:-4].rstrip()
    if (
        not name
        or len(name) > 80
        or name in {".", ".."}
        or any(character in name for character in "/\\\x00\r\n")
    ):
        raise DatabaseToolError("输出名称无效，请提供不含目录的简短文件名")
    return name


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def _serialize_csv(rows: list[dict[str, Any]]) -> tuple[bytes, list[str]]:
    columns: list[str] = []
    for row in rows:
        for column in row:
            if column not in columns:
                columns.append(column)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
    if columns:
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(value) for key, value in row.items()})
    return stream.getvalue().encode("utf-8"), columns


async def _simple_remote_result(
    tool_name: str,
    arguments: dict[str, Any],
    runtime: ToolRuntime,
) -> str:
    try:
        result = await _invoke_text(tool_name, arguments, runtime)
        return _json_result("success", result=result)
    except Exception as exc:  # noqa: BLE001 - keep business failures in the tool
        return _json_result("error", error=_safe_error(exc))


@tool("database_list_schemas")
async def database_list_schemas(runtime: ToolRuntime) -> str:
    """列出只读 PostgreSQL 数据库中的 schema；数据库分析应先确认数据范围。"""

    return await _simple_remote_result("list_schemas", {}, runtime)


@tool("database_list_objects")
async def database_list_objects(
    schema_name: str,
    runtime: ToolRuntime,
    object_type: Literal["table", "view", "sequence", "extension"] = "table",
) -> str:
    """列出指定 schema 中的表、视图、序列或扩展。"""

    return await _simple_remote_result(
        "list_objects",
        {"schema_name": schema_name, "object_type": object_type},
        runtime,
    )


@tool("database_get_object_details")
async def database_get_object_details(
    schema_name: str,
    object_name: str,
    runtime: ToolRuntime,
    object_type: Literal["table", "view", "sequence", "extension"] = "table",
) -> str:
    """读取一个数据库对象的字段、约束和索引，避免模型猜测关联关系。"""

    return await _simple_remote_result(
        "get_object_details",
        {
            "schema_name": schema_name,
            "object_name": object_name,
            "object_type": object_type,
        },
        runtime,
    )


@tool("database_query_preview")
async def database_query_preview(sql: str, runtime: ToolRuntime) -> str:
    """执行单条只读查询并返回受限预览；适合聚合、样例和口径核查。"""

    try:
        settings = get_settings()
        rows, truncated, _normalized = await _query_rows(
            sql,
            settings.postgres_mcp_preview_rows,
            runtime,
        )
        return _json_result(
            "success",
            row_count=len(rows),
            truncated=truncated,
            rows=rows,
        )
    except Exception as exc:  # noqa: BLE001 - keep business failures in the tool
        return _json_result("error", error=_safe_error(exc))


@tool("database_query_to_file")
async def database_query_to_file(
    sql: str,
    output_name: str,
    runtime: ToolRuntime,
) -> str:
    """把只读查询结果保存到共享沙箱，供 data-analyst 深度分析和制图。"""

    try:
        settings = get_settings()
        name = _database_filename(output_name)
        rows, truncated, normalized = await _query_rows(
            sql,
            settings.postgres_mcp_export_rows,
            runtime,
        )
        if truncated:
            raise DatabaseToolError(
                f"查询结果超过 {settings.postgres_mcp_export_rows} 行，请先聚合、过滤或拆分查询"
            )
        csv_content, columns = _serialize_csv(rows)
        if len(csv_content) > settings.postgres_mcp_export_bytes:
            raise DatabaseToolError(
                f"查询结果超过 {settings.postgres_mcp_export_bytes} 字节，请缩小数据范围"
            )

        csv_path = f"/workspace/database/{name}.csv"
        metadata_path = f"/workspace/database/{name}.meta.json"
        metadata = {
            "created_at": datetime.now(UTC).isoformat(),
            "row_count": len(rows),
            "columns": columns,
            "csv_bytes": len(csv_content),
            "sql_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        }
        thread_id = sandbox_manager.thread_id_from_runtime(runtime)
        await sandbox_manager.SANDBOX_MANAGER.upload_workspace_files(
            thread_id,
            [
                (csv_path, csv_content),
                (
                    metadata_path,
                    json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"),
                ),
            ],
            component="supervisor",
            persist=True,
        )
        return _json_result(
            "success",
            path=csv_path,
            metadata_path=metadata_path,
            row_count=len(rows),
            columns=columns,
            bytes=len(csv_content),
        )
    except Exception as exc:  # noqa: BLE001 - keep business failures in the tool
        return _json_result("error", error=_safe_error(exc))


DATABASE_TOOLS = [
    database_list_schemas,
    database_list_objects,
    database_get_object_details,
    database_query_preview,
    database_query_to_file,
]
