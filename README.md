# Deep Data Research Agent

基于 DeepAgents 的网页数据分析 Agent MVP。用户提交自然语言任务后，Supervisor 通过 ASGI
异步子代理启动 `crawl-worker`；Worker 使用 Tavily 搜索、爬取或提取公开网页，整理证据并返回
初步分析，Supervisor 最终生成简要结论和 Markdown 报告。

## 当前能力

- DeepAgents Supervisor，使用内置 `write_todos` 管理计划。
- ASGI co-deployed `crawl-worker`，拥有独立 thread 和上下文。
- Tavily Search、Crawl、Extract 三种采集入口。
- `StateBackend + FilesystemBackend` 混合后端。
- 按 thread 隔离的本地产物目录。
- Supervisor 与 Worker 分别加载只读 Skill。
- LangSmith 自动记录 Agent、模型和工具调用。
- React 研究工作台，流式展示对话、计划、工具调用和异步任务轨迹。

当前不包含长期记忆、HITL、沙箱、登录态网页、Playwright 和生产级认证。

## 配置

项目依赖已经写入 `pyproject.toml`。复制环境变量模板并填写模型、Tavily 和 LangSmith Key：

```powershell
Copy-Item -LiteralPath '.env.example' -Destination '.env'
```

国内 OpenAI-compatible 模型需要具备稳定的工具调用能力。第一阶段建议使用非 reasoning 模型，
并配置：

```dotenv
OPENAI_API_KEY=your-model-api-key
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_MODEL=qwen-plus
TAVILY_API_KEY=tvly-your-api-key
```

不要提交 `.env`，也不要把 API Key 作为命令行参数传递。

## 启动后端

```powershell
$env:PYTHONUTF8='1'
uv run langgraph dev --n-jobs-per-worker 4 --no-browser
```

Windows 下显式启用 UTF-8，可规避 `langgraph-api` 读取 OpenAPI 文件时使用 GBK 导致的启动错误。

同一 `langgraph.json` 注册两个图：

- `supervisor`：用户入口。
- `crawl-worker`：Supervisor 通过异步子代理工具启动的后台图。

## 启动前端

另开一个 PowerShell：

```powershell
Set-Location -LiteralPath '.\frontend'
Copy-Item -LiteralPath '.env.example' -Destination '.env.local'
npm install
npm run dev
```

浏览器访问 `http://127.0.0.1:5174`。前端默认连接 `http://127.0.0.1:2024` 的
`supervisor` 图；需要修改时编辑 `frontend/.env.local`。界面支持：

- 自然语言任务提交与流式 Markdown 报告；
- DeepAgents todo 计划与 `async_tasks` 任务轨迹；
- 工具调用输入、结果和运行状态；
- 停止生成、新建任务及 thread URL 恢复。

## 交互方式

异步任务通常需要两轮交互：

1. 向 `supervisor` 提交任务，例如“抓取 Tavily 文档中与 Python SDK 有关的页面并总结主要接口”。
2. Supervisor 返回完整 `task_id`，不会立即循环查询。
3. 稍后发送“检查任务 `<task_id>`”；成功后 Supervisor 根据 Worker 结果生成最终报告。

Supervisor 支持 DeepAgents 自动提供的 `check_async_task`、`update_async_task`、
`cancel_async_task` 和 `list_async_tasks`。

## 本地产物

每个 graph thread 的文件写入：

```text
data/jobs/<thread-id>/workspace/
├── raw/<url-hash>.md
├── search_result.json / crawl_result.json / extract_result.json
├── *_pages.jsonl
├── crawl_report.md
└── final_report.md
```

`data/` 仅用于本地 MVP，已经加入 `.gitignore`。

## LangSmith

设置以下变量后，LangChain、DeepAgents 和工具调用会自动写入指定项目：

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your-langsmith-api-key
LANGSMITH_PROJECT=deep-data-research-agent-mvp
```

Supervisor 和异步 Worker 是两个独立 trace，可使用 `task_id`（即 Worker thread ID）关联。

## 检查

```powershell
uv run ruff check .
uv run pytest
Set-Location -LiteralPath '.\frontend'
npm test -- --run
npm run build
```
