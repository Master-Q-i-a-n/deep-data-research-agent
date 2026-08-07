---
name: database-readonly-analysis
description: 规划并执行 PostgreSQL 只读数据分析，适用于数据库查询、指标计算、趋势分析、分群、图表和 Markdown 报告。
---

# 数据库只读分析

本 Skill 不假定固定业务指标，不负责数据库管理、写入或索引调优。

## 理解任务

1. 确认业务问题、指标口径、时间范围、分析维度、筛选条件和期望产物。
2. 缺少会影响正确性的定义时停止分析，最终返回 `needs_input`，并在 `required_inputs` 中
   清楚列出缺失信息，不得自行假设。

## 确认结构与查询

1. 先调用 `database_list_schemas`，再用 `database_list_objects` 查找候选表或视图。
2. 对实际使用的对象调用 `database_get_object_details`，核对字段、类型、主键、外键和约束。
3. 不根据相似字段名猜测关联；约束缺失时用小范围查询验证唯一性、覆盖率和关联方向。
4. 先用 `database_query_preview` 检查计数、日期范围、空值和基数，再执行正式聚合查询。
5. 优先让 PostgreSQL 过滤、连接和聚合；需要进一步统计或制图时，用
   `database_query_to_file` 写入 `/workspace/database/`，任务脚本写入 `/workspace/scripts/`，
   最终产物写入 `/workspace/output/`。

## 验证与输出

1. 核对主键唯一性、重复值、空值、时间范围、单位币种、连接放大、未匹配记录和汇总总计。
2. 验证失败时不得给出确定性结论；工具的语法、字段、权限等确定性错误不得原样重试。
3. 查询结果超限时先聚合、过滤或拆分，不把截断数据当作完整数据。
4. 按需生成 Markdown、CSV、JSON 和 PNG；不生成 PDF，不调用用户交互或下载工具。
5. 最终仅按 data-analyst 系统提示词返回 JSON 文本，报告指标定义、关键 SQL、验证结果、
   数据局限、结论依据和实际存在的产物路径。

只允许单条 `SELECT` 或 `WITH` 查询，不执行写入、DDL、数据库管理、锁表或 Shell 数据库命令。
