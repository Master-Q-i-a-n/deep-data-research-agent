"""Safe Supervisor artifact discovery, resolution, and Markdown bundling."""

from __future__ import annotations

import mimetypes
import re
import zipfile
from io import BytesIO
from pathlib import PurePosixPath

from deep_data_research_agent.infrastructure.workspace import (
    WorkspaceFileNotFound,
    WorkspaceObject,
    WorkspaceScope,
    WorkspaceStore,
    workspace_relative_path,
)

DOWNLOADABLE_SUFFIXES = frozenset(
    {".csv", ".json", ".md", ".pdf", ".png", ".xlsx", ".zip"}
)
ARTIFACT_CARD_SUFFIXES = frozenset({".md", ".pdf", ".zip"})
BUNDLE_COMPANION_SUFFIXES = frozenset(
    {".csv", ".json", ".png", ".tsv", ".xlsx"}
)
_MARKDOWN_IMAGE_PATTERN = re.compile(
    r"!\[[^\]]*\]\(\s*(?:<(?P<angle>[^>]+)>|(?P<plain>[^\s)]+))"
    r"(?:\s+['\"][^'\"]*['\"])?\s*\)"
)
_HTML_IMAGE_PATTERN = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*(?P<quote>['\"])(?P<src>.*?)(?P=quote)[^>]*>",
    re.IGNORECASE,
)


class ArtifactError(ValueError):
    """A user-facing artifact validation error with an HTTP-compatible status."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


async def workspace_artifacts(
    store: WorkspaceStore,
    scope: WorkspaceScope,
) -> list[dict[str, object]]:
    """List user-facing artifacts while preserving the existing card policy."""

    artifacts: list[dict[str, object]] = []
    for item in await store.list(scope):
        relative = workspace_relative_path(item.path)
        if relative.parts[0] in {"input", "profile", "raw", "scripts"}:
            continue
        is_output = relative.parts[0] in {"charts", "output"}
        is_report = "report" in relative.stem.lower()
        if not is_output and not is_report:
            continue
        if relative.suffix.lower() not in ARTIFACT_CARD_SUFFIXES:
            continue
        mime_type = mimetypes.guess_type(relative.name)[0] or "application/octet-stream"
        artifacts.append(
            {
                "path": item.path,
                "filename": relative.name,
                "size": item.size,
                "mime_type": mime_type,
            }
        )
    report_priority = {
        "/workspace/output/final_report.pdf": 0,
        "/workspace/output/final_report.md": 1,
        "/workspace/final_report.pdf": 2,
        "/workspace/final_report.md": 3,
    }
    return sorted(
        artifacts,
        key=lambda item: (report_priority.get(str(item["path"]), 4), str(item["path"])),
    )


async def resolve_download_object(
    store: WorkspaceStore,
    scope: WorkspaceScope,
    virtual_path: str,
) -> WorkspaceObject:
    """Resolve one allowed virtual workspace object."""

    try:
        relative = workspace_relative_path(virtual_path)
    except ValueError as exc:
        raise ArtifactError(str(exc)) from exc
    if relative.suffix.lower() not in DOWNLOADABLE_SUFFIXES:
        raise ArtifactError("下载文件类型不受支持")
    try:
        return await store.stat(scope, relative)
    except WorkspaceFileNotFound as exc:
        raise ArtifactError("文件不存在", status_code=404) from exc


async def build_markdown_bundle(
    store: WorkspaceStore,
    scope: WorkspaceScope,
    virtual_path: str,
) -> tuple[bytes, str]:
    """Build a Markdown ZIP from durable workspace objects."""

    report = await resolve_download_object(store, scope, virtual_path)
    report_relative = workspace_relative_path(report.path)
    if report_relative.suffix.lower() != ".md":
        raise ArtifactError("只有 Markdown 报告支持图片打包下载")

    try:
        markdown = (await store.read(scope, report_relative)).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactError("Markdown 报告不是 UTF-8 编码", status_code=409) from exc

    sources = [
        match.group("angle") or match.group("plain") or ""
        for match in _MARKDOWN_IMAGE_PATTERN.finditer(markdown)
    ]
    sources.extend(match.group("src") for match in _HTML_IMAGE_PATTERN.finditer(markdown))

    report_parent = report_relative.parent
    assets: dict[str, PurePosixPath] = {}
    included_files: set[PurePosixPath] = set()
    rewrites: dict[str, str] = {}
    for raw_source in sources:
        source = raw_source.strip().replace("\\", "/")
        if not source or source.startswith(("http://", "https://", "data:", "blob:", "#")):
            continue
        source_path = source.split("#", 1)[0].split("?", 1)[0]
        pure = PurePosixPath(source_path)
        if ".." in pure.parts:
            raise ArtifactError(f"报告图片路径不安全：{source}", status_code=409)
        if source_path.startswith("/workspace/"):
            try:
                relative = workspace_relative_path(source_path)
            except ValueError as exc:
                raise ArtifactError(f"报告图片路径不安全：{source}", status_code=409) from exc
            archive_path = relative.as_posix()
            rewrites[raw_source] = archive_path
        elif pure.is_absolute():
            raise ArtifactError(f"报告图片必须位于工作区：{source}", status_code=409)
        else:
            relative = report_parent / pure
            archive_path = pure.as_posix()

        try:
            await store.stat(scope, relative)
        except WorkspaceFileNotFound as exc:
            raise ArtifactError(f"报告引用的图片不存在：{source}", status_code=409) from exc
        assets[archive_path] = relative
        included_files.add(relative)

    # Include analysis outputs beside the report even when Markdown does not embed them.
    parent_prefix = None if report_parent == PurePosixPath(".") else report_parent
    for item in await store.list(scope, prefix=parent_prefix):
        relative = workspace_relative_path(item.path)
        if relative == report_relative or relative in included_files:
            continue
        if relative.suffix.lower() not in BUNDLE_COMPANION_SUFFIXES:
            continue
        if report_parent != PurePosixPath(".") and not relative.is_relative_to(
            report_parent
        ):
            continue
        archive_path = (
            relative.relative_to(report_parent).as_posix()
            if report_parent != PurePosixPath(".")
            else relative.as_posix()
        )
        assets[archive_path] = relative
        included_files.add(relative)

    bundled_markdown = markdown
    for original, replacement in rewrites.items():
        bundled_markdown = bundled_markdown.replace(original, replacement)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(report_relative.name, bundled_markdown.encode("utf-8"))
        for archive_path, relative in sorted(assets.items()):
            archive.writestr(archive_path, await store.read(scope, relative))
    return buffer.getvalue(), f"{report_relative.stem}-bundle.zip"


__all__ = [
    "ArtifactError",
    "build_markdown_bundle",
    "resolve_download_object",
    "workspace_artifacts",
]
