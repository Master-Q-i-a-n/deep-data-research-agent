"""Safe Supervisor artifact discovery, resolution, and Markdown bundling."""

from __future__ import annotations

import io
import mimetypes
import re
import zipfile
from pathlib import Path, PurePosixPath

from deep_data_research_agent import sandbox_manager

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


def workspace_artifacts(root: Path) -> list[dict[str, object]]:
    """List user-facing artifacts without following links outside the snapshot."""

    if not root.is_dir():
        return []
    resolved_root = root.resolve()
    artifacts: list[dict[str, object]] = []
    for candidate in resolved_root.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(resolved_root):
            continue
        relative = resolved.relative_to(resolved_root)
        if relative.parts[0] in {"input", "profile", "raw", "scripts"}:
            continue
        is_output = relative.parts[0] in {"charts", "output"}
        is_report = "report" in resolved.stem.lower()
        if not is_output and not is_report:
            continue
        # Images and data tables belong in the report ZIP rather than the card list.
        if resolved.suffix.lower() not in ARTIFACT_CARD_SUFFIXES:
            continue
        mime_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        artifacts.append(
            {
                "path": f"/workspace/{relative.as_posix()}",
                "filename": resolved.name,
                "size": resolved.stat().st_size,
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


def resolve_download_path(root: Path, virtual_path: str) -> Path:
    """Resolve one virtual workspace path while rejecting traversal and links."""

    try:
        relative = sandbox_manager.workspace_relative_path(virtual_path)
    except ValueError as exc:
        raise ArtifactError(str(exc)) from exc
    if relative.suffix.lower() not in DOWNLOADABLE_SUFFIXES:
        raise ArtifactError("下载文件类型不受支持")

    resolved_root = root.resolve()
    candidate = resolved_root / Path(*relative.parts)
    if candidate.is_symlink():
        raise ArtifactError("文件不存在", status_code=404)
    target = candidate.resolve()
    if not target.is_relative_to(resolved_root):
        raise ArtifactError("下载路径越过工作区")
    if not target.is_file():
        raise ArtifactError("文件不存在", status_code=404)
    return target


def build_markdown_bundle(root: Path, virtual_path: str) -> tuple[bytes, str]:
    """Build a Markdown ZIP containing referenced images and companion data files."""

    report_path = resolve_download_path(root, virtual_path)
    if report_path.suffix.lower() != ".md":
        raise ArtifactError("只有 Markdown 报告支持图片打包下载")

    try:
        markdown = report_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactError("Markdown 报告不是 UTF-8 编码", status_code=409) from exc

    sources = [
        match.group("angle") or match.group("plain") or ""
        for match in _MARKDOWN_IMAGE_PATTERN.finditer(markdown)
    ]
    sources.extend(match.group("src") for match in _HTML_IMAGE_PATTERN.finditer(markdown))

    resolved_root = root.resolve()
    report_parent = report_path.parent.resolve()
    assets: dict[str, Path] = {}
    included_files: set[Path] = set()
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
            relative = sandbox_manager.workspace_relative_path(source_path)
            archive_path = PurePosixPath(*relative.parts).as_posix()
            candidate = resolved_root / Path(*relative.parts)
            # The report is extracted at the ZIP root, where /workspace is absent.
            rewrites[raw_source] = archive_path
        elif pure.is_absolute():
            raise ArtifactError(f"报告图片必须位于工作区：{source}", status_code=409)
        else:
            archive_path = pure.as_posix()
            candidate = report_parent / Path(*pure.parts)

        if candidate.is_symlink():
            raise ArtifactError(f"报告图片不能是符号链接：{source}", status_code=409)
        image_path = candidate.resolve()
        if not image_path.is_relative_to(resolved_root):
            raise ArtifactError(f"报告图片越过工作区：{source}", status_code=409)
        if not image_path.is_file():
            raise ArtifactError(f"报告引用的图片不存在：{source}", status_code=409)
        assets[archive_path] = image_path
        included_files.add(image_path)

    # Include analysis outputs beside the report even when they are not embedded.
    for candidate in report_parent.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if candidate.suffix.lower() not in BUNDLE_COMPANION_SUFFIXES:
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(resolved_root) or resolved in included_files:
            continue
        archive_path = PurePosixPath(*resolved.relative_to(report_parent).parts).as_posix()
        assets[archive_path] = resolved
        included_files.add(resolved)

    bundled_markdown = markdown
    for original, replacement in rewrites.items():
        bundled_markdown = bundled_markdown.replace(original, replacement)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(report_path.name, bundled_markdown.encode("utf-8"))
        for archive_path, asset_path in sorted(assets.items()):
            archive.write(asset_path, archive_path)
    return buffer.getvalue(), f"{report_path.stem}-bundle.zip"
