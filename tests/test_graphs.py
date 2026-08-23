import inspect
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from deep_data_research_agent.agents.crawl_worker import crawl_agent
from deep_data_research_agent.agents.crawl_worker import graph as worker_graph
from deep_data_research_agent.agents.model_profile import (
    DEFAULT_EXCLUDED_TOOLS,
    REVIEWER_EXCLUDED_TOOLS,
)
from deep_data_research_agent.agents.prompts import (
    ANALYSIS_REVIEWER_PROMPT,
    DATA_ANALYST_PROMPT,
    SUPERVISOR_PROMPT,
)
from deep_data_research_agent.agents.supervisor import graph as supervisor_graph

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_supervisor_exposes_sync_and_async_delegation_tools() -> None:
    tools = supervisor_graph.nodes["tools"].bound.tools_by_name

    assert supervisor_graph.name == "supervisor"
    assert "assign_skill" in tools
    assert "validate_report_artifacts" not in tools
    assert "ask_user" in tools
    assert "request_report_download" in tools
    assert "send_report_email" in tools
    assert "capture_user_memory" in tools
    assert "record_failure_lesson" not in tools
    assert "start_async_task" in tools
    assert "check_async_task" in tools
    assert "task" in tools
    assert "write_todos" in tools
    # DeepAgents 0.7 keeps built-in tools in the graph registry and applies
    # HarnessProfile.excluded_tools immediately before each model request.
    assert DEFAULT_EXCLUDED_TOOLS == {"delete"}
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
    assert "write_todos" in tools
    assert DEFAULT_EXCLUDED_TOOLS == {"delete"}
    assert "task" not in tools


def test_data_analyst_inherits_deepagent_tools_and_only_adds_database_tools() -> None:
    task_tool = supervisor_graph.nodes["tools"].bound.tools_by_name["task"]
    child_graphs = inspect.getclosurevars(task_tool.coroutine).nonlocals[
        "subagent_graphs"
    ]
    data_analyst = child_graphs["data-analyst"]
    tools = data_analyst.nodes["tools"].bound.tools_by_name

    # Data analysis may legitimately require many model/tool turns; only the
    # Reviewer keeps a hard per-delegation model-call cap.
    assert "SubagentModelCallLimitMiddleware.before_model" not in data_analyst.nodes
    assert "SubagentModelCallLimitMiddleware.after_model" not in data_analyst.nodes
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
    assert DEFAULT_EXCLUDED_TOOLS == {"delete"}
    assert not {
        "task",
        "assign_skill",
        "ask_user",
        "request_report_download",
        "send_report_email",
        "tavily_search",
        "record_failure_lesson",
    } & set(tools)

    model_closure = inspect.getclosurevars(data_analyst.nodes["model"].bound.func).nonlocals
    assert model_closure["initial_response_format"] is None


def test_analysis_reviewer_is_a_read_only_workspace_specialist() -> None:
    task_tool = supervisor_graph.nodes["tools"].bound.tools_by_name["task"]
    child_graphs = inspect.getclosurevars(task_tool.coroutine).nonlocals[
        "subagent_graphs"
    ]
    reviewer = child_graphs["analysis-reviewer"]
    tools = reviewer.nodes["tools"].bound.tools_by_name

    # DeepAgents 0.7 makes Todo opt-in, so Reviewer never registers it. Execute
    # stays registered and a role-aware middleware hides it from non-numeric
    # model requests.
    assert {"ls", "read_file", "glob", "grep", "execute"} <= set(tools)
    assert "write_todos" not in tools
    assert REVIEWER_EXCLUDED_TOOLS == {
        "delete",
        "write_file",
        "edit_file",
        "write_todos",
    }
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

    model_closure = inspect.getclosurevars(reviewer.nodes["model"].bound.func).nonlocals
    assert model_closure["initial_response_format"] is None


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
    assert "完整产物自检" in DATA_ANALYST_PROMPT
    assert "可正常解码" in DATA_ANALYST_PROMPT
    assert "结构化输出能够读取" in DATA_ANALYST_PROMPT
    assert "核心结果的脚本必须作为 artifacts" in DATA_ANALYST_PROMPT
    assert "Reviewer 要求的分析修订完成后" in DATA_ANALYST_PROMPT
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

    assert "唯一有效的最终输出合约" in ANALYSIS_REVIEWER_PROMPT
    assert "最多返回 10 个" in ANALYSIS_REVIEWER_PROMPT
    assert "不使用 write_todos" in ANALYSIS_REVIEWER_PROMPT
    assert "只有 numeric_consistency 额外看到只读 execute" in ANALYSIS_REVIEWER_PROMPT
    assert "methodology_validity" in ANALYSIS_REVIEWER_PROMPT
    assert "evidence_and_limitations" in ANALYSIS_REVIEWER_PROMPT
    assert "不得写入、编辑或删除" in ANALYSIS_REVIEWER_PROMPT
    assert "不得连接数据库" in ANALYSIS_REVIEWER_PROMPT
    assert "不得采集网页" in ANALYSIS_REVIEWER_PROMPT
    assert "不得替 data-analyst" in ANALYSIS_REVIEWER_PROMPT
    assert "最多进行 3 次证据核验" in ANALYSIS_REVIEWER_PROMPT
    assert "立即停止当前" in ANALYSIS_REVIEWER_PROMPT
    assert "直接返回 revision_required 和" in ANALYSIS_REVIEWER_PROMPT
    assert "offset=0, limit=1000" in ANALYSIS_REVIEWER_PROMPT
    assert "offset + limit" in ANALYSIS_REVIEWER_PROMPT
    assert "禁止重叠、回退" in ANALYSIS_REVIEWER_PROMPT
    assert "30 次实际工具调用" in ANALYSIS_REVIEWER_PROMPT
    assert "只能包含一个 JSON 对象" in ANALYSIS_REVIEWER_PROMPT
    assert "不检查文件存在性" in ANALYSIS_REVIEWER_PROMPT
    assert "不得扫描目录" in ANALYSIS_REVIEWER_PROMPT
    assert "artifact_integrity" not in ANALYSIS_REVIEWER_PROMPT

    assert "analysis-reviewer" in SUPERVISOR_PROMPT
    assert "validate_report_artifacts" not in SUPERVISOR_PROMPT
    assert "确定性工具职责与触发条件" not in SUPERVISOR_PROMPT
    assert "委派 data-analyst 修订一次" in SUPERVISOR_PROMPT
    assert "修订后不得再次调用 Reviewer" in SUPERVISOR_PROMPT
    assert "默认不调用 Reviewer" in SUPERVISOR_PROMPT
    assert "用户在当前请求中明确要求" in SUPERVISOR_PROMPT
    assert "不得根据任务复杂度" in SUPERVISOR_PROMPT
    assert "一般质量要求也不视为明确的审查请求" in SUPERVISOR_PROMPT
    assert "固定并发调用 3 个独立 Reviewer" in SUPERVISOR_PROMPT
    assert "artifact_integrity" not in SUPERVISOR_PROMPT
    assert "numeric_consistency" in SUPERVISOR_PROMPT
    assert "methodology_validity" in SUPERVISOR_PROMPT
    assert "evidence_and_limitations" in SUPERVISOR_PROMPT
    assert "模型训练" in SUPERVISOR_PROMPT
    assert "多表关联" in SUPERVISOR_PROMPT
    assert "统计推断" in SUPERVISOR_PROMPT
    assert "任一结果为 analysis_revision" in SUPERVISOR_PROMPT
    assert "直接编辑主 Markdown" in SUPERVISOR_PROMPT
    assert "high 优先且合计" in SUPERVISOR_PROMPT
    assert "同一报告不得并行执行分析或修订" in SUPERVISOR_PROMPT
    assert "三个分工明确的只读 Reviewer 审查可以并发" in SUPERVISOR_PROMPT
    assert "不检查文件存在" in SUPERVISOR_PROMPT
    assert "`【返回格式】`" in SUPERVISOR_PROMPT
    assert "字段定义或 JSON 示例" in SUPERVISOR_PROMPT


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
