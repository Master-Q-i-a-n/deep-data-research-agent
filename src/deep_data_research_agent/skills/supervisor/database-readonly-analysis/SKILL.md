---
name: database-readonly-analysis
description: 规划并执行 PostgreSQL 只读数据分析，适用于数据库查询、销售分析、指标计算、趋势分析、分群、数据库图表和报告。
---

# 数据库只读分析

本 Skill 指导 Supervisor 完成业务数据分析，不假定 Olist 或其他固定业务指标，不负责数据库管理、写入或索引调优。

## 阶段一：理解任务并规划

1. ①确认业务问题、指标口径、时间范围、分析维度、筛选条件和期望产物。
2. ②缺少会影响正确性的定义时调用 `ask_user`，一次最多询问三个问题；收到补充前停止分析。
3. ③复杂任务使用 `write_todos` 建立“结构探查、查询、验证、分析、输出”计划。

## 阶段二：确认数据库结构

1. ①先调用 `database_list_schemas` 确认数据范围，再用 `database_list_objects` 查找候选表或视图。
2. ②对实际使用的对象调用 `database_get_object_details`，核对字段、类型、主键、外键、约束和索引。
3. ③不得只根据相似字段名猜测关联关系；约束缺失时用小范围查询验证唯一性、覆盖率和关联方向。

## 阶段三：查询与深度分析

1. ①先用 `database_query_preview` 执行小范围探查、计数、日期范围、空值和基数检查。
2. ②明确输入表、连接条件、筛选规则、指标公式和分组粒度，再执行正式聚合查询。
3. ③优先让 PostgreSQL 完成过滤、连接和聚合，不把大表完整放进模型上下文。
4. ④需要统计、图表或复杂处理时，调用 `database_query_to_file` 写入 `/workspace/database/`。
5. ⑤按任务需要编写 `/workspace/scripts/analyze_database_<task>.py`，产物统一写入 `/workspace/output/`。

## 阶段四：验证与报告

1. ①核对主键唯一性、重复值、空值、时间范围、单位和币种。
2. ②检查连接前后行数、连接放大、未匹配记录和一对多关系，避免重复计数。
3. ③检查分组汇总与总计是否一致；验证失败时不得给出确定性结论。
4. ④简单查询直接回答；复杂任务生成图表、分析摘要和 `/workspace/output/final_report.md`。
5. ⑤报告说明指标定义、关键 SQL、验证结果、数据局限和结论依据。
6. ⑥完整阅读 `/skills/supervisor/md-to-pdf/SKILL.md`，默认转换为
   `/workspace/output/final_report.pdf`；转换失败时保留 Markdown 并说明原因。
7. ⑦最终回答列出 PDF、Markdown 和其他实际产物；用户要求下载时调用
   `request_report_download`，传入真实产物路径。

## 注意事项

- 只允许单条 `SELECT` 或 `WITH` 查询，不执行写入、DDL、数据库管理、锁表或任意 Shell 数据库命令。
- MCP 工具失败时按返回错误修正；语法、字段、权限等确定性错误不得原样重试。
- 查询结果超限时先聚合、过滤或拆分，不得把截断数据当作完整数据。
- 数据库任务由 Supervisor 同步处理；只有用户明确要求网页补充时才启动 crawl-worker，并区分两类证据。
- 不在本 Skill 中写死销售、采购、财务等业务规则；具体口径来自用户或相应领域 Skill。
