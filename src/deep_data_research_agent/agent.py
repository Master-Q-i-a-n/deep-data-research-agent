"""Supervisor DeepAgent graph exposed through the LangGraph Agent Server."""

from deepagents import AsyncSubAgent, create_deep_agent
from deepagents.middleware.async_subagents import AsyncSubAgentMiddleware

from deep_data_research_agent.backends import (
    FILESYSTEM_PERMISSIONS,
    create_backend,
)
from deep_data_research_agent.config import create_chat_model
from deep_data_research_agent.memory import (
    AGENT_MEMORY_PATHS,
    USER_PREFERENCES_PATH,
    AgentExperienceEnqueueMiddleware,
    AsyncTaskPreferenceForwardingMiddleware,
    MemoryRefreshMiddleware,
    UserPreferenceUpdateMiddleware,
)
from deep_data_research_agent.model_profile import register_mvp_profile
from deep_data_research_agent.prompts import ASYNC_SUBAGENT_PROMPT, SUPERVISOR_PROMPT
from deep_data_research_agent.skill_middleware import (
    ReloadableSkillsMiddleware,
    SandboxLifecycleMiddleware,
    SkillsSyncMiddleware,
    SkillToolErrorMiddleware,
    UserSkillsRestoreMiddleware,
)
from deep_data_research_agent.skill_tools import ASSIGN_SKILL_TOOL

register_mvp_profile()

graph = create_deep_agent(
    name="supervisor",
    model=create_chat_model(),
    system_prompt=SUPERVISOR_PROMPT,
    tools=ASSIGN_SKILL_TOOL,
    middleware=[
        SandboxLifecycleMiddleware(
            component="supervisor",
            # Skill 的下载与依赖测试需要联网，supervisor 沙箱打开网络。
            network_enabled=True,
        ),
        # Directly configure DeepAgents' memory middleware with a read-only
        # prompt, while refreshing checkpoint-cached contents every run.
        MemoryRefreshMiddleware(
            backend_factory=create_backend,
            sources=[AGENT_MEMORY_PATHS["supervisor"], USER_PREFERENCES_PATH],
            initialize_preferences=True,
        ),
        SkillsSyncMiddleware(
            component="supervisor",
            scope="supervisor",
        ),
        UserSkillsRestoreMiddleware(
            component="supervisor",
            agent_name="supervisor",
        ),
        SkillToolErrorMiddleware(
            tool_names={tool.name for tool in ASSIGN_SKILL_TOOL},
        ),
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
                ),
            ],
        ),
        AsyncTaskPreferenceForwardingMiddleware(),
        ReloadableSkillsMiddleware(
            backend=create_backend,
            sources=[
                ("/skills/supervisor/", "内置"),
                ("/persisted-skills/active/", "用户"),
            ],
        ),
        AgentExperienceEnqueueMiddleware(agent_name="supervisor"),
        UserPreferenceUpdateMiddleware(),
    ],
    backend=create_backend,
    permissions=FILESYSTEM_PERMISSIONS,
)
