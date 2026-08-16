import inspect
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from deep_data_research_agent.agent import graph as supervisor_graph
from deep_data_research_agent.crawl_worker import crawl_agent
from deep_data_research_agent.crawl_worker import graph as worker_graph
from deep_data_research_agent.prompts import (
    ANALYSIS_REVIEWER_PROMPT,
    DATA_ANALYST_PROMPT,
    SUPERVISOR_PROMPT,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_supervisor_exposes_sync_and_async_delegation_tools() -> None:
    tools = supervisor_graph.nodes["tools"].bound.tools_by_name

    assert supervisor_graph.name == "supervisor"
    assert "assign_skill" in tools
    assert "ask_user" in tools
    assert "request_report_download" in tools
    assert "send_report_email" in tools
    assert "capture_user_memory" in tools
    assert "record_failure_lesson" not in tools
    assert "start_async_task" in tools
    assert "check_async_task" in tools
    assert "task" in tools
    assert "data-analyst" in tools["task"].description
    assert "analysis-reviewer" in tools["task"].description
    assert "quick_answer" in tools["task"].description
    assert "formal_report" in tools["task"].description
    assert "一次委派内完成" in tools["task"].description
    assert "crawl-worker" in tools["start_async_task"].description
    assert not {
        "database_list_schemas",
        "database_list_objects",
        "database_get_object_details",
        "database_query_preview",
        "database_query_to_file",
    } & set(tools)


def test_crawl_worker_exposes_tavily_tools() -> None:
    tools = crawl_agent.nodes["tools"].bound.tools_by_name

    assert worker_graph.name == "crawl-worker"
    assert {
        "ensure_sandbox",
        "crawl_agent",
        "export_workspace",
        "build_result",
    } <= set(worker_graph.nodes)
    assert {"tavily_search", "tavily_crawl", "tavily_extract"} <= set(tools)
    assert "record_failure_lesson" not in tools
    assert "execute" in tools
    assert "task" not in tools


def test_data_analyst_inherits_deepagent_tools_and_only_adds_database_tools() -> None:
    task_tool = supervisor_graph.nodes["tools"].bound.tools_by_name["task"]
    child_graphs = inspect.getclosurevars(task_tool.coroutine).nonlocals[
        "subagent_graphs"
    ]
    data_analyst = child_graphs["data-analyst"]
    tools = data_analyst.nodes["tools"].bound.tools_by_name

    assert {
        "write_todos",
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "execute",
        "database_list_schemas",
        "database_list_objects",
        "database_get_object_details",
        "database_query_preview",
        "database_query_to_file",
    } <= set(tools)
    assert not {
        "task",
        "assign_skill",
        "ask_user",
        "request_report_download",
        "send_report_email",
        "tavily_search",
        "record_failure_lesson",
    } & set(tools)


def test_analysis_reviewer_is_a_read_only_workspace_specialist() -> None:
    task_tool = supervisor_graph.nodes["tools"].bound.tools_by_name["task"]
    child_graphs = inspect.getclosurevars(task_tool.coroutine).nonlocals[
        "subagent_graphs"
    ]
    reviewer = child_graphs["analysis-reviewer"]
    tools = reviewer.nodes["tools"].bound.tools_by_name

    # DeepAgents always installs its built-in filesystem and execute tools. The
    # reviewer receives no business tools, and filesystem writes are denied by
    # its own permission list.
    assert {"ls", "read_file", "glob", "grep"} <= set(tools)
    assert not {
        "task",
        "assign_skill",
        "ask_user",
        "request_report_download",
        "send_report_email",
        "tavily_search",
        "database_list_schemas",
        "database_query_preview",
        "capture_user_memory",
    } & set(tools)

    write_tool = tools["write_file"]
    filesystem_middleware = inspect.getclosurevars(write_tool.coroutine).nonlocals[
        "self"
    ]
    permissions = [vars(permission) for permission in filesystem_middleware._permissions]
    assert permissions == [
        {"operations": ["write"], "paths": ["/**"], "mode": "deny"},
        {
            "operations": ["read"],
            "paths": ["/workspace/**"],
            "mode": "allow",
        },
        {"operations": ["read"], "paths": ["/**"], "mode": "deny"},
    ]


def test_prompts_keep_supervisor_generic_and_data_analyst_contract_complete() -> None:
    for business_term in ("CSV", "XLSX", "PostgreSQL", "采购算法"):
        assert business_term not in SUPERVISOR_PROMPT
    for contract_field in (
        '"status"',
        '"summary"',
        '"findings"',
        '"artifacts"',
        '"warnings"',
        '"required_inputs"',
    ):
        assert contract_field in DATA_ANALYST_PROMPT
    assert "needs_input" in DATA_ANALYST_PROMPT
    assert "不直接\n  与用户交互" in DATA_ANALYST_PROMPT
    assert "quick_answer" in SUPERVISOR_PROMPT
    assert "formal_report" in SUPERVISOR_PROMPT
    assert "不得要求生成 Markdown" in SUPERVISOR_PROMPT
    assert "不得生成 PDF" in SUPERVISOR_PROMPT
    assert "quick_answer" in DATA_ANALYST_PROMPT
    assert "不生成 Markdown 主报告" in DATA_ANALYST_PROMPT
    assert "artifacts 返回空列表" in DATA_ANALYST_PROMPT
    assert "同一目标" in SUPERVISOR_PROMPT
    assert "只派发一个data-analyst" in SUPERVISOR_PROMPT
    assert "默认调用动态注入的报告转换 Skill" in SUPERVISOR_PROMPT
    assert "在报告同目录生成同名 PDF" in SUPERVISOR_PROMPT
    assert "不得仅完成探查、部分指标或某个报告章节" in DATA_ANALYST_PROMPT
    assert "相对于报告文件的路径嵌入" in DATA_ANALYST_PROMPT
    assert "capture_user_memory" in SUPERVISOR_PROMPT
    assert "record_failure_lesson" not in SUPERVISOR_PROMPT
    assert "明确要求通过邮件发送报告" in SUPERVISOR_PROMPT
    assert "不从记忆或历史收件人中推测" in SUPERVISOR_PROMPT
    assert "不得自行创建新的邮件工具调用" in SUPERVISOR_PROMPT
    assert "record_failure_lesson" not in DATA_ANALYST_PROMPT


def test_analysis_reviewer_prompt_and_supervisor_routing_are_bounded() -> None:
    for contract_field in (
        '"status"',
        '"summary"',
        '"issues"',
        '"severity"',
        '"category"',
        '"description"',
        '"evidence"',
        '"suggested_fix"',
        '"checked_artifacts"',
        '"warnings"',
    ):
        assert contract_field in ANALYSIS_REVIEWER_PROMPT

    assert "passed | revision_required | failed" in ANALYSIS_REVIEWER_PROMPT
    assert "最多返回 10 个" in ANALYSIS_REVIEWER_PROMPT
    assert "禁止调用 execute" in ANALYSIS_REVIEWER_PROMPT
    assert "不得写入、编辑或删除" in ANALYSIS_REVIEWER_PROMPT
    assert "不得连接数据库" in ANALYSIS_REVIEWER_PROMPT
    assert "不得采集网页" in ANALYSIS_REVIEWER_PROMPT
    assert "不得替 data-analyst" in ANALYSIS_REVIEWER_PROMPT

    assert "analysis-reviewer" in SUPERVISOR_PROMPT
    assert "自主判断是否调用" in SUPERVISOR_PROMPT
    assert "最多定向修订一次" in SUPERVISOR_PROMPT
    assert "修订后不得再次调用 analysis-reviewer" in SUPERVISOR_PROMPT
    assert "不得并行执行分析、审查或修订" in SUPERVISOR_PROMPT


@pytest.mark.asyncio
async def test_crawl_worker_builds_validated_artifact_result() -> None:
    # Invoke the node as compiled by LangGraph so argument-injection mistakes
    # cannot be hidden by manually supplying extra Python function arguments.
    update = await worker_graph.nodes["build_result"].ainvoke(
        {
            "messages": [
                AIMessage(
                    content=(
                        "status: success\n发现 3 个有效来源。\n"
                        "[示例来源](https://example.com/report)"
                    )
                )
            ],
            "exported_artifacts": [
                {"path": "/workspace/crawl_report.md", "size": 128},
                {"path": "/workspace/raw/page.md", "size": 64},
            ],
        },
        {},
    )

    result = json.loads(update["messages"][0].content)
    assert result["status"] == "success"
    assert result["artifacts"][0]["type"] == "report"
    assert result["artifacts"][1]["type"] == "source"
    assert result["sources"] == [
        {"title": "示例来源", "url": "https://example.com/report"}
    ]


def test_agents_enable_the_expected_memory_sources() -> None:
    supervisor_names = set(supervisor_graph.get_graph().nodes)
    worker_names = set(crawl_agent.get_graph().nodes)

    assert "MemoryRefreshMiddleware.before_agent" in supervisor_names
    assert "MemoryRefreshMiddleware.before_agent" in worker_names
    assert "FailureReviewMiddleware.after_agent" in supervisor_names
    assert "FailureReviewMiddleware.after_agent" in worker_names
    # The custom subclass replaces the duplicate memory= middleware and owns
    # both refresh and read-only prompt injection.
    assert "MemoryMiddleware.before_agent" not in supervisor_names
    assert "MemoryMiddleware.before_agent" not in worker_names

    task_tool = supervisor_graph.nodes["tools"].bound.tools_by_name["task"]
    child_graphs = inspect.getclosurevars(task_tool.coroutine).nonlocals[
        "subagent_graphs"
    ]
    data_analyst_names = set(child_graphs["data-analyst"].get_graph().nodes)
    assert "MemoryRefreshMiddleware.before_agent" in data_analyst_names
    assert "FailureReviewMiddleware.after_agent" in data_analyst_names

    reviewer_names = set(child_graphs["analysis-reviewer"].get_graph().nodes)
    assert "MemoryRefreshMiddleware.before_agent" not in reviewer_names
    assert "FailureReviewMiddleware.after_agent" not in reviewer_names
    assert "MongoSkillsRestoreMiddleware.before_agent" not in reviewer_names


def test_supervisor_sandbox_lifecycle_precedes_skill_loading() -> None:
    edges = {
        (edge.source, edge.target)
        for edge in supervisor_graph.get_graph().edges
    }

    assert (
        "SandboxLifecycleMiddleware.before_agent",
        "MemoryRefreshMiddleware.before_agent",
    ) in edges
    assert (
        "MemoryRefreshMiddleware.before_agent",
        "MongoSkillsRestoreMiddleware.before_agent",
    ) in edges
    assert (
        "MongoSkillsRestoreMiddleware.before_agent",
        "ReloadableSkillsMiddleware.before_agent",
    ) in edges
    assert ("ReloadableSkillsMiddleware.before_agent", "model") in edges


def test_only_supervisor_and_crawl_worker_are_public_graphs() -> None:
    config = json.loads((PROJECT_ROOT / "langgraph.json").read_text("utf-8"))

    assert set(config["graphs"]) == {"supervisor", "crawl-worker"}


def test_manage_skill_doc_has_flexible_direct_flow() -> None:
    path = (
        PROJECT_ROOT
        / "src"
        / "deep_data_research_agent"
        / "skills"
        / "supervisor"
        / "skill-manage"
        / "SKILL.md"
    )
    text = path.read_text("utf-8")

    assert len(text.splitlines()) <= 100
    assert "## 注意事项" in text
    for stage in ("阶段一", "阶段二", "阶段三", "阶段四"):
        assert stage in text
    for number in "①②③④":
        assert number in text
    assert "assign_skill" in text
    assert "data-analyst" in text
    assert "{{SKILL_ROOT}}" in text
