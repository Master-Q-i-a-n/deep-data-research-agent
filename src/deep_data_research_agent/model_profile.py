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


def register_mvp_profile() -> None:
    """Register separate Supervisor and crawl-worker harness profiles."""

    register_harness_profile(
        "openai",
        HarnessProfile(
            base_system_prompt=BASE_AGENT_PROMPT,
            tool_description_overrides=TOOL_DESCRIPTION_OVERRIDES,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    register_harness_profile(
        "deep-data-worker",
        HarnessProfile(
            base_system_prompt=BASE_AGENT_PROMPT,
            tool_description_overrides=TOOL_DESCRIPTION_OVERRIDES,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
