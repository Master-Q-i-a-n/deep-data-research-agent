"""Sandbox lifecycle and reloadable Skill middleware."""

from __future__ import annotations

import json
from typing import Any

from deepagents.middleware.skills import SkillsMiddleware, SkillsState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.store.base import BaseStore

from deep_data_research_agent.core.identity import (
    assigned_skill_namespace,
    user_identity,
)
from deep_data_research_agent.infrastructure.sandbox import manager as sandbox_manager
from deep_data_research_agent.skill_system.storage import (
    public_skill_namespace,
    public_skill_root,
    stored_file_content,
    user_skill_root,
)


async def _search_all(
    store: BaseStore,
    namespace: tuple[str, ...],
) -> list[Any]:
    items: list[Any] = []
    offset = 0
    while True:
        page = await store.asearch(namespace, limit=100, offset=offset)
        if not page:
            break
        items.extend(page)
        if len(page) < 100:
            break
        offset += 100
    return items


class SandboxLifecycleMiddleware(AgentMiddleware):
    """Ensure and export the Supervisor sandbox around one Agent run."""

    def __init__(self, *, component: str, network_enabled: bool = False) -> None:
        self._component = component
        self._network_enabled = network_enabled

    async def abefore_agent(self, state, runtime):
        thread_id = sandbox_manager.thread_id_from_runtime(runtime)
        await sandbox_manager.SANDBOX_MANAGER.ensure(
            thread_id,
            component=self._component,
            network_enabled=self._network_enabled,
            user_id=user_identity(runtime),
        )

    async def aafter_agent(self, state, runtime):
        thread_id = sandbox_manager.thread_id_from_runtime(runtime)
        # HITL resume or another worker process may not share the in-memory
        # handle from before_agent. Re-adopt it from the Redis registry first.
        await sandbox_manager.SANDBOX_MANAGER.ensure(
            thread_id,
            component=self._component,
            network_enabled=self._network_enabled,
            user_id=user_identity(runtime),
        )
        await sandbox_manager.SANDBOX_MANAGER.export_workspace(
            thread_id,
            component=self._component,
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


class MongoSkillsRestoreMiddleware(AgentMiddleware):
    """Restore one Agent's public and private MongoDB Skills into its sandbox."""

    def __init__(self, *, component: str, agent_name: str) -> None:
        self._component = component
        self._agent_name = agent_name

    async def abefore_agent(self, state, runtime):
        store = getattr(runtime, "store", None)
        if store is None:
            raise RuntimeError("LangGraph Store 不可用，无法恢复已激活 Skill")

        thread_id = sandbox_manager.thread_id_from_runtime(runtime)
        sources = (
            (public_skill_namespace(self._agent_name), public_skill_root(self._agent_name)),
            (assigned_skill_namespace(runtime, self._agent_name), user_skill_root(self._agent_name)),
        )
        for namespace, root in sources:
            items = await _search_all(store, namespace)
            files = [
                (
                    item.key.removeprefix("/active/"),
                    stored_file_content(item.value),
                )
                for item in items
                if item.key.startswith("/active/")
            ]
            await sandbox_manager.SANDBOX_MANAGER.replace_directory_files(
                thread_id,
                root,
                files,
                component=self._component,
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
