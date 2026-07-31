"""Sandbox lifecycle and reloadable Skill middleware."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from deepagents.middleware.skills import SkillsMiddleware, SkillsState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.store.base import BaseStore

from deep_data_research_agent import sandbox_manager
from deep_data_research_agent.identity import assigned_skill_namespace, user_identity

_SKILLS_ROOT = Path(__file__).resolve().parent / "skills"


def _load_builtin_files(scope: str) -> list[tuple[str, bytes]]:
    """Load immutable built-in Skill files before ASGI request handling."""

    root = _SKILLS_ROOT / scope
    if not root.is_dir():
        raise RuntimeError(f"内置 Skill 目录不存在：{root}")
    return [
        ((Path(scope) / path.relative_to(root)).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]


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


def _stored_file_content(value: dict[str, Any]) -> bytes:
    """Decode the v2 StoreBackend value written by Skill activation."""

    content = value.get("content")
    encoding = value.get("encoding")
    if not isinstance(content, str):
        raise TypeError("MongoDB Skill 文件缺少字符串内容")
    if encoding == "base64":
        return base64.b64decode(content, validate=True)
    if encoding in {None, "utf-8"}:
        return content.encode("utf-8")
    raise ValueError(f"MongoDB Skill 文件编码不受支持：{encoding}")


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


class SkillsSyncMiddleware(AgentMiddleware):
    """Copy built-in Skill files into the component's physical sandbox."""

    def __init__(self, *, component: str, scope: str) -> None:
        self._component = component
        self._files = _load_builtin_files(scope)

    async def abefore_agent(self, state, runtime):
        thread_id = sandbox_manager.thread_id_from_runtime(runtime)
        await sandbox_manager.SANDBOX_MANAGER.replace_directory_files(
            thread_id,
            "/skills",
            self._files,
            component=self._component,
        )


class UserSkillsRestoreMiddleware(AgentMiddleware):
    """Restore active MongoDB Skill files into an Agent's physical sandbox."""

    def __init__(self, *, component: str, agent_name: str) -> None:
        self._component = component
        self._agent_name = agent_name

    async def abefore_agent(self, state, runtime):
        store = getattr(runtime, "store", None)
        if store is None:
            raise RuntimeError("LangGraph Store 不可用，无法恢复已激活 Skill")

        namespace = assigned_skill_namespace(runtime, self._agent_name)
        items = await _search_all(store, namespace)
        files: list[tuple[str, bytes]] = []
        for item in items:
            if not item.key.startswith("/active/"):
                continue
            relative = item.key.removeprefix("/")
            files.append((relative, _stored_file_content(item.value)))

        thread_id = sandbox_manager.thread_id_from_runtime(runtime)
        await sandbox_manager.SANDBOX_MANAGER.replace_directory_files(
            thread_id,
            "/persisted-skills",
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
