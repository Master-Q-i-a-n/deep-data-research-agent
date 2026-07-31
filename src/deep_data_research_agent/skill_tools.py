"""极简 Skill 分配工具：assign_skill。

用户在 supervisor 沙箱的 /skills/main/{name}/ 中创建或下载 Skill 并通过 execute 完成测试后，
调用 assign_skill 一步将其持久化到 MongoDB 并分配给目标 Agent。下一轮对话由
UserSkillsRestoreMiddleware 从 MongoDB 恢复到 /persisted-skills/。
"""

from __future__ import annotations

import base64
import json
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated

from langchain.tools import ToolRuntime, tool

from deep_data_research_agent import sandbox_manager
from deep_data_research_agent.identity import user_hash

_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_SKILL_NAME_LENGTH = 64
_LANGGRAPH_JSON = Path(__file__).resolve().parents[2] / "langgraph.json"
_FALLBACK_TARGETS = frozenset({"supervisor", "crawl-worker"})
_COMPONENT = "supervisor"


def _available_targets() -> frozenset[str]:
    """按 langgraph.json 的 graphs 动态读取可分配的目标 Agent。

    解析失败时回退到内置默认值，保证工具仍可用。
    """

    try:
        config = json.loads(_LANGGRAPH_JSON.read_text("utf-8"))
        graphs = config.get("graphs")
    except (OSError, ValueError, json.JSONDecodeError):
        graphs = None
    if isinstance(graphs, dict) and graphs:
        return frozenset(graphs.keys())
    return _FALLBACK_TARGETS


def _validated_skill_name(value: str) -> str:
    name = value.strip()
    if (
        not name
        or len(name) > _MAX_SKILL_NAME_LENGTH
        or not _SKILL_NAME_RE.fullmatch(name)
    ):
        raise ValueError(
            "Skill 名称必须为 1 到 64 个字符，且只能包含小写字母、数字和单连字符"
        )
    return name


def _validated_targets(values: list[str], available: frozenset[str]) -> list[str]:
    normalized = list(dict.fromkeys(value.strip() for value in values))
    if not normalized:
        raise ValueError("必须至少指定一个目标 Agent")
    invalid = [value for value in normalized if value not in available]
    if invalid:
        allowed = "、".join(sorted(available))
        raise ValueError(f"无效目标：{'、'.join(invalid)}；可用目标：{allowed}")
    return normalized


def _file_store_value(content: bytes) -> dict[str, str]:
    """将文件内容编码为 StoreBackend v2 格式，供 _stored_file_content 解码。"""

    now = datetime.now(UTC).isoformat()
    try:
        text = content.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = base64.b64encode(content).decode("ascii")
        encoding = "base64"
    return {
        "content": text,
        "encoding": encoding,
        "created_at": now,
        "modified_at": now,
    }


async def _read_skill_files(backend, root: str) -> list[tuple[str, bytes]]:
    result = await backend.aglob("**/*", path=root)
    if result.error:
        raise RuntimeError(f"无法枚举 Skill 目录：{result.error}")
    paths = [
        str(item["path"])
        for item in result.matches or []
        if not item.get("is_dir")
    ]
    if not paths:
        raise RuntimeError(f"Skill 目录 {root}/ 不存在或为空")
    responses = await backend.adownload_files(paths)
    files: list[tuple[str, bytes]] = []
    for response in responses:
        if response.error or response.content is None:
            raise RuntimeError(
                f"无法读取 Skill 文件 {response.path}：{response.error or '内容为空'}"
            )
        absolute = PurePosixPath(response.path)
        if not absolute.is_relative_to(root):
            raise RuntimeError(f"Skill 文件越过根目录：{response.path}")
        relative = absolute.relative_to(root).as_posix()
        files.append((relative, response.content))
    if not any(relative == "SKILL.md" for relative, _ in files):
        raise RuntimeError(f"Skill {root}/ 缺少根级 SKILL.md")
    return sorted(files)


@tool(
    "assign_skill",
    description=(
        "将 /skills/main/{skill_name}/ 中已通过测试的 Skill 分配给目标 Agent 并持久化"
        "到 MongoDB，然后清理临时目录。目标 Agent 在下一轮对话中自动加载该 Skill。"
    ),
)
async def assign_skill(
    skill_name: Annotated[str, "Skill 名称（小写字母、数字和单连字符）"],
    targets: Annotated[list[str], "目标 Agent 名称列表，如 ['supervisor'] 或 ['crawl-worker']"],
    runtime: ToolRuntime,
) -> str:
    """一步完成 Skill 的分配与持久化。"""

    name = _validated_skill_name(skill_name)
    available = _available_targets()
    normalized_targets = _validated_targets(targets, available)

    store = getattr(runtime, "store", None)
    if store is None:
        raise RuntimeError("LangGraph Store 不可用，无法持久化 Skill")

    thread_id = sandbox_manager.thread_id_from_runtime(runtime)
    backend = sandbox_manager.SANDBOX_MANAGER.get_backend(
        thread_id,
        component=_COMPONENT,
    )
    source_dir = f"/skills/main/{name}"
    files = await _read_skill_files(backend, source_dir)

    user_ns = user_hash(runtime)
    for target in normalized_targets:
        namespace = (user_ns, "skills", "assigned", target)
        for relative, content in files:
            await store.aput(
                namespace,
                f"/active/{name}/{relative}",
                _file_store_value(content),
            )
        await store.aput(
            namespace,
            f"/manifests/{name}.json",
            _file_store_value(
                json.dumps(
                    {
                        "skill_name": name,
                        "activated_at": datetime.now(UTC).isoformat(),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            ),
        )

    cleanup = await backend.aexecute(
        f"rm -rf {shlex.quote(source_dir)}",
        timeout=30,
    )
    if cleanup.exit_code not in {None, 0}:
        raise RuntimeError(
            f"清理临时 Skill 目录失败：{cleanup.output[-500:]}"
        )

    return json.dumps(
        {
            "status": "assigned",
            "skill_name": name,
            "targets": normalized_targets,
            "file_count": len(files),
        },
        ensure_ascii=False,
    )


ASSIGN_SKILL_TOOL = [assign_skill]
