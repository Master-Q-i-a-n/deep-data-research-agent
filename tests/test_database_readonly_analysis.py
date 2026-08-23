"""PostgreSQL MCP tools and database analysis Skill regression tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.runtime import ExecutionInfo, Runtime

from deep_data_research_agent.agents.prompts import (
    DATA_ANALYST_PROMPT,
    SUPERVISOR_PROMPT,
)
from deep_data_research_agent.infrastructure.mongodb.store import _public_seed_values
from deep_data_research_agent.tools import database as database_tools

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = (
    PROJECT_ROOT
    / "src"
    / "deep_data_research_agent"
    / "skills"
    / "data-analyst"
    / "database-readonly-analysis"
)


def _runtime(thread_id: str = "thread-database") -> Runtime:
    return Runtime(
        execution_info=ExecutionInfo(
            checkpoint_id="checkpoint",
            checkpoint_ns="",
            task_id="task-database",
            thread_id=thread_id,
        )
    )


def test_database_skill_is_builtin_planning_guidance() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text("utf-8")
    files = set(_public_seed_values("data-analyst"))

    assert len(text.splitlines()) <= 100
    assert "name: database-readonly-analysis" in text
    for stage in (
        "理解任务并规划",
        "确认数据库结构",
        "查询与深度分析",
        "验证与报告",
    ):
        assert stage in text
    assert "不假定固定业务指标" in text
    assert "write_todos" in text
    assert "/workspace/output/final_report.md" in text
    assert "不得只完成结构" in text
    assert "needs_input" in text
    assert "相对于主报告的路径嵌入" in text
    assert "ask_user" not in text
    assert "request_report_download" not in text
    assert "/active/database-readonly-analysis/SKILL.md" in files
    assert "PostgreSQL" not in SUPERVISOR_PROMPT
    assert "PostgreSQL 只读分析" in DATA_ANALYST_PROMPT


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "WITH removed AS (DELETE FROM orders RETURNING *) SELECT * FROM removed",
        "SELECT * INTO backup FROM orders",
        "SELECT 1; SELECT 2",
        "VACUUM orders",
    ],
)
def test_readonly_sql_guard_rejects_unsafe_statements(sql: str) -> None:
    with pytest.raises(database_tools.DatabaseToolError):
        database_tools._normalize_readonly_sql(sql)


def test_readonly_sql_guard_accepts_select_and_cte() -> None:
    assert database_tools._normalize_readonly_sql("SELECT 1;") == "SELECT 1"
    assert database_tools._normalize_readonly_sql(
        "-- probe\nWITH totals AS (SELECT 1 AS value) SELECT * FROM totals"
    ).startswith("-- probe")


def test_bridge_parser_uses_literal_and_inner_json() -> None:
    rows = [
        {"order_id": "001", "amount": 12.5, "tags": ["new"]},
        {"order_id": "002", "amount": None, "tags": []},
    ]
    server_text = repr(
        [{"__bridge_json": json.dumps(rows, ensure_ascii=False)}]
    )

    assert database_tools._parse_bridge_rows(server_text) == rows
    with pytest.raises(database_tools.DatabaseToolError):
        database_tools._parse_bridge_rows("__import__('os').system('whoami')")


@pytest.mark.asyncio
async def test_disabled_mcp_does_not_create_client(monkeypatch) -> None:
    monkeypatch.setattr(database_tools, "_client", None)
    monkeypatch.setattr(database_tools, "_remote_tools", None)
    monkeypatch.setattr(
        database_tools,
        "get_settings",
        lambda: SimpleNamespace(
            postgres_mcp_enabled=False,
            postgres_mcp_url="http://127.0.0.1:8000/sse",
        ),
    )

    with pytest.raises(database_tools.DatabaseToolError, match="未启用"):
        await database_tools._load_remote_tools()
    assert database_tools._client is None


@pytest.mark.asyncio
async def test_query_rows_caps_preview_and_marks_truncation(monkeypatch) -> None:
    rows = [{"value": 1}, {"value": 2}, {"value": 3}]
    server_text = repr(
        [{"__bridge_json": json.dumps(rows, ensure_ascii=False)}]
    )

    async def fake_invoke(*_args, **_kwargs):
        return server_text

    monkeypatch.setattr(database_tools, "_invoke_text", fake_invoke)

    result, truncated, normalized = await database_tools._query_rows(
        "SELECT value FROM sample",
        2,
        _runtime(),
    )
    assert result == rows[:2]
    assert truncated is True
    assert normalized == "SELECT value FROM sample"


@pytest.mark.asyncio
async def test_deterministic_mcp_error_is_not_retried(monkeypatch) -> None:
    calls = 0

    async def fake_invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return [{"type": "text", "text": "Error: relation does not exist"}]

    monkeypatch.setattr(database_tools, "_invoke_remote_tool", fake_invoke)

    with pytest.raises(database_tools.DatabaseToolError, match="relation does not exist"):
        await database_tools._invoke_text("execute_sql", {"sql": "SELECT 1"}, _runtime())
    assert calls == 1


@pytest.mark.asyncio
async def test_transient_mcp_error_retries_once(monkeypatch) -> None:
    calls = 0

    async def fake_invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("temporary SSE failure")
        return [{"type": "text", "text": "[{'value': 1}]"}]

    monkeypatch.setattr(database_tools, "_invoke_remote_tool", fake_invoke)

    result = await database_tools._invoke_text(
        "execute_sql",
        {"sql": "SELECT 1"},
        _runtime(),
    )
    assert result == "[{'value': 1}]"
    assert calls == 2


@pytest.mark.asyncio
async def test_database_tool_exports_query_to_shared_sandbox(
    monkeypatch,
) -> None:
    uploads: list[dict] = []

    async def fake_query_rows(_sql, row_limit, _runtime):
        assert row_limit == 50_000
        return ([{"order_id": "001", "amount": 12.5}], False, "SELECT 1")

    async def fake_upload(thread_id, files, **kwargs):
        uploads.append(
            {"thread_id": thread_id, "files": files, "kwargs": kwargs}
        )

    monkeypatch.setattr(database_tools, "_query_rows", fake_query_rows)
    monkeypatch.setattr(
        database_tools,
        "get_settings",
        lambda: SimpleNamespace(
            postgres_mcp_export_rows=50_000,
            postgres_mcp_export_bytes=20 * 1024 * 1024,
        ),
    )
    monkeypatch.setattr(
        database_tools.sandbox_manager.SANDBOX_MANAGER,
        "upload_workspace_files",
        fake_upload,
    )
    result = json.loads(
        await database_tools.database_query_to_file.coroutine(
            sql="SELECT 1",
            output_name="orders",
            runtime=_runtime(),
        )
    )
    assert result["status"] == "success"
    assert result["path"] == "/workspace/database/orders.csv"
    assert uploads[0]["thread_id"] == "thread-database"
    assert uploads[0]["kwargs"] == {"component": "supervisor", "persist": True}
    paths = [path for path, _content in uploads[0]["files"]]
    assert paths == [
        "/workspace/database/orders.csv",
        "/workspace/database/orders.meta.json",
    ]


@pytest.mark.asyncio
async def test_export_refuses_truncated_result_without_upload(monkeypatch) -> None:
    uploaded = False

    async def fake_query_rows(*_args, **_kwargs):
        return ([{"value": 1}], True, "SELECT value FROM sample")

    async def fake_upload(*_args, **_kwargs):
        nonlocal uploaded
        uploaded = True

    monkeypatch.setattr(database_tools, "_query_rows", fake_query_rows)
    monkeypatch.setattr(
        database_tools,
        "get_settings",
        lambda: SimpleNamespace(
            postgres_mcp_export_rows=50_000,
            postgres_mcp_export_bytes=20 * 1024 * 1024,
        ),
    )
    monkeypatch.setattr(
        database_tools.sandbox_manager.SANDBOX_MANAGER,
        "upload_workspace_files",
        fake_upload,
    )

    result = json.loads(
        await database_tools.database_query_to_file.coroutine(
            sql="SELECT value FROM sample",
            output_name="too-large",
            runtime=_runtime(),
        )
    )
    assert result["status"] == "error"
    assert "超过 50000 行" in result["error"]
    assert uploaded is False


@pytest.mark.asyncio
async def test_database_tool_returns_mcp_failure_as_json(monkeypatch) -> None:
    async def fake_invoke(*_args, **_kwargs):
        raise ConnectionError("SSE service unavailable")

    monkeypatch.setattr(database_tools, "_invoke_remote_tool", fake_invoke)
    result = json.loads(
        await database_tools.database_list_schemas.coroutine(runtime=_runtime())
    )
    assert result["status"] == "error"
    assert "SSE service unavailable" in result["error"]


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_MCP_INTEGRATION") != "1",
    reason="需要显式启用本地 PostgreSQL MCP 集成测试",
)
@pytest.mark.asyncio
async def test_real_postgres_mcp_lists_schemas() -> None:
    result = json.loads(
        await database_tools.database_list_schemas.coroutine(
            runtime=_runtime("thread-real-database")
        )
    )
    assert result["status"] == "success", result.get("error", result)
    assert "olist" in result["result"] or "public" in result["result"]
