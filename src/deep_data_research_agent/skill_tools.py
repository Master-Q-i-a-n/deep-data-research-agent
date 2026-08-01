"""极简 Skill 分配工具：assign_skill。

用户在 supervisor 沙箱的 /skill-manage/{name}/ 中创建或下载 Skill 并完成测试后，
调用 assign_skill 将其持久化到 MongoDB 并分配给目标 Agent。该目录直接由默认
OpenSandbox 后端处理，所以文件工具与 execute 使用完全相同的路径。
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


def _absolute_path(root: str, relative: str) -> str:
    """把 aglob 返回的相对路径规整为沙箱绝对路径。"""

    path = PurePosixPath(relative)
    if path.is_absolute():
        return path.as_posix()
    return (PurePosixPath(root) / path).as_posix()


async def _enumerate_physical_files(
    backend,
    root: str,
) -> list[str]:
    """枚举暂存目录下的所有文件（沙箱绝对路径）。

    优先用 aglob；若 aglob 返回空或报错（曾出现目录有文件但 aglob 为空的情形），
    改用单值 JSON 输出兜底，仍为空则区分「目录不存在」与「目录为空」报错。
    """

    result = await backend.aglob("**/*", path=root)
    paths: list[str] = []
    if not result.error:
        for item in result.matches or []:
            if item.get("is_dir"):
                continue
            paths.append(_absolute_path(root, str(item["path"])))
    if paths:
        return paths

    # OpenSandbox 当前会直接拼接 stdout 日志块，多行 find 输出可能失去换行。
    # 改为输出单个 JSON 数组，使文件列表不依赖日志中的行分隔符。
    script = (
        "import json, os; "
        f"root = {root!r}; "
        "paths = sorted("
        "os.path.join(base, name) "
        "for base, _, names in os.walk(root) "
        "for name in names "
        "if not os.path.islink(os.path.join(base, name))"
        "); "
        "print(json.dumps(paths, ensure_ascii=False))"
    )
    probe = await backend.aexecute(
        f"python3 -c {shlex.quote(script)}",
        timeout=30,
    )
    output = (probe.output or "").strip()
    try:
        decoded = json.loads(output) if output else []
    except json.JSONDecodeError as exc:
        raise RuntimeError("无法解析沙箱返回的 Skill 文件列表") from exc
    found = [
        path
        for path in decoded
        if isinstance(path, str) and path.startswith(f"{root}/")
    ]
    if not found:
        if probe.exit_code not in {None, 0}:
            raise RuntimeError(f"Skill 目录不存在：{root}/")
        raise RuntimeError(f"Skill 目录 {root}/ 不存在或为空")
    return found


async def _read_skill_files(
    backend,
    root: str,
) -> list[tuple[str, bytes]]:
    """读取 Skill 暂存目录的全部文件，返回 (相对路径, 内容) 列表。"""

    paths = await _enumerate_physical_files(backend, root)
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
        "将 /skill-manage/{skill_name}/ 中已通过测试的 Skill 分配给目标 Agent 并持久化"
        "到 MongoDB，并保留候选目录以便继续检查或分配。目标 Agent 在下一轮对话中"
        "自动加载该 Skill。"
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
    source_dir = f"/skill-manage/{name}"
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
