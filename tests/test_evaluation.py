import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from deep_data_research_agent.database import repository as database
from deep_data_research_agent.database.models import Base
from deep_data_research_agent.evaluation import runner as evaluation


async def _noop_schema_check(**_kwargs) -> None:
    return None


def test_manifest_has_expected_twenty_cases_in_order() -> None:
    manifest = evaluation.load_manifest()

    assert [case.id for case in manifest.cases] == [
        "Q01", "Q02", "Q03", "F01", "F02", "F03", "F04", "F05", "F06",
        "D01", "D02", "D03", "D04", "W01", "W02", "X01", "X02", "X03",
        "S01", "S02",
    ]
    assert sum(case.category == "diagnostic" for case in manifest.cases) == 3
    assert sum(case.report_required for case in manifest.cases) == 12
    assert sum(case.email_approval_expected for case in manifest.cases) == 4


def test_redact_removes_nested_secrets_and_bearer_values() -> None:
    value = {
        "Authorization": "Bearer visible-token",
        "nested": {"api_key": "secret-value", "safe": "token=still-secret"},
    }

    redacted = evaluation._redact(value)

    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["nested"]["api_key"] == "[REDACTED]"
    assert "still-secret" not in redacted["nested"]["safe"]


def test_interrupt_actions_accepts_args_and_arguments() -> None:
    state = {
        "interrupts": [
            {
                "value": {
                    "action_requests": [
                        {"name": "ask_user", "arguments": {"question": "规格？"}},
                        {"name": "send_report_email", "args": {"recipient": "eval@example.com"}},
                    ]
                }
            }
        ]
    }

    actions = evaluation._interrupt_actions(state)

    assert actions[0]["args"] == {"question": "规格？"}
    assert actions[1]["args"]["recipient"] == "eval@example.com"


def test_validate_report_checks_pdf_and_relative_images(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    chart = artifact_dir / "charts" / "plot.png"
    chart.parent.mkdir(parents=True)
    chart.write_bytes(b"png")
    (artifact_dir / "final_report.md").write_text(
        "# 报告\n\n" + "有效分析内容。" * 50 + "\n\n![图](charts/plot.png)\n",
        encoding="utf-8",
    )
    (artifact_dir / "final_report.pdf").write_bytes(b"%PDF-1.7\nbody")

    valid, warnings, report = evaluation._validate_report(tmp_path)

    assert valid is True
    assert warnings == []
    assert "有效分析内容" in report


def test_hard_success_uses_exact_simple_gold() -> None:
    case = evaluation.load_manifest().cases[0]

    success, reasons = evaluation._hard_success(
        case,
        final_response="最多可以买 23 件，剩余 360 元。",
        report_valid=False,
        actions=[],
        email_approval_valid=False,
        trace=[],
        async_task_ids=set(),
        user_hash="a" * 64,
    )

    assert success is True
    assert reasons == []


def test_summary_excludes_diagnostics_from_core_rate() -> None:
    results = [
        {
            "category": "simple", "task_success": True, "llm_call_count": 1,
            "supervisor_ttft_ms": 100, "e2e_ms": 200, "input_tokens": 10,
            "output_tokens": 2, "total_tokens": 12, "cache_read_tokens": 5,
            "tool_selection_accuracy": 1.0, "tool_argument_accuracy": 1.0,
            "report_quality_score": None,
            "artifact_export_ms": 20,
            "trace_export_ms": 30,
        },
        {
            "category": "diagnostic", "task_success": False, "llm_call_count": 2,
            "supervisor_ttft_ms": None, "e2e_ms": 300, "input_tokens": 20,
            "output_tokens": 3, "total_tokens": 23, "cache_read_tokens": 10,
            "tool_selection_accuracy": 0.5, "tool_argument_accuracy": 0.5,
            "report_quality_score": None,
            "artifact_export_ms": 40,
            "trace_export_ms": 50,
        },
    ]

    summary = evaluation._build_summary(
        results,
        {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )

    assert summary["core_success_rate"] == 1.0
    assert summary["diagnostic_success_rate"] == 0.0
    assert summary["cache_hit_rate"] == 0.5
    assert summary["llm_calls_total"] == 3
    assert summary["artifact_export_ms_total"] == 60
    assert summary["artifact_export_ms_mean"] == 30
    assert summary["trace_export_ms_total"] == 80
    assert summary["trace_export_ms_mean"] == 40


def test_extract_json_object_handles_code_fence() -> None:
    parsed = evaluation._extract_json_object("```json\n{\"tool_selection_accuracy\": 1}\n```")
    assert parsed == {"tool_selection_accuracy": 1}


def _fake_trace_run(
    run_id: str,
    run_type: str,
    started_at: datetime,
    *,
    agent_name: str,
    graph_id: str = "supervisor",
    inputs=None,
    outputs=None,
):
    return SimpleNamespace(
        id=run_id,
        trace_id=f"trace-{run_id}",
        parent_run_id=None,
        name=agent_name,
        run_type=run_type,
        start_time=started_at,
        end_time=started_at + timedelta(seconds=1),
        first_token_time=started_at + timedelta(milliseconds=250),
        status="success",
        error=None,
        prompt_tokens=100 if run_type == "llm" else 0,
        completion_tokens=20 if run_type == "llm" else 0,
        total_tokens=120 if run_type == "llm" else 0,
        prompt_token_details={"cache_read": 40} if run_type == "llm" else {},
        completion_token_details={},
        tags=[],
        extra={
            "metadata": {
                "eval_run_id": "eval-run",
                "eval_case_id": "Q01",
                "lc_agent_name": agent_name,
                "graph_id": graph_id,
                "ignored": "must-not-be-exported",
            }
        },
        inputs=inputs,
        outputs=outputs,
    )


@pytest.mark.asyncio
async def test_trace_export_uses_server_filters_and_minimal_records(tmp_path: Path) -> None:
    started_at = datetime.now(UTC)
    root = _fake_trace_run("root", "chain", started_at, agent_name="supervisor")
    llm = _fake_trace_run("llm", "llm", started_at, agent_name="supervisor")
    tool = _fake_trace_run(
        "tool",
        "tool",
        started_at,
        agent_name="data-analyst",
        inputs={"path": "/workspace/file.csv", "token": "secret"},
        outputs={"text": "x" * 8_000},
    )

    class FakeLangSmith:
        def __init__(self):
            self.calls = []

        def list_runs(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("is_root"):
                return iter([root])
            if kwargs.get("run_type") == "llm":
                return iter([llm])
            if kwargs.get("run_type") == "tool":
                return iter([tool])
            raise AssertionError("unexpected unfiltered trace query")

    client = FakeLangSmith()
    trace, metrics = await evaluation._collect_trace(
        client,
        "project",
        "eval-run",
        "Q01",
        started_at,
        tmp_path,
    )

    assert len(client.calls) == 3
    assert {call.get("run_type") for call in client.calls} == {None, "llm", "tool"}
    assert all("eval_run_id" in call["filter"] for call in client.calls)
    assert all("eval_case_id" in call["filter"] for call in client.calls)
    assert all("limit" not in call for call in client.calls)
    assert [item["record_kind"] for item in trace] == ["root", "llm", "tool"]
    assert "inputs" not in trace[0] and "outputs" not in trace[0]
    assert "inputs" not in trace[1] and "outputs" not in trace[1]
    assert trace[2]["inputs"]["token"] == "[REDACTED]"
    assert trace[2]["outputs"]["truncated"] is True
    assert "ignored" not in trace[0]["metadata"]
    assert metrics["llm_call_count"] == 1
    assert metrics["supervisor_ttft_ms"] == 250
    assert metrics["cache_hit_rate"] == 0.4


@pytest.mark.asyncio
async def test_e2e_freezes_before_slow_artifact_and_trace_exports(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clock = {"value": 100.0}

    class Threads:
        async def create(self, **_kwargs):
            return {"thread_id": "thread"}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"tasks": []}

    class Http:
        async def post(self, *_args, **_kwargs):
            return Response()

    async def submit(*_args, **_kwargs):
        clock["value"] += 2
        state = {
            "values": {
                "messages": [AIMessage(content="最多可以买 23 件，剩余 360 元。")]
            }
        }
        return None, state, None

    async def export_artifacts(*_args, **_kwargs):
        clock["value"] += 5

    async def collect_trace(*_args, **_kwargs):
        clock["value"] += 7
        return [], {
            "llm_call_count": 1,
            "llm_component_counts": {"supervisor": 1},
            "supervisor_ttft_ms": 100,
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "cache_read_tokens": 0,
            "cache_hit_rate": 0,
            "tool_calls": [],
            "trace_run_count": 0,
        }

    monkeypatch.setattr(evaluation.time, "monotonic", lambda: clock["value"])
    monkeypatch.setattr(evaluation, "_submit_run", submit)
    monkeypatch.setattr(evaluation, "_export_artifacts", export_artifacts)
    monkeypatch.setattr(evaluation, "_collect_trace", collect_trace)
    case = evaluation.load_manifest().cases[0]

    result = await evaluation._run_case(
        case,
        run_id="eval-run",
        user_hash="a" * 64,
        graph_client=SimpleNamespace(threads=Threads()),
        http=Http(),
        langsmith=SimpleNamespace(),
        project="project",
        data_root=tmp_path,
        run_dir=tmp_path,
    )

    assert result["e2e_ms"] == 2_000
    assert result["artifact_export_ms"] == 5_000
    assert result["trace_export_ms"] == 7_000
    assert result["task_success"] is True


@pytest.mark.parametrize(
    ("mode", "case_id", "expected_calls"),
    [
        ("ask_user", "X01", 1),
        ("email_reject", "F02", 2),
        ("async", "W01", 2),
        ("timeout", "Q01", 1),
    ],
)
@pytest.mark.asyncio
async def test_e2e_terminal_boundaries(
    monkeypatch,
    tmp_path: Path,
    mode: str,
    case_id: str,
    expected_calls: int,
) -> None:
    clock = {"value": 10.0}
    submit_calls: list[dict] = []
    case = next(item for item in evaluation.load_manifest().cases if item.id == case_id)
    for filename in case.input_files:
        (tmp_path / filename).write_text("fixture", encoding="utf-8")

    class Threads:
        async def create(self, **_kwargs):
            return {"thread_id": "thread"}

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Http:
        async def post(self, path, **_kwargs):
            if path.startswith("/files/"):
                return Response({"files": [{"path": "/workspace/input.csv"}]})
            tasks = []
            if mode == "async":
                tasks = [
                    {
                        "task_id": "child-thread",
                        "thread_id": "child-thread",
                        "status": "success",
                    }
                ]
            return Response({"tasks": tasks})

    async def submit(*_args, **kwargs):
        submit_calls.append(kwargs)
        clock["value"] += 1
        if mode == "timeout":
            raise TimeoutError("case timeout")
        state = {"values": {"messages": [AIMessage(content="最终回复")]}}
        if mode == "ask_user":
            state["interrupts"] = [
                {
                    "value": {
                        "action_requests": [
                            {"name": "ask_user", "args": {"question": "请补充规格"}}
                        ]
                    }
                }
            ]
        elif mode == "email_reject" and len(submit_calls) == 1:
            state["interrupts"] = [
                {
                    "value": {
                        "action_requests": [
                            {
                                "name": "send_report_email",
                                "args": {
                                    "recipient": "eval@example.com",
                                    "pdf_path": "/workspace/output/report.pdf",
                                    "markdown_path": "/workspace/output/report.md",
                                },
                            }
                        ]
                    }
                }
            ]
        return None, state, None

    async def poll_async(*_args, **_kwargs):
        return [{"task_id": "child-thread", "status": "success"}]

    async def export_artifacts(*_args, **_kwargs):
        return None

    async def collect_trace(*_args, **_kwargs):
        return [], {
            "llm_call_count": 1,
            "llm_component_counts": {"supervisor": 1},
            "supervisor_ttft_ms": 100,
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "cache_read_tokens": 0,
            "cache_hit_rate": 0,
            "tool_calls": [],
            "trace_run_count": 0,
        }

    monkeypatch.setattr(evaluation.time, "monotonic", lambda: clock["value"])
    monkeypatch.setattr(evaluation, "_submit_run", submit)
    monkeypatch.setattr(evaluation, "_poll_async_tasks", poll_async)
    monkeypatch.setattr(evaluation, "_export_artifacts", export_artifacts)
    monkeypatch.setattr(evaluation, "_collect_trace", collect_trace)
    monkeypatch.setattr(evaluation, "_validate_report", lambda _path: (True, [], "report"))

    result = await evaluation._run_case(
        case,
        run_id="eval-run",
        user_hash="a" * 64,
        graph_client=SimpleNamespace(threads=Threads()),
        http=Http(),
        langsmith=SimpleNamespace(),
        project="project",
        data_root=tmp_path,
        run_dir=tmp_path,
    )

    assert len(submit_calls) == expected_calls
    assert result["e2e_ms"] == expected_calls * 1_000
    if mode == "ask_user":
        assert result["interrupt_actions"][0]["name"] == "ask_user"
    elif mode == "email_reject":
        assert result["email_approval_valid"] is True
        assert result["time_to_email_approval_ms"] == 1_000
        assert submit_calls[1]["command"]["resume"]["decisions"][0]["type"] == "reject"
    elif mode == "async":
        assert result["async_task_ids"] == ["child-thread"]
        assert "check_async_task" in submit_calls[1]["message"]
    else:
        assert result["infrastructure_error"] == "case timeout"


@pytest_asyncio.fixture
async def isolated_database(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_session_factory", factory)
    monkeypatch.setattr(database, "_initialized", False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(database, "_validate_deployed_schema", _noop_schema_check)
    await database.ensure_schema()
    try:
        yield
    finally:
        await database.close_database()


@pytest.mark.asyncio
async def test_delete_user_removes_dependents_and_rejects_system_account(isolated_database) -> None:
    user = await database.create_user("eval-user", "hash")
    await database.claim_thread("thread-a", user.id)
    await database.create_login_session(user.id)

    assert await database.list_user_thread_ids(user.id) == ["thread-a"]
    assert await database.delete_user(user.id) is True
    assert await database.get_user_by_id(user.id) is None
    assert await database.list_user_thread_ids(user.id) == []
    with pytest.raises(ValueError, match="系统账户"):
        await database.delete_user(database.DEFAULT_USER_ID)


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RUN_EVAL_INTEGRATION") != "1", reason="requires live services")
def test_live_q01_registers_and_destroys_account(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "deep_data_research_agent.evaluation.runner",
            "--case",
            "Q01",
            "--output-root",
            str(tmp_path),
        ],
        cwd=evaluation.REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipts = list(tmp_path.glob("*/cleanup_receipt.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text(encoding="utf-8"))["cleanup_verified"] is True
    run_dir = receipts[0].parent
    result = json.loads((run_dir / "cases" / "Q01" / "result.json").read_text(encoding="utf-8"))
    trace = json.loads((run_dir / "cases" / "Q01" / "trace.json").read_text(encoding="utf-8"))
    assert result["task_success"] is True
    assert result["llm_call_count"] >= 1
    assert trace
