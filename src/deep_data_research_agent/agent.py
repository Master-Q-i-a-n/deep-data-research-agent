"""Supervisor DeepAgent graph exposed through the LangGraph Agent Server."""

from deepagents import AsyncSubAgent, SubAgent, create_deep_agent
from deepagents.middleware.async_subagents import AsyncSubAgentMiddleware

from deep_data_research_agent.backends import (
    FILESYSTEM_PERMISSIONS,
    create_backend,
)
from deep_data_research_agent.config import create_chat_model
from deep_data_research_agent.database_tools import DATABASE_TOOLS
from deep_data_research_agent.interaction_tools import INTERACTION_TOOLS
from deep_data_research_agent.memory import (
    DATA_ANALYST_FAILURE_TOOL,
    SUPERVISOR_MEMORY_TOOLS,
    USER_MEMORY_PATH,
    AsyncTaskBridgeMiddleware,
    MemoryRefreshMiddleware,
    agent_memory_path,
)
from deep_data_research_agent.model_profile import register_mvp_profile
from deep_data_research_agent.prompts import (
    ASYNC_SUBAGENT_PROMPT,
    DATA_ANALYST_PROMPT,
    SUPERVISOR_PROMPT,
)
from deep_data_research_agent.skill_middleware import (
    MongoSkillsRestoreMiddleware,
    ReloadableSkillsMiddleware,
    SandboxLifecycleMiddleware,
    SkillToolErrorMiddleware,
)
from deep_data_research_agent.skill_storage import public_skill_root, user_skill_root
from deep_data_research_agent.skill_tools import ASSIGN_SKILL_TOOL

register_mvp_profile()

graph = create_deep_agent(
    name="supervisor",
    model=create_chat_model(),
    system_prompt=SUPERVISOR_PROMPT,
    tools=[*ASSIGN_SKILL_TOOL, *INTERACTION_TOOLS, *SUPERVISOR_MEMORY_TOOLS],
    subagents=[
        SubAgent(
            name="data-analyst",
            description=(
                "对本地 CSV、TSV、XLSX 文件或 PostgreSQL 只读数据执行端到端分析；"
                "一次委派内完成结构探查、指标计算、验证、制图和 Markdown 主报告，"
                "主报告以相对路径嵌入全部生成图表，并生成可核验的 Markdown、CSV、JSON、"
                "PNG 产物；信息不足时返回 needs_input。"
            ),
            system_prompt=DATA_ANALYST_PROMPT,
            tools=[*DATABASE_TOOLS, DATA_ANALYST_FAILURE_TOOL],
            skills=[
                f"{public_skill_root('data-analyst')}/",
                f"{user_skill_root('data-analyst')}/",
            ],
            middleware=[
                MemoryRefreshMiddleware(
                    backend_factory=create_backend,
                    sources=[USER_MEMORY_PATH, agent_memory_path("data-analyst")],
                ),
                MongoSkillsRestoreMiddleware(
                    component="supervisor",
                    agent_name="data-analyst",
                )
            ],
        )
    ],
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
            sources=[USER_MEMORY_PATH, agent_memory_path("supervisor")],
        ),
        MongoSkillsRestoreMiddleware(
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
        AsyncTaskBridgeMiddleware(),
        ReloadableSkillsMiddleware(
            backend=create_backend,
            sources=[
                (f"{public_skill_root('supervisor')}/", "公共"),
                (f"{user_skill_root('supervisor')}/", "用户"),
            ],
        ),
    ],
    backend=create_backend,
    permissions=FILESYSTEM_PERMISSIONS,
    interrupt_on={
        "ask_user": {"allowed_decisions": ["respond"]},
        "request_report_download": {
            "allowed_decisions": ["approve", "reject"],
        },
    },
)
