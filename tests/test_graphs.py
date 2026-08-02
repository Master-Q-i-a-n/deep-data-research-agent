import json
from pathlib import Path

from deep_data_research_agent.agent import graph as supervisor_graph
from deep_data_research_agent.crawl_worker import crawl_agent
from deep_data_research_agent.crawl_worker import graph as worker_graph

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_supervisor_exposes_assign_skill_and_async_crawl_tools() -> None:
    tools = supervisor_graph.nodes["tools"].bound.tools_by_name

    assert supervisor_graph.name == "supervisor"
    assert "assign_skill" in tools
    assert "start_async_task" in tools
    assert "check_async_task" in tools
    assert "task" not in tools
    assert "crawl-worker" in tools["start_async_task"].description


def test_crawl_worker_exposes_tavily_tools() -> None:
    tools = crawl_agent.nodes["tools"].bound.tools_by_name

    assert worker_graph.name == "crawl-worker"
    assert {
        "ensure_sandbox",
        "crawl_agent",
        "export_workspace",
    } <= set(worker_graph.nodes)
    assert {"tavily_search", "tavily_crawl", "tavily_extract"} <= set(tools)
    assert "execute" in tools
    assert "task" not in tools


def test_agents_enable_the_expected_memory_sources() -> None:
    supervisor_names = set(supervisor_graph.get_graph().nodes)
    worker_names = set(crawl_agent.get_graph().nodes)

    assert "MemoryRefreshMiddleware.before_agent" in supervisor_names
    assert "MemoryRefreshMiddleware.before_agent" in worker_names
    # The custom subclass replaces the duplicate memory= middleware and owns
    # both refresh and read-only prompt injection.
    assert "MemoryMiddleware.before_agent" not in supervisor_names
    assert "MemoryMiddleware.before_agent" not in worker_names


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
        "SkillsSyncMiddleware.before_agent",
    ) in edges
    assert (
        "SkillsSyncMiddleware.before_agent",
        "UserSkillsRestoreMiddleware.before_agent",
    ) in edges
    assert (
        "UserSkillsRestoreMiddleware.before_agent",
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
    assert "子智能体" not in text
    assert "assign_skill" in text
