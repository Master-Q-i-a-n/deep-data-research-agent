---
name: tabular-data-analysis
description: 对 CSV、TSV 或 XLSX 表格执行分析。quick_answer 处理直接查值和简单统计且不生成文件；formal_report 执行从探查到 Markdown 主报告的完整流程。任务包含 /workspace/input/ 路径或要求分析上传表格时必须使用。
---

# 本地表格数据分析

根据委派中明确标注的 `quick_answer` 或 `formal_report` 模式执行。根据真实数据和上级目标选择
方法，不在本 Skill 中假定业务指标。formal_report 对一次委派负责完整分析生命周期；除非上级
明确限定了可独立验收的子目标，不得只完成探查、部分计算或报告章节就返回。

## 阶段一：理解任务并规划

1. 用 `ls /workspace/input/` 核对输入，确认目标、指标口径、时间范围、分组维度和执行模式。
2. quick_answer 不使用 `write_todos`；formal_report 复杂任务立即用 `write_todos` 建立“探查、
   分析、验证、输出”计划，并随执行更新状态。
3. 缺少会影响正确性的字段、单位、币种、工作表、表头或关联键时停止分析，最终返回
   `needs_input` 并在 `required_inputs` 中列出缺失信息，不得自行假设。

## 阶段二：确定性探查

1. quick_answer 只读取目标所需的表、列和最少数据，使用一次性只读命令直接计算，不生成探查
   文件。formal_report 对每个输入执行：
   `python /skills/public/data-analyst/active/tabular-data-analysis/scripts/profile_table.py --input <上传路径> --output /workspace/output/profile_<文件名>.json`
2. 只读取 profile JSON，不把完整表格放进模型消息；关注编码、工作表、表头、字段类型、
   缺失、重复、范围、公式和截断警告。
3. 多表关联键、单位或表头仍不明确时返回 `needs_input`，不能根据相似列名直接猜测。

## 阶段三：制定并执行分析

1. 常规表格使用 pandas/openpyxl，大表或 SQL 聚合使用 Polars/DuckDB。
2. 明确输入、清洗规则、指标公式、关联关系和验证方法，再编写本次任务专用的
   `/workspace/scripts/analyze_<task>.py`。
3. `/workspace/input/` 原文件只读；中间结果和最终产物写入 `/workspace/output/`，不得运行时
   安装依赖。
4. 执行脚本并根据真实报错修正；不得隐藏失败、伪造结果或把异常当作空数据。

## 阶段四：验证与输出

1. 核对输入输出行数、筛选与去重影响、关联未匹配数、汇总与总计、单位币种、缺失和异常值。
2. 验证失败时先修正；无法消除的限制写入 `warnings`，不得给出虚假精确结论。
3. quick_answer 只在最终 JSON 的 `summary`、`findings` 和 `warnings` 中返回答案、有效样本数、
   口径及必要限制，`artifacts` 必须为空；不得生成 Markdown、CSV、JSON、PNG 或 PDF。
4. formal_report 必须生成分析摘要和主 Markdown 报告，按任务需要生成图表。上级未指定主报告
   路径时使用 `/workspace/output/final_report.md`。主报告说明目标与口径、处理规则、关键发现、
   验证结果、数据局限和产物索引；辅助产物按需
   使用 CSV、JSON 和 PNG。图表放在主报告同目录或子目录，必须用相对于主报告的路径嵌入
   本次生成的每一张 PNG，不能只列出文件名或绝对路径。
5. formal_report 完成前用文件工具核验主报告的全部图片引用及所有声明产物存在。两种模式都仅
   按 data-analyst 系统提示词返回 JSON 文本；只有 formal_report 在 `artifacts` 中列出真实路径。

## 注意事项

- 不生成 PDF，不调用下载或用户交互工具；需要补充信息时只返回 `needs_input`。
- 不静默填充、删行、去重、类型转换或猜测关联键；所有影响结论的处理必须说明并验证。
- Excel 公式只读取已有缓存值；缓存缺失时报告限制，不执行公式、宏、外部链接或嵌入对象。
- `truncated=true` 只限制探查，正式任务脚本仍须按任务要求处理完整数据。
- 标识符按字符串处理并保留前导零。
