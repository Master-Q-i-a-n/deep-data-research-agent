"""本地表格分析 Skill 与确定性探查脚本的回归测试。"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox
from langchain_core.messages import AIMessage
from langgraph.runtime import ExecutionInfo, Runtime
from openpyxl import Workbook

from deep_data_research_agent.agents.prompts import (
    DATA_ANALYST_PROMPT,
    SUPERVISOR_PROMPT,
)
from deep_data_research_agent.agents.supervisor import graph as supervisor_graph
from deep_data_research_agent.infrastructure.mongodb.store import _public_seed_values
from deep_data_research_agent.infrastructure.sandbox import manager as sandbox_manager

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = (
    PROJECT_ROOT
    / "src"
    / "deep_data_research_agent"
    / "skills"
    / "data-analyst"
    / "tabular-data-analysis"
)
SCRIPT_PATH = SKILL_ROOT / "scripts" / "profile_table.py"


def _load_profile_module():
    spec = importlib.util.spec_from_file_location("profile_table", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tabular_skill_is_builtin_planning_guidance() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text("utf-8")
    files = set(_public_seed_values("data-analyst"))

    assert len(text.splitlines()) <= 100
    assert "name: tabular-data-analysis" in text
    for stage in (
        "理解任务并规划",
        "确定性探查",
        "制定并执行分析",
        "验证与输出",
    ):
        assert stage in text
    assert "不在本 Skill 中假定业务指标" in text
    assert "quick_answer" in text
    assert "formal_report" in text
    assert "不得生成 Markdown、CSV、JSON、PNG 或 PDF" in text
    assert "write_todos" in text
    assert "/workspace/output/final_report.md" in text
    assert "不得只完成探查" in text
    assert "needs_input" in text
    assert "相对于主报告的路径嵌入" in text
    assert "ask_user" not in text
    assert "request_report_download" not in text
    assert {
        "/active/tabular-data-analysis/SKILL.md",
        "/active/tabular-data-analysis/scripts/profile_table.py",
    } <= files
    assert "CSV" not in SUPERVISOR_PROMPT
    assert "CSV、TSV、XLSX" in DATA_ANALYST_PROMPT
    assert "needs_input" in DATA_ANALYST_PROMPT


def test_profile_csv_preserves_identifier_and_detects_duplicates(
    tmp_path: Path,
) -> None:
    module = _load_profile_module()
    input_path = tmp_path / "orders.csv"
    input_path.write_text(
        "order_id,amount,email\n001,10,a@example.com\n001,10,a@example.com\n002,,b@example.com\n",
        encoding="utf-8-sig",
    )

    profile = module.build_profile(input_path)
    table = profile["tables"][0]

    assert table["encoding"] == "utf-8-sig"
    assert table["duplicate_rows"] == 1
    assert table["fields"][0]["inferred_type"] == "string"
    assert table["fields"][1]["missing"] == 1
    assert table["sample_rows"][0]["email"] == "<已脱敏>"


def test_profile_gb18030_tsv(tmp_path: Path) -> None:
    module = _load_profile_module()
    input_path = tmp_path / "供应商.tsv"
    input_path.write_bytes("供应商\t交期\n甲公司\t7\n".encode("gb18030"))

    profile = module.build_profile(input_path)
    table = profile["tables"][0]

    assert profile["format"] == "tsv"
    assert table["encoding"] == "gb18030"
    assert [field["name"] for field in table["fields"]] == ["供应商", "交期"]


def test_profile_xlsx_lists_sheets_and_formula_limitations(tmp_path: Path) -> None:
    module = _load_profile_module()
    input_path = tmp_path / "workbook.xlsx"
    workbook = Workbook()
    active = workbook.active
    active.title = "订单"
    active.append(["物料号", "数量", "合计"])
    active.append(["0001", 2, "=B2*10"])
    hidden = workbook.create_sheet("说明")
    hidden.sheet_state = "hidden"
    hidden.append(["字段", "含义"])
    workbook.save(input_path)

    profile = module.build_profile(input_path)

    assert [table["name"] for table in profile["tables"]] == ["订单", "说明"]
    assert profile["tables"][0]["formula_cells"] == 1
    assert profile["tables"][0]["formula_cells_without_cache"] == 1
    assert profile["tables"][1]["state"] == "hidden"
    assert any("公式缺少缓存值" in warning for warning in profile["warnings"])


def test_profile_marks_scan_truncation(tmp_path: Path) -> None:
    module = _load_profile_module()
    module.MAX_PROFILE_ROWS = 2
    input_path = tmp_path / "large.csv"
    input_path.write_text("id\n1\n2\n3\n", encoding="utf-8")

    profile = module.build_profile(input_path)

    assert profile["tables"][0]["scanned_rows"] == 2
    assert profile["tables"][0]["truncated"] is True
    assert profile["warnings"]


def test_profile_reports_empty_table_without_inventing_columns(tmp_path: Path) -> None:
    module = _load_profile_module()
    input_path = tmp_path / "empty.csv"
    input_path.write_bytes(b"")

    profile = module.build_profile(input_path)

    assert profile["tables"][0]["blank"] is True
    assert profile["tables"][0]["fields"] == []
    assert profile["tables"][0]["sample_rows"] == []


def test_profile_cli_only_prints_compact_status(tmp_path: Path) -> None:
    input_path = tmp_path / "orders.csv"
    output_path = tmp_path / "profile" / "orders.json"
    input_path.write_text("id,value\n001,2\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status == {
        "status": "success",
        "profile": str(output_path),
        "tables": 1,
        "warning_count": 0,
    }
    assert output_path.is_file()


@pytest.mark.asyncio
async def test_compiled_supervisor_execute_node_runs_profile_script(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class LocalTestSandbox(BaseSandbox):
        @property
        def id(self) -> str:
            return "local-profile-test"

        def execute(self, command: str, *, timeout=None) -> ExecuteResponse:
            args = command if os.name == "nt" else shlex.split(command)
            result = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
            )
            return ExecuteResponse(
                output=result.stdout + result.stderr,
                exit_code=result.returncode,
            )

        def upload_files(self, files):
            return [
                FileUploadResponse(path=path, error=None) for path, _content in files
            ]

        def download_files(self, paths):
            return [
                FileDownloadResponse(path=path, content=None, error="not_needed")
                for path in paths
            ]

    input_path = tmp_path / "compiled.csv"
    output_path = tmp_path / "compiled-profile.json"
    input_path.write_text("id,value\n001,2\n", encoding="utf-8")
    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER,
        "get_backend",
        lambda *_args, **_kwargs: LocalTestSandbox(),
    )
    command = subprocess.list2cmdline(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ]
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "execute",
                "args": {"command": command},
                "id": "call-profile",
                "type": "tool_call",
            }
        ],
    )
    runtime = Runtime(
        execution_info=ExecutionInfo(
            checkpoint_id="checkpoint",
            checkpoint_ns="",
            task_id="task-profile",
            thread_id="thread-profile",
        )
    )

    update = await supervisor_graph.nodes["tools"].ainvoke(
        {"messages": [message]},
        {"configurable": {"thread_id": "thread-profile"}},
        runtime=runtime,
    )

    assert update["messages"][0].status == "success"
    assert output_path.is_file()
