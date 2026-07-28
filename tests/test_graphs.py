from deep_data_research_agent.agent import graph as supervisor_graph
from deep_data_research_agent.crawl_worker import graph as worker_graph


def test_supervisor_exposes_async_subagent_tools_only() -> None:
    tools = supervisor_graph.nodes["tools"].bound.tools_by_name

    assert supervisor_graph.name == "supervisor"
    assert "start_async_task" in tools
    assert "check_async_task" in tools
    assert "task" not in tools


def test_crawl_worker_exposes_tavily_tools() -> None:
    tools = worker_graph.nodes["tools"].bound.tools_by_name

    assert worker_graph.name == "crawl-worker"
    assert {"tavily_search", "tavily_crawl", "tavily_extract"} <= set(tools)
    assert "task" not in tools
