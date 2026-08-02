"""Human-interaction tools for missing information and report downloads."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from pydantic import Field

from deep_data_research_agent import sandbox_manager
from deep_data_research_agent.identity import user_identity

DOWNLOADABLE_SUFFIXES = frozenset(
    {".csv", ".json", ".md", ".pdf", ".png", ".xlsx", ".zip"}
)


def _validated_download_name(value: str | None, source: PurePosixPath) -> str:
    """Return a safe browser download name without accepting a local path."""

    if value is None or not value.strip():
        return source.name
    name = value.strip()
    if PurePosixPath(name).name != name or "\\" in name or name in {".", ".."}:
        raise ValueError("下载名称只能是文件名，不能包含目录")
    if PurePosixPath(name).suffix.lower() not in DOWNLOADABLE_SUFFIXES:
        raise ValueError("下载文件类型不受支持")
    return name


@tool(
    "ask_user",
    description=(
        "当前任务缺少会影响分析正确性的关键信息时，暂停执行并请求用户补充。"
        "一次最多询问三个缺失字段；信息足够时不要调用。"
    ),
)
def ask_user(
    question: Annotated[str, "需要用户回答的明确问题"],
    missing_fields: Annotated[
        list[str],
        Field(min_length=1, max_length=3, description="缺失字段名称，最多三个"),
    ],
    known_information: Annotated[str, "已经确认的信息摘要"] = "",
) -> str:
    """Provide a defensive result if HITL interception is accidentally disabled."""

    return "尚未收到用户补充信息，不能继续假设或执行后续分析。"


@tool(
    "request_report_download",
    description=(
        "仅当用户明确要求下载、保存到本地或导出文件时调用。默认下载"
        "/workspace/final_report.md；调用前必须确认目标文件已经生成。"
    ),
)
async def request_report_download(
    file_path: Annotated[str, "要下载的 /workspace 下文件路径"] = (
        "/workspace/final_report.md"
    ),
    download_name: Annotated[str | None, "浏览器保存时使用的文件名，可省略"] = None,
    *,
    runtime: ToolRuntime,
) -> ToolMessage:
    """Export one approved sandbox file and return app-facing download metadata."""

    try:
        relative = sandbox_manager.workspace_relative_path(file_path)
        if relative.suffix.lower() not in DOWNLOADABLE_SUFFIXES:
            raise ValueError("仅支持下载报告、表格、图片、JSON 或 ZIP 产物")
        virtual_path = f"/workspace/{relative.as_posix()}"
        filename = _validated_download_name(download_name, relative)

        thread_id = sandbox_manager.thread_id_from_runtime(runtime)
        user_id = user_identity(runtime)
        backend = await sandbox_manager.SANDBOX_MANAGER.ensure(
            thread_id,
            component="supervisor",
            network_enabled=True,
            user_id=user_id,
        )
        responses = await backend.adownload_files([virtual_path])
        response = responses[0] if responses else None
        if response is None or response.error or response.content is None:
            detail = response.error if response is not None else "文件不存在"
            raise RuntimeError(f"无法读取下载文件：{detail}")

        # HITL 批准后立即导出，不能依赖仅在完整 run 结束时执行的 aafter_agent。
        await sandbox_manager.SANDBOX_MANAGER.export_workspace(
            thread_id,
            component="supervisor",
        )
        return ToolMessage(
            content=f"文件已准备下载：{filename}",
            tool_call_id=runtime.tool_call_id,
            name="request_report_download",
            status="success",
            artifact={
                "type": "file_download",
                "path": virtual_path,
                "filename": filename,
                "size": len(response.content),
            },
        )
    except Exception as exc:  # noqa: BLE001 - keep download failures recoverable.
        return ToolMessage(
            content=f"下载准备失败：{exc}",
            tool_call_id=runtime.tool_call_id,
            name="request_report_download",
            status="error",
        )


INTERACTION_TOOLS = [ask_user, request_report_download]
