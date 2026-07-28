"""Supervisor DeepAgent graph exposed through the LangGraph Agent Server."""

from deepagents import AsyncSubAgent, create_deep_agent
from deepagents.middleware.async_subagents import AsyncSubAgentMiddleware

from deep_data_research_agent.backends import (
    FILESYSTEM_PERMISSIONS,
    create_backend,
)
from deep_data_research_agent.config import create_chat_model
from deep_data_research_agent.model_profile import register_mvp_profile
from deep_data_research_agent.prompts import ASYNC_SUBAGENT_PROMPT, SUPERVISOR_PROMPT

register_mvp_profile()

graph = create_deep_agent(
    name="supervisor",
    model=create_chat_model(),
    system_prompt=SUPERVISOR_PROMPT,
    middleware=[
        AsyncSubAgentMiddleware(
            system_prompt=ASYNC_SUBAGENT_PROMPT,
            async_subagents=[
                AsyncSubAgent(
                    name="crawl-worker",
                    description=(
                        "使用 Tavily 搜索、爬取或提取公开网页，并返回带 URL 来源的"
                        "采集结果和初步分析。所有网页任务都应委派给它。"
                    ),
                    graph_id="crawl-worker",
                    # 不设置 url，使用同一部署内的 ASGI transport。
                )
            ],
        )
    ],
    skills=["/skills/supervisor/"],
    backend=create_backend,
    permissions=FILESYSTEM_PERMISSIONS,
)
