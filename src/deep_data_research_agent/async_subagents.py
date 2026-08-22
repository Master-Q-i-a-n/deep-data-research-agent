"""Project-specific async subagent wiring with evaluation metadata propagation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from deepagents.middleware.async_subagents import (
    AsyncSubAgent,
    AsyncSubAgentMiddleware,
    AsyncTask,
    StartAsyncTaskSchema,
    _ClientCache,
    _validate_agent_type,
)
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command

logger = logging.getLogger(__name__)


class MetadataPropagatingAsyncSubAgentMiddleware(AsyncSubAgentMiddleware):
    """Launch child runs with the parent's benchmark identifiers when present."""

    def __init__(
        self,
        *,
        async_subagents: list[AsyncSubAgent],
        system_prompt: str | None,
    ) -> None:
        super().__init__(
            async_subagents=async_subagents,
            system_prompt=system_prompt,
        )
        agent_map = {agent["name"]: agent for agent in async_subagents}
        clients = _ClientCache(agent_map)
        original = next(tool for tool in self.tools if tool.name == "start_async_task")

        def start_async_task(
            description: str,
            subagent_type: str,
            runtime: ToolRuntime,
        ) -> str | Command:
            error = _validate_agent_type(agent_map, subagent_type)
            if error:
                return error
            spec = agent_map[subagent_type]
            try:
                client = clients.get_sync(subagent_type)
                metadata = self._child_metadata(runtime, spec["graph_id"])
                thread = client.threads.create(
                    metadata=metadata,
                    graph_id=spec["graph_id"],
                )
                run = client.runs.create(
                    thread_id=thread["thread_id"],
                    assistant_id=spec["graph_id"],
                    input={"messages": [{"role": "user", "content": description}]},
                    metadata=metadata,
                )
            except Exception as exc:  # noqa: BLE001 - preserve the tool contract.
                logger.warning("Failed to launch async subagent '%s': %s", subagent_type, exc)
                return f"Failed to launch async subagent '{subagent_type}': {exc}"
            return self._task_command(runtime, subagent_type, thread, run)

        async def astart_async_task(
            description: str,
            subagent_type: str,
            runtime: ToolRuntime,
        ) -> str | Command:
            error = _validate_agent_type(agent_map, subagent_type)
            if error:
                return error
            spec = agent_map[subagent_type]
            try:
                client = clients.get_async(subagent_type)
                metadata = self._child_metadata(runtime, spec["graph_id"])
                thread = await client.threads.create(
                    metadata=metadata,
                    graph_id=spec["graph_id"],
                )
                run = await client.runs.create(
                    thread_id=thread["thread_id"],
                    assistant_id=spec["graph_id"],
                    input={"messages": [{"role": "user", "content": description}]},
                    metadata=metadata,
                )
            except Exception as exc:  # noqa: BLE001 - preserve the tool contract.
                logger.warning("Failed to launch async subagent '%s': %s", subagent_type, exc)
                return f"Failed to launch async subagent '{subagent_type}': {exc}"
            return self._task_command(runtime, subagent_type, thread, run)

        replacement = StructuredTool.from_function(
            name="start_async_task",
            func=start_async_task,
            coroutine=astart_async_task,
            description=original.description,
            args_schema=StartAsyncTaskSchema,
            infer_schema=False,
        )
        self.tools = [
            replacement if tool.name == "start_async_task" else tool
            for tool in self.tools
        ]

    @staticmethod
    def _child_metadata(runtime: ToolRuntime, graph_id: str) -> dict[str, Any]:
        config = runtime.config if isinstance(runtime.config, dict) else {}
        parent = config.get("metadata") if isinstance(config.get("metadata"), dict) else {}
        metadata = {
            key: parent[key]
            for key in ("eval_run_id", "eval_case_id")
            if key in parent
        }
        metadata.update(
            {
                "graph_id": graph_id,
                "kind": "async-subagent",
            }
        )
        parent_thread_id = parent.get("thread_id")
        if parent_thread_id:
            metadata["parent_thread_id"] = str(parent_thread_id)
        return metadata

    @staticmethod
    def _task_command(
        runtime: ToolRuntime,
        subagent_type: str,
        thread: dict[str, Any],
        run: dict[str, Any],
    ) -> Command:
        task_id = str(thread["thread_id"])
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        task: AsyncTask = {
            "task_id": task_id,
            "agent_name": subagent_type,
            "thread_id": task_id,
            "run_id": str(run["run_id"]),
            "status": "running",
            "created_at": now,
            "last_checked_at": now,
            "last_updated_at": now,
        }
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Launched async subagent. task_id: {task_id}",
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
                "async_tasks": {task_id: task},
            }
        )
