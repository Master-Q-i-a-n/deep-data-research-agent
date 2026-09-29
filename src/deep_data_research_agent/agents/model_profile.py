"""MVP DeepAgents harness profile."""

from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)

from deep_data_research_agent.agents.prompts import (
    BASE_AGENT_PROMPT,
    TOOL_DESCRIPTION_OVERRIDES,
)
from deep_data_research_agent.core.model_execution import ModelExecutionProfile

SUPERVISOR_MODEL_PROFILE = ModelExecutionProfile(
    name="supervisor",
    enable_streaming=True,
    enable_hosted_web_search=True,
)
DATA_ANALYST_MODEL_PROFILE = ModelExecutionProfile(
    name="data-analyst",
    harness_provider="deep-data-worker",
)
ANALYSIS_REVIEWER_MODEL_PROFILE = ModelExecutionProfile(
    name="analysis-reviewer",
    harness_provider="deep-data-reviewer",
)
CRAWL_WORKER_MODEL_PROFILE = ModelExecutionProfile(
    name="crawl-worker",
    harness_provider="deep-data-worker",
)

DEFAULT_EXCLUDED_TOOLS = frozenset({"delete"})
REVIEWER_EXCLUDED_TOOLS = frozenset(
    {"delete", "write_file", "edit_file", "write_todos"}
)


def register_mvp_profile() -> None:
    """Register separate Supervisor and crawl-worker harness profiles."""

    # DeepAgents replaces inline subagent prompts when base_system_prompt is set.
    # A suffix preserves each role's instructions and appends shared rules instead.
    register_harness_profile(
        "openai",
        HarnessProfile(
            system_prompt_suffix=BASE_AGENT_PROMPT,
            tool_description_overrides=TOOL_DESCRIPTION_OVERRIDES,
            # DeepAgents 0.7 adds a recursive delete tool. This application
            # deletes user files only through its authenticated HTTP API.
            excluded_tools=DEFAULT_EXCLUDED_TOOLS,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    register_harness_profile(
        "deep-data-worker",
        HarnessProfile(
            system_prompt_suffix=BASE_AGENT_PROMPT,
            tool_description_overrides=TOOL_DESCRIPTION_OVERRIDES,
            excluded_tools=DEFAULT_EXCLUDED_TOOLS,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    register_harness_profile(
        "deep-data-reviewer",
        HarnessProfile(
            system_prompt_suffix=BASE_AGENT_PROMPT,
            tool_description_overrides=TOOL_DESCRIPTION_OVERRIDES,
            # Execute is filtered dynamically and is visible only to the
            # numeric-consistency Reviewer role.
            excluded_tools=REVIEWER_EXCLUDED_TOOLS,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
