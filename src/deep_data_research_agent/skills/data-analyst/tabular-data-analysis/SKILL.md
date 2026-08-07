---
name: tabular-data-analysis
description: 分析 CSV、TSV 或 XLSX 表格文件。用于数据探查、多表关联、清洗统计、生成图表或 Markdown 报告；任务包含 /workspace/input/ 路径或要求分析上传表格时必须使用。
---

# 本地表格数据分析

按以下流程执行；根据真实数据和上级传入的目标选择方法，不在本 Skill 中假定业务指标。

## 理解与探查

1. 用 `ls /workspace/input/` 核对输入，确认目标、指标口径、时间范围、分组维度和期望产物。
2. 缺少会影响正确性的字段、单位、币种、工作表、表头或关联键时停止分析，最终返回
   `needs_input` 并在 `required_inputs` 中列出缺失信息，不得自行假设。
3. 对每个输入执行：
   `python /skills/public/data-analyst/active/tabular-data-analysis/scripts/profile_table.py --input <上传路径> --output /workspace/output/profile_<文件名>.json`
4. 只读取 profile JSON，不把完整表格放进模型消息；关注编码、工作表、表头、字段类型、
   缺失、重复、范围、公式和截断警告。

## 分析与验证

1. 常规表格使用 pandas/openpyxl，大表或 SQL 聚合使用 Polars/DuckDB。
2. 明确输入、清洗规则、指标公式、关联关系和验证方法，再编写本次任务专用的
   `/workspace/scripts/analyze_<task>.py`。
3. `/workspace/input/` 原文件只读；中间结果和最终产物写入 `/workspace/output/`，不得运行时
   安装依赖。
4. 核对输入输出行数、筛选与去重影响、关联未匹配数、汇总与总计、单位币种、缺失和异常值。
5. 验证失败时先修正；无法消除的限制写入 `warnings`，不得给出虚假精确结论。

## 输出

- 按任务需要生成 Markdown、CSV、JSON 和 PNG；不调用下载或用户交互工具。
- 最终仅按 data-analyst 系统提示词返回 JSON 文本，并列出实际存在的产物路径。
- 不静默填充、删行、去重、类型转换或猜测关联键；所有影响结论的处理必须说明并验证。
- Excel 公式只读取已有缓存值；缓存缺失时报告限制，不执行公式、宏、外部链接或嵌入对象。
- 标识符按字符串处理并保留前导零。
