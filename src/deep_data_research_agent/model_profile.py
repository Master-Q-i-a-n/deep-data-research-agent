"""MVP DeepAgents harness profile."""

from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)

from deep_data_research_agent.prompts import (
    BASE_AGENT_PROMPT,
    TOOL_DESCRIPTION_OVERRIDES,
)

DEFAULT_EXCLUDED_TOOLS = frozenset({"delete"})
REVIEWER_EXCLUDED_TOOLS = frozenset(
    {"delete", "write_file", "edit_file", "write_todos"}
)


def register_mvp_profile() -> None:
    """Register separate Supervisor and crawl-worker harness profiles."""

    register_harness_profile(
        "openai",
        HarnessProfile(
            base_system_prompt=BASE_AGENT_PROMPT,
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
            base_system_prompt=BASE_AGENT_PROMPT,
            tool_description_overrides=TOOL_DESCRIPTION_OVERRIDES,
            excluded_tools=DEFAULT_EXCLUDED_TOOLS,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    register_harness_profile(
        "deep-data-reviewer",
        HarnessProfile(
            base_system_prompt=BASE_AGENT_PROMPT,
            tool_description_overrides=TOOL_DESCRIPTION_OVERRIDES,
            # Execute is filtered dynamically and is visible only to the
            # numeric-consistency Reviewer role.
            excluded_tools=REVIEWER_EXCLUDED_TOOLS,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
