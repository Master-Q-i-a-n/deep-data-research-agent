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
    """Disable host execution and the implicit synchronous subagent.

    The application uses only the explicitly configured ASGI async worker in
    this phase. Provider-level registration also works for pre-built
    ``ChatOpenAI`` instances.
    """

    register_harness_profile(
        "openai",
        HarnessProfile(
            base_system_prompt=BASE_AGENT_PROMPT,
            tool_description_overrides=TOOL_DESCRIPTION_OVERRIDES,
            excluded_tools=frozenset({"execute"}),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
