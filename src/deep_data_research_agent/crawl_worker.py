"""ASGI co-deployed asynchronous crawl-worker graph."""

from typing import Any

from deepagents import DeepAgentState, create_deep_agent
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from deep_data_research_agent import sandbox_manager
from deep_data_research_agent.backends import (
    WORKER_FILESYSTEM_PERMISSIONS,
    create_worker_backend,
)
from deep_data_research_agent.config import create_chat_model
from deep_data_research_agent.identity import user_identity_from_config
from deep_data_research_agent.model_profile import register_mvp_profile
from deep_data_research_agent.prompts import CRAWL_WORKER_PROMPT
from deep_data_research_agent.skill_middleware import (
    ReloadableSkillsMiddleware,
    SkillsSyncMiddleware,
    UserSkillsRestoreMiddleware,
)
from deep_data_research_agent.tavily_tools import CRAWL_TOOLS

register_mvp_profile()

crawl_agent = create_deep_agent(
    name="crawl-worker-agent",
    model=create_chat_model(worker=True),
    tools=CRAWL_TOOLS,
    system_prompt=CRAWL_WORKER_PROMPT,
    middleware=[
        SkillsSyncMiddleware(
            component="crawl-worker",
            scope="worker",
        ),
        UserSkillsRestoreMiddleware(
            component="crawl-worker",
            agent_name="crawl-worker",
        ),
        ReloadableSkillsMiddleware(
            backend=create_worker_backend,
            sources=[
                ("/skills/worker/", "内置"),
                ("/persisted-skills/active/", "用户"),
            ],
        )
    ],
    backend=create_worker_backend,
    permissions=WORKER_FILESYSTEM_PERMISSIONS,
)


async def _ensure_sandbox(
    _state: DeepAgentState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Initialize the task sandbox before DeepAgents middleware resolves it."""

    thread_id = sandbox_manager.thread_id_from_config(config)
    await sandbox_manager.SANDBOX_MANAGER.ensure(
        thread_id,
        user_id=user_identity_from_config(config),
    )
    return {}


async def _export_workspace(
    _state: DeepAgentState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Persist the successful sandbox workspace for frontend and later runs."""

    thread_id = sandbox_manager.thread_id_from_config(config)
    await sandbox_manager.SANDBOX_MANAGER.export_workspace(thread_id)
    return {}


builder = StateGraph(DeepAgentState)
builder.add_node("ensure_sandbox", _ensure_sandbox)
builder.add_node("crawl_agent", crawl_agent)
builder.add_node("export_workspace", _export_workspace)
builder.add_edge(START, "ensure_sandbox")
builder.add_edge("ensure_sandbox", "crawl_agent")
builder.add_edge("crawl_agent", "export_workspace")
builder.add_edge("export_workspace", END)

graph = builder.compile(name="crawl-worker")
