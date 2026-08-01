"""DeepAgents filesystem backend configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepagents import FilesystemPermission
from deepagents.backends import (
    CompositeBackend,
    FilesystemBackend,
    StateBackend,
    StoreBackend,
)

from deep_data_research_agent import sandbox_manager
from deep_data_research_agent.identity import assigned_skill_namespace

SKILLS_ROOT = Path(__file__).resolve().parent / "skills"
# FilesystemBackend 构造时会同步解析本地路径，必须在模块导入阶段完成，不能在
# ReloadableSkillsMiddleware.before_agent 的异步请求路径中重复初始化。
_BUILTIN_SKILLS_BACKEND = FilesystemBackend(
    root_dir=SKILLS_ROOT,
    virtual_mode=True,
)


def _thread_id(runtime: Any) -> str:
    """Return the sanitized LangGraph thread ID for a backend factory call."""

    return sandbox_manager.thread_id_from_runtime(runtime)


def _sandbox_backend(
    runtime: Any,
    *,
    component: str,
) -> CompositeBackend:
    """Build one request-local sandbox backend with stable routed storage."""

    sandbox = sandbox_manager.SANDBOX_MANAGER.get_backend(
        _thread_id(runtime),
        component=component,
    )
    routes: dict[str, Any] = {
        "/state/": StateBackend(),
        "/skills/": _BUILTIN_SKILLS_BACKEND,
        "/persisted-skills/": StoreBackend(
            namespace=lambda rt: assigned_skill_namespace(rt, component),
            file_format="v2",
        ),
    }

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
    )


def create_worker_backend(runtime: Any) -> CompositeBackend:
    """Create the crawl-worker backend after its outer graph initializes it."""

    return _sandbox_backend(
        runtime,
        component="crawl-worker",
    )


# DeepAgents evaluates these rules in order. Unmatched paths, such as
# /workspace/** and /skill-manage/**, are handled by the isolated default sandbox.
FILESYSTEM_PERMISSIONS = [
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
        paths=["/persisted-skills/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=["/persisted-skills/**"],
        mode="allow",
    ),
]


WORKER_FILESYSTEM_PERMISSIONS = [
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
        paths=["/persisted-skills/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["read"],
        paths=["/persisted-skills/**"],
        mode="allow",
    ),
]
