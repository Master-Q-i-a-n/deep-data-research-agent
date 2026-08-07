---
name: tavily-crawling
description: 使用 Tavily 为网页数据任务选择搜索、站点爬取或 URL 提取方式，并产出可追溯证据。
---

# Tavily 网页采集

## 选择工具

- 主题或问题但没有 URL：先 `tavily_search`，再对关键结果使用 `tavily_extract`。
- 单个站点根 URL：使用 `tavily_crawl`，instructions 写清目标页面和数据。
- 明确 URL 列表：直接使用 `tavily_extract`。

## 工作规则

1. 一次只处理用户分配的当前任务。
2. 优先使用默认参数，不主动扩大页数或深度。
3. 工具返回的是 manifest；正文需要按其中的 `content_path` 使用 `read_file` 读取。
4. 只读取与问题相关的页面，避免把所有正文装入上下文。
5. 对重复、缺失和冲突信息进行说明，不填补网页中不存在的数据。
6. 每个关键结论必须引用来源 URL。
