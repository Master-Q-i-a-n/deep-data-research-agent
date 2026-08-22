"""Supervisor DeepAgent graph exposed through the LangGraph Agent Server."""

from deepagents import AsyncSubAgent, SubAgent, create_deep_agent
from langchain.agents.middleware import TodoListMiddleware

from deep_data_research_agent.async_subagents import (
    MetadataPropagatingAsyncSubAgentMiddleware,
)
from deep_data_research_agent.backends import SUPERVISOR_BACKEND
from deep_data_research_agent.config import (
    create_chat_model,
    create_data_analyst_model,
    create_reviewer_model,
)
from deep_data_research_agent.database_tools import DATABASE_TOOLS
from deep_data_research_agent.interaction_tools import INTERACTION_TOOLS
from deep_data_research_agent.memory import (
    SUPERVISOR_MEMORY_TOOLS,
    USER_MEMORY_PATH,
    AsyncTaskBridgeMiddleware,
    FailureReviewMiddleware,
    MemoryRefreshMiddleware,
    agent_memory_path,
)
from deep_data_research_agent.model_profile import register_mvp_profile
from deep_data_research_agent.prompts import (
    ANALYSIS_REVIEWER_PROMPT,
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
from deep_data_research_agent.subagent_contracts import (
    ReviewerResultValidationMiddleware,
    ReviewerToolGuardMiddleware,
    SubagentModelCallLimitMiddleware,
)

register_mvp_profile()

graph = create_deep_agent(
    name="supervisor",
    model=create_chat_model(),
    system_prompt=SUPERVISOR_PROMPT,
    tools=[
        *ASSIGN_SKILL_TOOL,
        *INTERACTION_TOOLS,
        *SUPERVISOR_MEMORY_TOOLS,
    ],
    subagents=[
        SubAgent(
            name="data-analyst",
            model=create_data_analyst_model(),
            description=(
                "分析本地 CSV、TSV、XLSX 文件或 PostgreSQL 只读数据。委派时必须标注模式："
                "quick_answer 用于直接查值、计数或简单统计，只返回经校验的简洁结论且不生成"
                "文件；formal_report 用于复杂分析或用户明确要求报告、图表、导出时，在一次"
                "委派内完成探查、计算、验证、制图和 Markdown 主报告，并生成可核验产物；"
                "信息不足时返回 needs_input。"
            ),
            system_prompt=DATA_ANALYST_PROMPT,
            tools=[*DATABASE_TOOLS],
            skills=[
                f"{public_skill_root('data-analyst')}/",
                f"{user_skill_root('data-analyst')}/",
            ],
            middleware=[
                # DeepAgents 0.7 makes planning opt-in; keep it for analysis.
                TodoListMiddleware(),
                MemoryRefreshMiddleware(
                    backend=SUPERVISOR_BACKEND,
                    sources=[USER_MEMORY_PATH, agent_memory_path("data-analyst")],
                ),
                MongoSkillsRestoreMiddleware(
                    component="supervisor",
                    agent_name="data-analyst",
                ),
                FailureReviewMiddleware(
                    agent_name="data-analyst",
                    reviewable_tools={tool.name for tool in DATABASE_TOOLS},
                ),
            ],
        ),
        SubAgent(
            name="analysis-reviewer",
            model=create_reviewer_model(),
            description=(
                "对 data-analyst 已生成的 Markdown 主报告和声明产物进行只读质量复核；"
                "按独立角色检查数字一致性、方法有效性或结论证据与限制，"
                "最终返回 passed、revision_required 或 failed 的纯 JSON，不修改产物。"
                "默认不调用，除非用户明确说明"
            ),
            system_prompt=ANALYSIS_REVIEWER_PROMPT,
            # Explicitly avoid inheriting the Supervisor's business tools.
            tools=[],
            middleware=[
                # after_model hooks run in reverse order: count the model call
                # before validating or redirecting its final JSON response.
                ReviewerResultValidationMiddleware(),
                ReviewerToolGuardMiddleware(),
                SubagentModelCallLimitMiddleware(
                    agent_name="analysis-reviewer",
                    run_limit=12,
                ),
            ],
        ),
    ],
    middleware=[
        # Reviewer deliberately omits this official Todo middleware.
        TodoListMiddleware(),
        SandboxLifecycleMiddleware(
            component="supervisor",
            # Skill 的下载与依赖测试需要联网，supervisor 沙箱打开网络。
            network_enabled=True,
        ),
        # Directly configure DeepAgents' memory middleware with a read-only
        # prompt, while refreshing checkpoint-cached contents every run.
        MemoryRefreshMiddleware(
            backend=SUPERVISOR_BACKEND,
            sources=[USER_MEMORY_PATH, agent_memory_path("supervisor")],
        ),
        MongoSkillsRestoreMiddleware(
            component="supervisor",
            agent_name="supervisor",
        ),
        SkillToolErrorMiddleware(
            tool_names={tool.name for tool in ASSIGN_SKILL_TOOL},
        ),
        MetadataPropagatingAsyncSubAgentMiddleware(
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
            backend=SUPERVISOR_BACKEND,
            sources=[
                (f"{public_skill_root('supervisor')}/", "公共"),
                (f"{user_skill_root('supervisor')}/", "用户"),
            ],
        ),
        FailureReviewMiddleware(
            agent_name="supervisor",
            reviewable_tools={
                "task",
                "start_async_task",
                "check_async_task",
                "update_async_task",
                "cancel_async_task",
                "list_async_tasks",
                "assign_skill",
                "request_report_download",
                "send_report_email",
            },
        ),
    ],
    backend=SUPERVISOR_BACKEND,
    interrupt_on={
        "ask_user": {"allowed_decisions": ["respond"]},
        "request_report_download": {
            "allowed_decisions": ["approve", "reject"],
        },
        "send_report_email": {
            "allowed_decisions": ["approve", "reject"],
        },
    },
)
