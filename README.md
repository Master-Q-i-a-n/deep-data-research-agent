# Deep Data Research Agent

基于 DeepAgents 的网页数据分析 Agent MVP。用户提交自然语言任务后，Supervisor 通过 ASGI
异步子代理启动 `crawl-worker`；Worker 使用 Tavily 搜索、爬取或提取公开网页，整理证据并返回
初步分析，Supervisor 最终生成简要结论和 Markdown 报告。

Supervisor 可在已联网的沙箱中从公开 URL 下载或按需求创建 Skill，经测试后用单个
`assign_skill` 工具一步分配给 Supervisor、crawl-worker 或两者，并持久化到 MongoDB。

## 当前能力

- DeepAgents Supervisor，使用内置 `write_todos` 管理计划。
- ASGI co-deployed `crawl-worker`，拥有独立 thread 和上下文。
- Tavily Search、Crawl、Extract 三种采集入口。
- Worker 使用 `OpenSandbox + StateBackend + FilesystemBackend` 混合后端。
- 每个 Worker thread 独占一个沙箱，成功后导出本地产物快照。
- 沙箱内可执行仅限数据处理和分析用途的 Python 脚本。
- Supervisor 与 Worker 分别加载只读 Skill。
- 用户动态 Skill 使用 MongoDB `StoreBackend` 按用户和 Agent 隔离，并在每次 run 重新加载。
- Supervisor 读取 `skill-manage` 后通过 `assign_skill` 一步分配 Skill，不创建 Skill 子智能体。
- LangSmith 记录 Agent、模型、工具及沙箱生命周期。
- React 研究工作台，流式展示对话、计划、工具调用和异步任务轨迹。

当前不包含通用长期记忆、LangGraph interrupt、登录态网页、Playwright 和完整登录系统。

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
OPEN_SANDBOX_DOMAIN=127.0.0.1:8080
OPEN_SANDBOX_API_KEY=your-opensandbox-api-key
OPEN_SANDBOX_IMAGE=python:3.13-slim
APP_ENV=development
LOCAL_DEV_USER_ID=local-user
MONGODB_URI=mongodb://127.0.0.1:27017
```

不要提交 `.env`，也不要把 API Key 作为命令行参数传递。

动态 Skill 使用 `langgraph-store-mongodb` 提供的全局 LangGraph Store。开发环境没有认证
用户时使用 `LOCAL_DEV_USER_ID`；`APP_ENV=production` 时如果 LangGraph Server 没有注入
认证用户身份，用户级 Skill 读写会明确失败。MongoDB 不配置 TTL 和向量索引。

## OpenSandbox

所有 Agent 运行前必须能够连接已经启动的 OpenSandbox Server。当前配置假设服务监听
`127.0.0.1:8080`，并通过 Server Proxy 访问沙箱：

```dotenv
OPEN_SANDBOX_PROTOCOL=http
OPEN_SANDBOX_USE_SERVER_PROXY=true
OPEN_SANDBOX_TIMEOUT_SECONDS=1800
```

每个 thread 按 `supervisor`、`crawl-worker` 组件创建或复用 Agent 沙箱。
沙箱失效时会创建新实例，并从 `data/jobs/<thread-id>/<component>/workspace/` 恢复上一次
成功快照。`supervisor` 沙箱已联网（Skill 下载和依赖安装可直接用 `execute`），
`crawl-worker` 保持断网隔离，Tavily 请求始终由宿主进程完成。

Skill 在 `/skills/main/{name}/` 下创建或下载：下载支持公开 GitHub、HTTPS ZIP/TAR
压缩包或单个 `SKILL.md`；依赖用根级 `requirements.txt` 声明，测试时在沙箱中
`pip install`。压缩包按内容识别格式，并限制路径穿越和链接文件。首次运行前还应确认
相关镜像均已拉取到 Docker。

## 启动后端

```powershell
$env:PYTHONUTF8='1'
uv run langgraph dev --n-jobs-per-worker 4 --no-browser
```

Windows 下显式启用 UTF-8，可规避 `langgraph-api` 读取 OpenAPI 文件时使用 GBK 导致的启动错误。

同一 `langgraph.json` 只注册两个对外图：

- `supervisor`：用户入口。
- `crawl-worker`：Supervisor 通过异步子代理工具启动的后台图。

Skill 管理不注册独立图，也不使用同步 `task`；它作为 Supervisor 主图中的 Skill 流程
执行。`crawl-worker` 仍是唯一的异步子智能体。

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

Skill 流程已简化为一步分配：

1. 提交要下载的 URL，或描述要创建的 Skill，并指定分配目标（supervisor / crawl-worker）。
2. Supervisor 完整读取 `skill-manage`，在 /skills/main/{name}/ 创建或下载 Skill，
   再用 execute 测试，可按实际结果反复修改和重测。
3. 测试通过后调用 `assign_skill(name, targets)`：文件持久化到 MongoDB active 目录，
   并清理临时目录。
4. 目标 Agent 在下一轮对话中自动加载该 Skill（恢复至 /persisted-skills/）。

## 本地产物

Agent 执行期间文件位于沙箱 `/workspace`；任务成功后按组件合并导出到：

```text
data/jobs/<thread-id>/
├── supervisor/workspace/final_report.md
└── crawl-worker/workspace/
    ├── raw/<url-hash>.md
    ├── *_result.json
    ├── *_pages.jsonl
    └── crawl_report.md
```

Worker 采集产物位于异步任务 ID 对应的组件目录。`data/` 仅用于本地 MVP，已经加入
`.gitignore`。

## LangSmith

设置以下变量后，LangChain、DeepAgents 和工具调用会自动写入指定项目：

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your-langsmith-api-key
LANGSMITH_PROJECT=deep-data-research-agent-mvp
```

Supervisor 和异步 crawl-worker 是两个独立 trace，可使用 `task_id`（即 Worker thread ID）
关联。Skill 管理工具直接嵌套在 Supervisor trace 下。Agent trace 中还会出现
`sandbox.ensure`、`sandbox.restore`、
`sandbox.skills.sync` 和 `sandbox.export`；这些 span 不记录密钥或文件正文。

Skill trace 还包括 `assign_skill` 工具调用（直接嵌套在 Supervisor trace 下）。

## 检查

```powershell
uv run ruff check .
uv run pytest
# 需要真实 OpenSandbox Server 时再运行：
$env:RUN_OPENSANDBOX_INTEGRATION='1'
uv run pytest tests/test_sandbox_manager.py -k real_opensandbox
Set-Location -LiteralPath '.\frontend'
npm test -- --run
npm run build
```
