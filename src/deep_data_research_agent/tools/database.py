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
from psycopg import sql as pg_sql

from deep_data_research_agent.core.config import get_settings
from deep_data_research_agent.infrastructure.sandbox import manager as sandbox_manager

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
    runtime: ToolRuntime | None,
) -> Any:
    tools = await _load_remote_tools()
    remote_tool = tools[tool_name]
    settings = get_settings()
    # The surrounding trace records only size/status metadata.  Disabling the
    # adapter's automatic child trace prevents large exported rows being copied
    # into LangSmith while keeping the MCP lifecycle observable.
    with tracing_context(enabled=False):
        async with asyncio.timeout(settings.postgres_mcp_tool_timeout_seconds):
            # HTTP 数据浏览复用只读通道，但不创建 Agent 运行或沙箱。
            return await remote_tool.ainvoke(arguments, config=runtime.config if runtime else None)


async def _invoke_text(
    tool_name: str,
    arguments: dict[str, Any],
    runtime: ToolRuntime | None,
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


def _parse_object_metadata(text: str, expected_type: type) -> Any:
    """Accept the MCP server's JSON or Python repr, never executable expressions."""
    try:
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = ast.literal_eval(text)
        if not isinstance(value, expected_type):
            raise TypeError("unexpected metadata shape")
        return value
    except (ValueError, SyntaxError, TypeError) as exc:
        raise DatabaseToolError("MCP 结构结果格式无法解析，保留原始结果") from exc


async def _catalog_metadata(query: str, runtime: ToolRuntime) -> dict[str, Any]:
    """Run only internally built catalog SQL through the existing read-only MCP.

    Names are SQL literals, not executable SQL. Do not apply the user-query regex:
    valid quoted names can contain words such as 'update' or semicolons.
    """
    text = await _invoke_text("execute_sql", {"sql": _bridge_sql(query, 1)}, runtime)
    rows = _parse_bridge_rows(text)
    if len(rows) != 1:
        raise DatabaseToolError("补充元数据不可用：对象不存在、不可读或结果不唯一")
    return rows[0]


@tool("database_list_schemas")
async def database_list_schemas(runtime: ToolRuntime) -> str:
    """列出 PostgreSQL schema 及业务注释；补充查询失败时返回明确警告。"""
    try:
        original = await _invoke_text("list_schemas", {}, runtime)
    except DatabaseToolError as exc:
        return _json_result("error", error=_safe_error(exc))
    try:
        schemas = _parse_object_metadata(original, list)
        if any(not isinstance(s, dict) or not isinstance(s.get("schema_name"), str) for s in schemas):
            raise DatabaseToolError("MCP schema 列表格式无法解析")
        names = ", ".join(pg_sql.Literal(s["schema_name"]).as_string() for s in schemas)
        if schemas:
            metadata = await _catalog_metadata(f"""
SELECT COALESCE(jsonb_object_agg(n.nspname, d.description), '{{}}'::jsonb) AS comments
FROM pg_catalog.pg_namespace n
LEFT JOIN pg_catalog.pg_description d ON d.objoid=n.oid
 AND d.classoid='pg_catalog.pg_namespace'::regclass AND d.objsubid=0
WHERE n.nspname IN ({names}) AND has_schema_privilege(n.oid, 'USAGE')
""", runtime)
            comments = metadata["comments"]
            if not isinstance(comments, dict) or any(s["schema_name"] not in comments for s in schemas):
                raise DatabaseToolError("部分 schema 注释不可读取，不能判定为无注释")
            for schema in schemas:
                schema["schema_comment"] = comments[schema["schema_name"]]
        return _json_result("success", result=json.dumps(schemas, ensure_ascii=False), metadata_status="complete")
    except (DatabaseToolError, KeyError, TypeError, ValueError) as exc:
        return _json_result("success", result=original, metadata_status="partial", warnings=[_safe_error(exc)])


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
    """读取字段、业务注释、实际约束和索引；metadata_status=partial 时须核验警告。"""
    arguments = {"schema_name": schema_name, "object_name": object_name, "object_type": object_type}
    if object_type not in {"table", "view"}:
        return await _simple_remote_result("get_object_details", arguments, runtime)
    try:
        original = await _invoke_text("get_object_details", arguments, runtime)
    except DatabaseToolError as exc:
        return _json_result("error", error=_safe_error(exc))
    try:
        details = _parse_object_metadata(original, dict)
        if not isinstance(details.get("basic"), dict) or not isinstance(details.get("columns"), list):
            raise DatabaseToolError("MCP 对象结构格式无法解析")
        if any(not isinstance(c, dict) or not isinstance(c.get("column"), str) for c in details["columns"]):
            raise DatabaseToolError("MCP 字段结构格式无法解析")
        schema_literal = pg_sql.Literal(schema_name).as_string()
        object_literal = pg_sql.Literal(object_name).as_string()
        kinds = "'r','p'" if object_type == "table" else "'v','m'"
        metadata = await _catalog_metadata(f"""
SELECT
(SELECT d.description FROM pg_catalog.pg_description d
 WHERE d.objoid=c.oid AND d.classoid='pg_catalog.pg_class'::regclass AND d.objsubid=0) AS table_comment,
COALESCE((SELECT jsonb_object_agg(a.attname,d.description)
 FROM pg_catalog.pg_attribute a LEFT JOIN pg_catalog.pg_description d
 ON d.objoid=a.attrelid AND d.objsubid=a.attnum AND d.classoid='pg_catalog.pg_class'::regclass
 WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped), '{{}}'::jsonb) AS column_comments,
COALESCE((SELECT jsonb_agg(jsonb_build_object('name',k.conname,'type',k.contype,
 'definition',pg_get_constraintdef(k.oid)) ORDER BY k.conname)
 FROM pg_catalog.pg_constraint k WHERE k.conrelid=c.oid AND k.contype IN ('p','u','f','c','x')),
 '[]'::jsonb) AS constraints
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname={schema_literal} AND c.relname={object_literal} AND c.relkind IN ({kinds})
 AND has_schema_privilege(n.oid,'USAGE') AND has_table_privilege(c.oid,'SELECT')
""", runtime)
        comments = metadata["column_comments"]
        if not isinstance(comments, dict) or set(comments) != {c["column"] for c in details["columns"]}:
            raise DatabaseToolError("基础字段与补充元数据不一致，请重新探查")
        if not isinstance(metadata["constraints"], list):
            raise DatabaseToolError("补充约束格式无效")
        details["basic"]["comment"] = metadata["table_comment"]
        for column in details["columns"]:
            column["comment"] = comments[column["column"]]
        # The catalog is authoritative; the MCP's information_schema view can omit
        # constraints for a SELECT-only role. An actual empty list remains empty.
        details["constraints"] = metadata["constraints"]
        return _json_result("success", result=json.dumps(details, ensure_ascii=False), metadata_status="complete")
    except (DatabaseToolError, KeyError, TypeError, ValueError) as exc:
        return _json_result("success", result=original, metadata_status="partial", warnings=[_safe_error(exc)])


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
