---
name: md-to-pdf
description: 将 Markdown 报告转换为专业排版 PDF，支持中文、表格、代码块、脚注、任务列表、本地 PNG/SVG 图片、自定义 CSS、A4/Letter/Legal/A3、横向页面和页码。用户要求把 Markdown、分析报告或研究报告导出、转换或下载为 PDF 时使用。
---

# Markdown 转 PDF

使用 Python-Markdown 生成 HTML，再由 WeasyPrint 输出 PDF。不得安装 Pandoc、Node.js、Playwright、Chromium、Mermaid CLI 或 KaTeX。

## 执行步骤

1. ①确认输入 Markdown、输出 PDF、页面格式、方向、边距和是否显示页码。
2. ②检查输入文件和 Markdown 中引用的本地图片；相对图片路径必须以输入文件所在目录为基准且真实存在。
3. ③若输出路径已经存在且用户未明确允许覆盖，改用新文件名或先询问用户。
4. ④执行转换脚本：

```bash
python3 /skills/public/supervisor/active/md-to-pdf/scripts/md_to_pdf.py \
  /workspace/output/final_report.md \
  /workspace/output/final_report.pdf \
  --format A4 --header-footer
```

5. ⑤检查退出码和脚本返回的 JSON；失败时根据真实错误修正路径、Markdown 或 CSS 后重试，不得声称已生成。
6. ⑥使用 `pdfinfo` 检查页数和页面尺寸；重要报告再用 `pdftoppm -png` 渲染首页及末页，确认中文、表格和图片无乱码、裁切或重叠。
7. ⑦向用户返回 PDF 实际路径；用户要求保存到本地时调用 `request_report_download`。

## 参数

- `--format`：`A4`、`Letter`、`Legal` 或 `A3`，默认 `A4`。
- `--margin`：单一边距或 `上,右,下,左`，支持 `mm`、`cm`、`in`、`pt`、`px`。
- `--landscape`：横向页面。
- `--header-footer`：显示“第 N 页 / 共 M 页”。
- `--css <path>`：在默认样式后追加 UTF-8 自定义 CSS。
- `--asset-root <path>`：允许读取图片和 CSS 的根目录；默认是输入文件所在目录。

## 注意事项

- 输入与输出必须使用沙箱内绝对路径，正式产物放在 `/workspace/output/`。
- 图片优先使用相对于 Markdown 文件的路径；脚本拒绝读取该目录之外的资源和远程图片。
- PNG、JPEG、GIF、WebP 和 SVG 可直接嵌入；动态图按静态首帧处理。
- Mermaid 和 LaTeX 不会自动执行。先由可信工具转成 SVG/PNG，再在 Markdown 中引用。
- 不执行 Markdown 内的 JavaScript、宏或外部命令。
- 大表允许跨页，表头会重复；不要用超宽列或把完整原始数据塞入报告。
- 不修改输入 Markdown，不静默忽略缺失图片，不把转换成功等同于视觉验收成功。
