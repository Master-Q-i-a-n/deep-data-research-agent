#!/usr/bin/env python3
"""使用 Python-Markdown 和 WeasyPrint 将 Markdown 转换为 PDF。"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import markdown
from weasyprint import HTML, default_url_fetcher

_MARGIN_RE = re.compile(r"^\d+(?:\.\d+)?(?:mm|cm|in|pt|px)$", re.IGNORECASE)
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))",
    re.IGNORECASE,
)
_HTML_IMAGE_RE = re.compile(
    r"<img\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

_DEFAULT_CSS = r"""
@page {
  size: __PAGE_SIZE__;
  margin: __MARGINS__;
}
__FOOTER__
html {
  color: #1e293b;
  background: #ffffff;
  font-family: "Noto Sans CJK SC", "Noto Sans CJK", sans-serif;
  font-size: 10.5pt;
  line-height: 1.7;
}
body { margin: 0; padding: 0; overflow-wrap: break-word; }
h1, h2, h3, h4, h5, h6 {
  color: #163b5c;
  font-weight: 700;
  line-height: 1.3;
  break-after: avoid;
}
h1 {
  margin: 0 0 0.8em;
  padding-bottom: 0.3em;
  border-bottom: 2px solid #38bdf8;
  font-size: 24pt;
}
h2 { margin: 1.35em 0 0.55em; font-size: 16pt; color: #155e75; }
h3 { margin: 1.1em 0 0.4em; font-size: 12.5pt; color: #334155; }
p { margin: 0.55em 0; }
a { color: #0369a1; text-decoration: none; }
ul, ol { margin: 0.55em 0; padding-left: 1.8em; }
li { margin: 0.2em 0; }
blockquote {
  margin: 0.9em 0;
  padding: 0.55em 1em;
  border-left: 4px solid #38bdf8;
  background: #f0f9ff;
  color: #334155;
  break-inside: avoid;
}
table {
  width: 100%;
  margin: 0.9em 0 1.1em;
  border-collapse: collapse;
  font-size: 9.3pt;
}
thead { display: table-header-group; background: #e0f2fe; }
tr { break-inside: avoid; }
th, td {
  padding: 0.45em 0.6em;
  border: 1px solid #94a3b8;
  vertical-align: top;
}
th { font-weight: 700; text-align: left; }
tbody tr:nth-child(even) { background: #f8fafc; }
code {
  font-family: "DejaVu Sans Mono", monospace;
  font-size: 0.9em;
  background: #f1f5f9;
  border-radius: 3px;
  padding: 0.08em 0.3em;
}
pre {
  margin: 0.9em 0;
  padding: 0.85em 1em;
  color: #e2e8f0;
  background: #0f172a;
  border-radius: 5px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  break-inside: avoid;
}
pre code { padding: 0; color: inherit; background: transparent; }
img, svg {
  display: block;
  max-width: 100%;
  max-height: 225mm;
  height: auto;
  margin: 0.8em auto;
  break-inside: avoid;
}
figure { margin: 1em 0; break-inside: avoid; }
figcaption { color: #64748b; font-size: 9pt; text-align: center; }
hr { margin: 1.5em 0; border: 0; border-top: 1px solid #cbd5e1; }
.footnote, .footnotes { font-size: 9pt; }
.task-list-item { list-style-type: none; }
.task-list-control { margin-right: 0.35em; }
.page-break { break-before: page; }
"""


def _parse_margins(value: str) -> tuple[str, str, str, str]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) == 1:
        parts *= 4
    if len(parts) != 4 or any(not _MARGIN_RE.fullmatch(part) for part in parts):
        raise ValueError(
            "边距必须为单一值或“上,右,下,左”，并使用 mm、cm、in、pt 或 px 单位"
        )
    return tuple(parts)  # type: ignore[return-value]


def _frontmatter(source: str, fallback_title: str) -> tuple[str, str]:
    match = _FRONTMATTER_RE.match(source)
    if not match:
        return fallback_title, source
    title_match = re.search(
        r"^title\s*:\s*(.+?)\s*$",
        match.group(1),
        re.IGNORECASE | re.MULTILINE,
    )
    title = fallback_title
    if title_match:
        title = title_match.group(1).strip().strip("'\"") or fallback_title
    return title, source[match.end() :]


def _resolve_asset(reference: str, base_dir: Path, asset_root: Path) -> Path | None:
    value = html.unescape(reference).strip()
    parsed = urlparse(value)
    if parsed.scheme == "data":
        return None
    if parsed.scheme:
        raise ValueError(f"不允许远程或非文件图片：{reference}")
    path = Path(unquote(parsed.path))
    resolved = (path if path.is_absolute() else base_dir / path).resolve()
    if not resolved.is_relative_to(asset_root):
        raise ValueError(f"图片路径越过资源根目录：{reference}")
    if not resolved.is_file():
        raise FileNotFoundError(f"图片不存在：{resolved}")
    return resolved


def _validate_images(source: str, base_dir: Path, asset_root: Path) -> None:
    references = [left or right for left, right in _MARKDOWN_IMAGE_RE.findall(source)]
    references.extend(_HTML_IMAGE_RE.findall(source))
    for reference in dict.fromkeys(references):
        _resolve_asset(reference, base_dir, asset_root)


def _local_url_fetcher(asset_root: Path):
    def fetch(url: str):
        parsed = urlparse(url)
        if parsed.scheme == "data":
            return default_url_fetcher(url)
        if parsed.scheme != "file":
            raise ValueError(f"PDF 渲染禁止访问远程资源：{url}")
        resolved = Path(url2pathname(unquote(parsed.path))).resolve()
        if not resolved.is_relative_to(asset_root) or not resolved.is_file():
            raise ValueError(f"PDF 资源不在允许目录内或不存在：{resolved}")
        return default_url_fetcher(url)

    return fetch


def _document_css(
    page_format: str,
    margins: tuple[str, str, str, str],
    landscape: bool,
    header_footer: bool,
    custom_css: str,
) -> str:
    page_size = f"{page_format} landscape" if landscape else page_format
    footer = ""
    if header_footer:
        footer = r"""
@page {
  @bottom-center {
    content: "第 " counter(page) " 页 / 共 " counter(pages) " 页";
    color: #64748b;
    font-family: "Noto Sans CJK SC", sans-serif;
    font-size: 9pt;
  }
}
"""
    css = (
        _DEFAULT_CSS.replace("__PAGE_SIZE__", page_size)
        .replace("__MARGINS__", " ".join(margins))
        .replace("__FOOTER__", footer)
    )
    return f"{css}\n{custom_css}" if custom_css else css


def convert(args: argparse.Namespace) -> dict[str, object]:
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.is_absolute() or not output_path.is_absolute():
        raise ValueError("输入和输出必须使用绝对路径")
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"输入 Markdown 不存在：{input_path}")

    asset_root = Path(args.asset_root).resolve() if args.asset_root else input_path.parent
    if not input_path.is_relative_to(asset_root):
        raise ValueError("输入 Markdown 必须位于资源根目录内")

    source = input_path.read_text(encoding="utf-8")
    title, markdown_source = _frontmatter(source, input_path.stem)
    _validate_images(markdown_source, input_path.parent, asset_root)

    custom_css = ""
    if args.css:
        css_path = Path(args.css).resolve()
        if not css_path.is_relative_to(asset_root) or not css_path.is_file():
            raise ValueError("自定义 CSS 必须是资源根目录内的现有文件")
        custom_css = css_path.read_text(encoding="utf-8")

    body = markdown.markdown(
        markdown_source,
        extensions=[
            "extra",
            "sane_lists",
            "pymdownx.superfences",
            "pymdownx.tasklist",
            "pymdownx.tilde",
        ],
        extension_configs={"pymdownx.tasklist": {"custom_checkbox": True}},
        output_format="html5",
    )
    css = _document_css(
        args.page_format,
        _parse_margins(args.margin),
        args.landscape,
        args.header_footer,
        custom_css,
    )
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>{css}</style>
</head>
<body>{body}</body>
</html>"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(
        string=document,
        base_url=input_path.parent,
        url_fetcher=_local_url_fetcher(asset_root),
    ).write_pdf(output_path)
    return {
        "status": "success",
        "input": str(input_path),
        "output": str(output_path),
        "bytes": output_path.stat().st_size,
        "renderer": "WeasyPrint",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用 WeasyPrint 将 Markdown 转换为 PDF")
    parser.add_argument("input", help="输入 Markdown 绝对路径")
    parser.add_argument("output", help="输出 PDF 绝对路径")
    parser.add_argument(
        "--format",
        dest="page_format",
        default="A4",
        choices=["A4", "Letter", "Legal", "A3"],
        help="页面格式，默认 A4",
    )
    parser.add_argument(
        "--margin",
        default="18mm,16mm,20mm,16mm",
        help="单一边距或上,右,下,左",
    )
    parser.add_argument("--landscape", action="store_true", help="使用横向页面")
    parser.add_argument("--header-footer", action="store_true", help="显示页码")
    parser.add_argument("--css", help="追加的 UTF-8 CSS 文件")
    parser.add_argument(
        "--asset-root",
        help="允许读取图片和 CSS 的资源根目录；默认是输入文件所在目录",
    )
    # 接受旧版参数，避免历史对话中的命令直接失败；新引擎始终不执行 Mermaid/KaTeX。
    parser.add_argument("--no-mermaid", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-math", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    try:
        result = convert(_parser().parse_args())
    # CLI 边界统一把第三方渲染异常转换成可供 Agent 判断的 JSON 错误。
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


