"""Sandbox lifecycle and reloadable Skill middleware."""

from __future__ import annotations

import json
from typing import Any

from deepagents.middleware.skills import SkillsMiddleware, SkillsState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from deep_data_research_agent.core.identity import user_identity
from deep_data_research_agent.infrastructure.sandbox import manager as sandbox_manager


class SandboxLifecycleMiddleware(AgentMiddleware):
    """Export an existing sandbox without allocating one for a pure conversation."""

    def __init__(self, *, component: str) -> None:
        self._component = component

    async def aafter_agent(self, state, runtime):
        thread_id = sandbox_manager.thread_id_from_runtime(runtime)
        # HITL resume or another worker process may not share the in-memory
        # handle from an earlier tool call. Re-adopt it from the Redis registry first.
        await sandbox_manager.SANDBOX_MANAGER.export_existing_workspace(
            thread_id,
            component=self._component,
            user_id=user_identity(runtime),
        )


class SkillToolErrorMiddleware(AgentMiddleware):
    """Turn expected Skill business failures into recoverable tool messages."""

    def __init__(self, *, tool_names: set[str]) -> None:
        self._tool_names = frozenset(tool_names)

    async def awrap_tool_call(self, request, handler):
        if request.tool.name not in self._tool_names:
            return await handler(request)
        try:
            return await handler(request)
        except Exception as exc:  # noqa: BLE001
            # The inner LangSmith tool span keeps the exception while the main
            # Supervisor conversation remains available for correction.
            return ToolMessage(
                content=json.dumps(
                    {"status": "failed", "error": str(exc)},
                    ensure_ascii=False,
                ),
                tool_call_id=str(request.tool_call.get("id", "")),
                name=request.tool.name,
                status="error",
            )


class ReloadableSkillsMiddleware(SkillsMiddleware):
    """Reload Skill metadata on every run so assignments affect existing threads."""

    @staticmethod
    def _without_cached_skills(state: SkillsState) -> dict[str, Any]:
        clean_state = dict(state)
        clean_state.pop("skills_metadata", None)
        clean_state.pop("skills_load_errors", None)
        return clean_state

    def before_agent(self, state, runtime, config):
        """Run the official synchronous loader without session-level cache."""

        return super().before_agent(
            self._without_cached_skills(state),
            runtime,
            config,
        )

    async def abefore_agent(self, state, runtime, config):
        """Run the official asynchronous loader without session-level cache."""

        return await super().abefore_agent(
            self._without_cached_skills(state),
            runtime,
            config,
        )
