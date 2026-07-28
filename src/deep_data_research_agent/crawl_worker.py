"""ASGI co-deployed asynchronous crawl-worker graph."""

from deepagents import create_deep_agent

from deep_data_research_agent.backends import (
    FILESYSTEM_PERMISSIONS,
    create_backend,
)
from deep_data_research_agent.config import create_chat_model
from deep_data_research_agent.model_profile import register_mvp_profile
from deep_data_research_agent.prompts import CRAWL_WORKER_PROMPT
from deep_data_research_agent.tavily_tools import CRAWL_TOOLS

register_mvp_profile()

graph = create_deep_agent(
    name="crawl-worker",
    model=create_chat_model(worker=True),
    tools=CRAWL_TOOLS,
    system_prompt=CRAWL_WORKER_PROMPT,
    skills=["/skills/worker/"],
    backend=create_backend,
    permissions=FILESYSTEM_PERMISSIONS,
)
