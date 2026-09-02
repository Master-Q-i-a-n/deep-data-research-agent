"""Supervisor DeepAgent graph exposed through the LangGraph Agent Server."""

from deepagents import AsyncSubAgent, SubAgent, create_deep_agent
from langchain.agents.middleware import TodoListMiddleware

from deep_data_research_agent.admissions.token_usage import TokenUsageMiddleware
from deep_data_research_agent.agents.async_subagents import (
    MetadataPropagatingAsyncSubAgentMiddleware,
)
from deep_data_research_agent.agents.backends import SUPERVISOR_BACKEND
from deep_data_research_agent.agents.contracts import (
    ReviewerResultValidationMiddleware,
    ReviewerToolGuardMiddleware,
    SubagentModelCallLimitMiddleware,
)
from deep_data_research_agent.agents.middleware.skills import (
    MongoSkillsRestoreMiddleware,
    ReloadableSkillsMiddleware,
    SandboxLifecycleMiddleware,
    SkillToolErrorMiddleware,
)
from deep_data_research_agent.agents.model_profile import register_mvp_profile
from deep_data_research_agent.agents.prompts import (
    ANALYSIS_REVIEWER_PROMPT,
    ASYNC_SUBAGENT_PROMPT,
    DATA_ANALYST_PROMPT,
    SUPERVISOR_PROMPT,
)
from deep_data_research_agent.core.config import (
    create_graph_placeholder_model,
)
from deep_data_research_agent.memory.service import (
    SUPERVISOR_MEMORY_TOOLS,
    USER_MEMORY_PATH,
    AsyncTaskBridgeMiddleware,
    FailureReviewMiddleware,
    MemoryRefreshMiddleware,
    agent_memory_path,
)
from deep_data_research_agent.providers.context_usage import ContextUsageMiddleware
from deep_data_research_agent.providers.models import provider_summarization_middleware
from deep_data_research_agent.skill_system.storage import (
    public_skill_root,
    user_skill_root,
)
from deep_data_research_agent.tools.database import DATABASE_TOOLS
from deep_data_research_agent.tools.interaction import INTERACTION_TOOLS
from deep_data_research_agent.tools.skills import ASSIGN_SKILL_TOOL

register_mvp_profile()

graph = create_deep_agent(
    name="supervisor",
    model=create_graph_placeholder_model("supervisor"),
    system_prompt=SUPERVISOR_PROMPT,
    tools=[
        *ASSIGN_SKILL_TOOL,
        *INTERACTION_TOOLS,
        *SUPERVISOR_MEMORY_TOOLS,
    ],
    subagents=[
        SubAgent(
            name="data-analyst",
            model=create_graph_placeholder_model("worker"),
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
                provider_summarization_middleware("data-analyst", SUPERVISOR_BACKEND),
                TokenUsageMiddleware(agent_name="data-analyst"),
                ContextUsageMiddleware("data-analyst"),
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
            model=create_graph_placeholder_model("reviewer"),
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
                provider_summarization_middleware(
                    "analysis-reviewer", SUPERVISOR_BACKEND
                ),
                TokenUsageMiddleware(agent_name="analysis-reviewer"),
                ContextUsageMiddleware("analysis-reviewer"),
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
        provider_summarization_middleware("supervisor", SUPERVISOR_BACKEND),
        TokenUsageMiddleware(agent_name="supervisor"),
        ContextUsageMiddleware("supervisor"),
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
                        "使用 Tavily 批量抓取、提取并持久化公开网页，返回带 URL 来源的"
                        "采集结果和初步分析；适合长时间后台采集，不承接 Supervisor 可用"
                        "web_search 完成的常规网页检索。"
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
