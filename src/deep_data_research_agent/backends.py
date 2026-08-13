"""DeepAgents filesystem backend configuration."""

from __future__ import annotations

from typing import Any

from deepagents import FilesystemPermission
from deepagents.backends import (
    CompositeBackend,
    StateBackend,
    StoreBackend,
)

from deep_data_research_agent import sandbox_manager
from deep_data_research_agent.identity import assigned_skill_namespace
from deep_data_research_agent.memory import (
    failure_memory_namespace,
    user_memory_namespace,
)
from deep_data_research_agent.skill_storage import public_skill_namespace


def _thread_id(runtime: Any) -> str:
    """Return the sanitized LangGraph thread ID for a backend factory call."""

    return sandbox_manager.thread_id_from_runtime(runtime)


def _sandbox_backend(
    runtime: Any,
    *,
    component: str,
    skill_agents: tuple[str, ...],
) -> CompositeBackend:
    """Build one request-local sandbox backend with stable routed storage."""

    sandbox = sandbox_manager.SANDBOX_MANAGER.get_backend(
        _thread_id(runtime),
        component=component,
    )
    routes: dict[str, Any] = {
        "/state/": StateBackend(),
        "/memories/user/": StoreBackend(
            namespace=user_memory_namespace,
            file_format="v2",
        ),
    }
    for agent_name in skill_agents:
        routes[f"/memories/agent/{agent_name}/"] = StoreBackend(
            namespace=lambda _rt, name=agent_name: failure_memory_namespace(name),
            file_format="v2",
        )
        # Route at the Agent directory so the remaining StoreBackend key keeps
        # the required /active/... prefix used in MongoDB.
        routes[f"/skills/public/{agent_name}/"] = StoreBackend(
            namespace=lambda _rt, name=agent_name: public_skill_namespace(name),
            file_format="v2",
        )
        routes[f"/skills/user/{agent_name}/"] = StoreBackend(
            namespace=lambda rt, name=agent_name: assigned_skill_namespace(rt, name),
            file_format="v2",
        )

    return CompositeBackend(
        default=sandbox,
        routes=routes,
        artifacts_root="/state",
    )


def create_backend(runtime: Any) -> CompositeBackend:
    """Create the Supervisor backend after its sandbox lifecycle hook runs."""

    return _sandbox_backend(
        runtime,
        component="supervisor",
        skill_agents=("supervisor", "data-analyst"),
    )


def create_worker_backend(runtime: Any) -> CompositeBackend:
    """Create the crawl-worker backend after its outer graph initializes it."""

    return _sandbox_backend(
        runtime,
        component="crawl-worker",
        skill_agents=("crawl-worker",),
    )


# DeepAgents evaluates these rules in order. Unmatched paths, such as
# /workspace/** and /skill-manage/**, are handled by the isolated default sandbox.
FILESYSTEM_PERMISSIONS = [
    # HTTP 上传绕过 Agent 文件工具；模型只能读取原始输入，不能用
    # write_file/edit_file 静默覆盖用户文件。
    FilesystemPermission(
        operations=["write"],
        paths=["/workspace/input/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=["/workspace/input/**"],
        mode="allow",
    ),
    FilesystemPermission(
        operations=["write"],
        paths=["/skills/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=["/skills/**"],
        mode="allow",
    ),
    FilesystemPermission(
        operations=["read", "write"],
        paths=["/state/**"],
        mode="allow",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=[
            "/memories/agent/supervisor/archive/**",
            "/memories/agent/data-analyst/archive/**",
            "/memories/agent/crawl-worker/archive/**",
        ],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["write"],
        paths=["/memories/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=["/memories/**"],
        mode="allow",
    ),
]


WORKER_FILESYSTEM_PERMISSIONS = [
    FilesystemPermission(
        operations=["read"],
        paths=["/memories/agent/crawl-worker/archive/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["write"],
        paths=["/skills/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=["/skills/**"],
        mode="allow",
    ),
    FilesystemPermission(
        operations=["read", "write"],
        paths=["/state/**"],
        mode="allow",
    ),
    FilesystemPermission(
        operations=["write"],
        paths=["/memories/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=["/memories/**"],
        mode="allow",
    ),
]
