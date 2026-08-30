---
name: evidence-reporting
description: 将 crawl-worker 的网页证据整理为简要结论和带来源的 Markdown 分析报告。
---

# 证据化报告

## 报告要求

1. 先说明采集范围、时间和数据完整性。
2. 区分网页事实、统计结果和推断，不把推断写成事实。
3. 结论使用 `[1]`、`[2]` 编号引用，编号在全文保持一致。
4. 报告末尾添加 `## 来源`，逐行列出标题和 URL。
5. 明确列出失败页面、样本偏差、时效性和缺失字段。
6. 完整报告写入 `/workspace/output/final_report.md`，图片使用相对于报告的 `charts/...` 路径。
7. 当前任务使用 `deep-research` 时，交付格式以用户要求和该 Skill 为准：未指定格式只生成
   Markdown，明确要求 PDF 时才读取 `md-to-pdf`。其他网页证据报告仍完整阅读
   `/skills/public/supervisor/active/md-to-pdf/SKILL.md`，默认生成
   `/workspace/output/final_report.pdf`；转换失败时保留 Markdown 并说明原因。
8. 最终聊天内容先给 3–5 条简要结论，再列出 PDF、Markdown、图表和数据产物路径。
