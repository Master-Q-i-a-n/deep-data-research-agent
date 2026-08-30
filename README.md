# Deep Data Research Agent

基于 DeepAgents 的数据研究 Agent MVP。Supervisor 通过同步 `data-analyst` 分析本地表格和
PostgreSQL 只读数据，通过 ASGI 异步 `crawl-worker` 采集公开网页，再核验和整合结果。

Supervisor 可在已联网的沙箱中从公开 URL 下载或按需求创建 Skill，经测试后用单个
`assign_skill` 工具分配给 Supervisor、data-analyst、crawl-worker 中的一个或多个目标，并
持久化到 MongoDB。

## 当前能力

- DeepAgents Supervisor，使用内置 `write_todos` 管理计划。
- 通过官方 `subagents=` 注册的同步 `data-analyst`，负责 CSV、TSV、XLSX 与 PostgreSQL
  只读分析，并返回稳定 JSON 文本契约。
- ASGI co-deployed `crawl-worker`，拥有独立 thread 和上下文。
- Tavily Search、Crawl、Extract 三种采集入口。
- 内置 Supervisor `deep-research` Skill：明确要求深度研究或复杂任务具备多个研究信号时，
  编排多轮检索、证据补缺与冲突核验；单次搜索和单页摘要不触发，也不新增研究子智能体。
- Agent 使用 `OpenSandbox + StateBackend + StoreBackend` 混合后端。
- 每个 Worker thread 独占一个沙箱，成功后导出本地产物快照。
- 沙箱内可执行仅限数据处理和分析用途的 Python 脚本。
- 所有运行时 Skill 从 MongoDB 加载；仓库中的公共 Skill 只作为启动时的版本化种子。
- 公共 Skill 使用 `("public", "skills", agent_name)`，用户 Skill 使用
  `(user_hash, "skills", agent_name)`，按 Agent 隔离并在每次 run 重新加载；同名时用户版本优先。
- Supervisor 读取 `skill-manage` 后通过 `assign_skill` 一步分配 Skill，不创建 Skill 子智能体。
- LangSmith 记录 Agent、模型、工具及沙箱生命周期。
- React 三栏研究工作台，左侧显示用户会话，中间流式展示对话，右侧集中显示计划和异步任务轨迹。
- 可选注册登录；账户、会话、Skill 和检查点按用户隔离。开发环境匿名请求使用共享默认账户，
  生产环境必须登录。
- 账户、thread 归属、邮件投递与 LangGraph checkpoint 统一持久化到 PostgreSQL；Skill 与
  长期记忆继续使用 MongoDB。
- MongoDB 长期记忆只保留当前用户偏好/行为反馈和每个 Agent 独立的公共失败经验；三个
  Agent 均按用户及 Agent namespace 直接加载，写入由显式工具和后台 worker 控制。

当前不包含向量记忆、周期记忆整理和 Playwright 网页采集能力。

## 项目结构

后端采用 Python 标准 `src layout`，并按 API、Agent、准入、数据持久化、工具、Worker 和
外部基础设施分区；React 前端按功能组织。完整目录与依赖方向见
[项目结构说明](docs/architecture/project-structure.md)。

## 配置

项目依赖已经写入 `pyproject.toml`。复制环境变量模板并填写 Tavily、LangSmith 等平台配置：

```powershell
Copy-Item -LiteralPath '.env.example' -Destination '.env'
New-Item -ItemType Directory -Force -Path '.secrets' | Out-Null
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" |
  Set-Content -Encoding ASCII -NoNewline -LiteralPath '.secrets/model_provider_key'
```

登录后在右上角设置中配置模型 Provider、API Base URL、模型名和 API Key。API Key 只提交给
后端一次，并以部署级密钥加密保存；浏览器只保留当前页面中的草稿，不写入本地存储，也不会
附加到 Agent run。通用 OpenAI-compatible 模型需要具备稳定的工具调用能力；DeepSeek
reasoning 模型应明确选择 `DeepSeek` 类型。

环境中的 `OPENAI_*` 仅供本地 CLI 和评测工具使用，在线 Supervisor、Worker、上下文压缩与
后台记忆都读取当前账户的 Provider。服务端运行参数配置如下：

```dotenv
MODEL_PROVIDER_ENCRYPTION_KEY_FILE=.secrets/model_provider_key
MODEL_PROVIDER_HOST_ALLOWLIST=
MODEL_PROVIDER_TIMEOUT_SECONDS=120
MODEL_PROVIDER_TEST_TIMEOUT_SECONDS=20
MODEL_PROVIDER_STREAMING=false
MODEL_PROVIDER_CACHE_SIZE=128
MODEL_PROVIDER_CACHE_TTL_SECONDS=900
TAVILY_API_KEY=tvly-your-api-key
OPEN_SANDBOX_DOMAIN=127.0.0.1:8080
OPEN_SANDBOX_API_KEY=your-opensandbox-api-key
OPEN_SANDBOX_IMAGE=python:3.13-slim
APP_ENV=development
LOCAL_DEV_USER_ID=local-user
MONGODB_URI=mongodb://127.0.0.1:27017
MONGODB_MEMORY_COLLECTION=memories
MONGODB_MEMORY_JOB_COLLECTION=memory_update_jobs
MEMORY_MODEL=
MEMORY_MODEL_TIMEOUT_SECONDS=60
MEMORY_JOB_TIMEOUT_SECONDS=75
FAILURE_REVIEW_MAX_OUTPUT_TOKENS=4096
FAILURE_REVIEW_BUNDLE_MAX_BYTES=262144
FAILURE_REVIEW_PAYLOAD_TTL_HOURS=24
POSTGRES_URI=postgresql://deep_data_research_agent_app:your-password@127.0.0.1:5432/deep_data_research_agent
POSTGRES_APP_POOL_SIZE=5
POSTGRES_APP_MAX_OVERFLOW=10
POSTGRES_CHECKPOINT_POOL_MIN_SIZE=1
POSTGRES_CHECKPOINT_POOL_MAX_SIZE=5
POSTGRES_POOL_TIMEOUT_SECONDS=30
LANGGRAPH_STRICT_MSGPACK=true
AUTH_SESSION_DAYS=7
RATE_LIMIT_KEY_SECRET=
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_USERNAME=ddra
REDIS_PASSWORD_FILE=.secrets/redis_password
REDIS_CONNECT_TIMEOUT_SECONDS=2
REDIS_SOCKET_TIMEOUT_SECONDS=2
REDIS_MAX_CONNECTIONS=20
AUTH_LOGIN_LIMIT=10
AUTH_LOGIN_WINDOW_SECONDS=60
AUTH_REGISTER_LIMIT=3
AUTH_REGISTER_WINDOW_SECONDS=3600
QUESTION_LIMIT=20
QUESTION_WINDOW_SECONDS=60
THREAD_CONCURRENCY_LIMIT=3
TOKEN_BUCKET_CAPACITY=100000000
TOKEN_BUCKET_REFILL_PER_HOUR=10000000
TOKEN_RESERVATION_OUTPUT_TOKENS=8192
RUN_PERMIT_TTL_SECONDS=30
RUN_RESERVATION_TTL_SECONDS=15
RUN_ADMISSION_LOCK_SECONDS=5
```

不要提交 `.env`，也不要把 API Key 作为命令行参数传递。`APP_ENV=production` 时
`RATE_LIMIT_KEY_SECRET` 必须是至少 32 字符的稳定随机值，并且 Provider 加密密钥文件必须
存在且是有效的 Fernet key；生产环境会拒绝所有未登录请求。公网 Provider 默认只允许
HTTPS；内网、保留地址或 HTTP 目标必须由部署方加入 `MODEL_PROVIDER_HOST_ALLOWLIST`。

后端启动前先部署项目专用 Redis。脚本会生成被 Git 忽略的随机 ACL 密码；首次从旧的
`f10fedb99816` 容器切换时，会先把 `redis-data` 备份到 `.redis-backups/`，再由 Compose
接管同名数据卷：

```powershell
.\scripts\setup\redis.ps1
```

Redis 只监听宿主机回环地址，应用使用 `.secrets/redis_password` 认证，不需要把密码写入
真实 `.env`。详细的验收、备份和恢复步骤见 [Redis 运维说明](docs/operations/redis.md)。

如需使用 QQ 邮箱发送报告，在本地 `.env` 中启用固定发件邮箱。附件打包和SMTP提交由
Celery后台执行，`SMTP_PASSWORD` 必须填写QQ邮箱生成的授权码，而不是登录密码：

```dotenv
SMTP_ENABLED=true
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USERNAME=your-account@qq.com
SMTP_PASSWORD=your-qq-mail-authorization-code
SMTP_USE_SSL=true
SMTP_SENDER_NAME=深研
SMTP_TIMEOUT_SECONDS=30
SMTP_MAX_ATTACHMENT_BYTES=20971520
```

账户、登录令牌、thread 归属、邮件投递与 LangGraph checkpoint 统一使用 PostgreSQL。
先在被 Git 忽略的 `.env.postgres-admin` 中写入管理员连接；迁移角色和应用角色密码均可省略，
命令会分别生成并只保存到本地文件：

```dotenv
POSTGRES_ADMIN_URI=postgresql://postgres:admin-password@127.0.0.1:5432/postgres
# POSTGRES_MIGRATOR_PASSWORD=optional-fixed-password
# POSTGRES_APP_PASSWORD=optional-fixed-password
```

停止后端后创建独立迁移/运行角色，通过 Alembic 接管或升级应用表，并用迁移角色初始化官方
PostgreSQL checkpoint 表：

```powershell
uv run setup-agent-postgres
```

存量数据库只有在表、列、主键、外键和索引兼容时才会自动 stamp 基线；不兼容会在转移对象
所有权前中止。该命令不会在终端输出任何凭证，只把运行角色的 `POSTGRES_URI` 与
`LANGGRAPH_STRICT_MSGPACK=true` 写入本地 `.env`。运行角色没有建表或改表权限，应用启动也
不会调用 `create_all()` 或 checkpoint `setup()`。

生成迁移后应先检查模型漂移，再由部署流程升级，不能让 API 或 Celery 自动迁移：

```powershell
$env:POSTGRES_MIGRATION_URI='postgresql://deep_data_research_agent_migrator:<密码>@127.0.0.1:5432/deep_data_research_agent'
uv run alembic check
uv run alembic upgrade head
```

负载均衡探针使用无需认证的 `GET /health/live` 和 `GET /health/ready`。前者只检查进程，后者
并行检查 PostgreSQL 迁移/checkpoint、Redis 与 MongoDB；任何核心依赖失败时返回 503。

动态 Skill 使用 `langgraph-store-mongodb` 提供的全局 LangGraph Store。开发环境无 Bearer
Token 时 LangGraph Auth 注入共享身份 `local-user`；生产环境无 Token 返回 401。注册用户使用
独立 UUID。MongoDB 不配置 TTL 和向量索引，密码使用 Argon2id，登录令牌只以 SHA-256 摘要
写入 PostgreSQL。注册和登录使用 Redis ZSET 滑动窗口；登录按 IP、账户两个维度分别限制
10 次/60 秒，成功和失败均计数。每个用户最多同时运行 3 个 Supervisor 会话、每分钟最多发起
20 个顶层 Supervisor run；内部异步子 Agent 通过服务端签名标记排除。Redis 不可用时保护逻辑
fail-closed，不回退 PostgreSQL。应用只读取 ASGI peer 地址，不直接信任 `X-Forwarded-For`。

自动失败回顾只把当前任务目标、已配对的工具调用/结果和最终结果加入 MongoDB 队列，再由
Celery异步处理。每个
Agent 每轮最多整理三条独立教训；不保存完整提示词、历史消息或 Agent 中间思考，也不依赖模型上下文缓存。登录用户可以在前端设置中
关闭自己的失败经验贡献或清除偏好与行为反馈；关闭贡献不影响已有公共经验的读取，会话、Skill
和产物也不受影响。

如需清空所有用户记忆和失败经验并重新开始，应先停止应用，再执行：

```powershell
uv run reset-agent-memory
```

该命令只清理旧偏好、`memories`、`memory_update_jobs` 和记忆 worker 租约，不会清理用户、
Skill、checkpoint、会话或异步任务。

## 启动 Celery

开发环境在两个独立PowerShell终端启动worker和Beat。Windows使用 `solo` 池，任务按
`memory`、`mail`、`maintenance` 三个逻辑队列路由；业务结果保存在MongoDB/PostgreSQL，
不使用Celery结果后端：

```powershell
$env:PYTHONUTF8='1'
uv run celery -A deep_data_research_agent.workers.app:celery_app worker --pool=solo -Q memory,mail,maintenance --loglevel=INFO
```

```powershell
$env:PYTHONUTF8='1'
uv run celery -A deep_data_research_agent.workers.app:celery_app beat --loglevel=INFO --schedule data/celerybeat-schedule
```

邮件工具返回 `queued` 和 `delivery_id`；可通过认证接口
`GET /email-deliveries/{delivery_id}` 查询 `queued/processing/retry/submitting/sent/failed/uncertain`
状态。生产环境应在Linux上用独立进程管理worker和Beat，并确保同一环境只运行一个Beat。

## 一行启动开发环境

完成 PostgreSQL、MongoDB 和项目 `.env` 的首次配置后，可在仓库根目录一次启动 Redis、
OpenSandbox、LangGraph、Celery Worker/Beat 和前端：

```powershell
.\scripts\dev.ps1
```

脚本会在 Redis 不健康时调用 Compose 初始化，并通过 WSL 的 `Debian` 发行版使用
`~/.sandbox.toml` 启动 OpenSandbox；首次启动时还会安装前端依赖。各进程日志写入
Windows 临时目录的 `deep-data-research-agent/dev-logs/`，避免日志触发 LangGraph 热重载。
脚本会等待 OpenSandbox、LangGraph 和前端端口可用后再报告就绪。按 `Ctrl+C` 会停止本次启动的
开发进程，但保留 Redis 容器。Alembic 迁移仍需
在部署或数据库结构变更时单独执行，不会随开发启动自动运行。发行版或配置路径不同时可传入
`-SandboxDistro` 和 `-SandboxConfig`。开发脚本会显式使用 `APP_ENV=development` 和本地工作区
存储，即使 `.env` 中残留了部署环境的 OSS 选择也不会误连 OSS。

已有外部 Redis 或只需要后端进程时可使用：

```powershell
.\scripts\dev.ps1 -SkipRedis -SkipFrontend
```

仅在需要从本机显式调试 OSS，并且已经配置可用的 OSS 凭据与网络时使用：

```powershell
.\scripts\dev.ps1 -UseOssWorkspace
```

## OpenSandbox

所有 Agent 运行前必须能够连接已经启动的 OpenSandbox Server。当前配置假设服务监听
`127.0.0.1:8080`，并通过 Server Proxy 访问沙箱：

```dotenv
OPEN_SANDBOX_PROTOCOL=http
OPEN_SANDBOX_USE_SERVER_PROXY=true
OPEN_SANDBOX_TIMEOUT_SECONDS=1800
```

每个 thread 按 `supervisor`、`crawl-worker` 组件创建或复用 Agent 沙箱；同步 data-analyst
继承并共享 supervisor 沙箱与工作区。
沙箱失效时会创建新实例，并从配置的本地或 OSS 工作区存储恢复上一次成功快照。
`supervisor` 沙箱已联网（Skill 下载和依赖安装可直接用 `execute`），
`crawl-worker` 保持断网隔离，Tavily 请求始终由宿主进程完成。

Skill 在 `/skill-manage/{name}/` 下创建或下载：下载支持公开 GitHub、HTTPS ZIP/TAR
压缩包或单个 `SKILL.md`；依赖用根级 `requirements.txt` 声明，测试时在沙箱中
`pip install`。压缩包按内容识别格式，并限制路径穿越和链接文件。首次运行前还应确认
相关镜像均已拉取到 Docker。

应用连接 MongoDB Store 时会先幂等迁移旧的 `(user_hash, "skills", "assigned", agent_name)`
namespace，再把仓库公共种子精确同步到各 Agent 的公共 namespace；同步失败会阻止启动。

需要从 MongoDB 手动校验或覆盖同步本地公共 Skill 时，使用维护脚本：

```powershell
uv run python .\scripts\maintenance\sync_skills.py --dry-run
uv run python .\scripts\maintenance\sync_skills.py --yes
```

## 启动后端

```powershell
$env:PYTHONUTF8='1'
uv run langgraph dev --n-jobs-per-worker 4 --no-browser
```

Windows 下显式启用 UTF-8，可规避 `langgraph-api` 读取 OpenAPI 文件时使用 GBK 导致的启动错误。
Windows 下不要添加 `--no-reload`：Uvicorn 0.51 在无 reload 子进程时会创建
`ProactorEventLoop`，而 psycopg 异步连接池要求 `SelectorEventLoop`。保留开发服务器默认的
reload 模式即可使用兼容的 Selector loop；Linux 部署不受该限制。

同一 `langgraph.json` 只注册两个对外图：

- `supervisor`：用户入口。
- `crawl-worker`：Supervisor 通过异步子代理工具启动的后台图。

`data-analyst` 只通过 Supervisor 的同步 `task` 工具调用，不注册公开图，也不产生异步任务
ID。Skill 管理仍是 Supervisor 主图中的直接流程；`crawl-worker` 是异步子智能体。

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
- 停止生成、新建任务及 thread URL 恢复；
- 当前用户的 Supervisor 会话历史、首条任务标题和历史 thread 切换；
- 注册、登录、退出登录和刷新后的登录恢复。
- 用户明确要求时，经确认将 PDF 主报告和完整材料 ZIP 发送到本次提供的单个邮箱。

登录、注册或退出登录后，前端会清除当前 thread 并进入对应身份的新空间。开发环境的默认账户由
所有未登录浏览器共享，不会在登录时把其会话或 Skill 复制到个人账户；生产环境未登录时保留
首页，但会锁定会话、上传和任务操作。

## 交互方式

异步任务通常需要两轮交互：

1. 向 `supervisor` 提交任务，例如“抓取 Tavily 文档中与 Python SDK 有关的页面并总结主要接口”。
2. Supervisor 返回完整 `task_id`，不会立即循环查询。
3. 稍后发送“检查任务 `<task_id>`”；成功后 Supervisor 根据 Worker 结果生成最终报告。

Supervisor 支持 DeepAgents 自动提供的 `check_async_task`、`update_async_task`、
`cancel_async_task` 和 `list_async_tasks`。

Skill 流程已简化为一步分配：

1. 提交要下载的 URL，或描述要创建的 Skill，并指定分配目标（supervisor / data-analyst /
   crawl-worker）。
2. Supervisor 完整读取 `skill-manage`，在 /skill-manage/{name}/ 创建或下载 Skill，
   再用 execute 测试，可按实际结果反复修改和重测。
3. 测试通过后调用 `assign_skill(name, targets)`：文件持久化到 MongoDB active 目录，
   候选目录继续保留，便于检查或分配给其他 Agent。
4. 目标 Agent 在下一轮对话中自动加载该 Skill；公共与用户 Skill 分别恢复到
   `/skills/public/{agent}/active/` 和 `/skills/user/{agent}/active/`。

## 工作区持久化

Agent 执行期间文件始终位于沙箱 `/workspace`。开发环境默认按组件合并导出到本地：

```text
data/users/<user-id>/
└── jobs/<thread-id>/
    ├── supervisor/workspace/final_report.md
    └── crawl-worker/workspace/
        ├── raw/<url-hash>.md
        ├── *_result.json
        ├── *_pages.jsonl
        └── crawl_report.md
```

生产环境使用阿里云 OSS 保存同一份逻辑快照：

```dotenv
WORKSPACE_STORAGE_BACKEND=oss
OSS_REGION=cn-beijing
OSS_ENDPOINT=https://oss-cn-beijing-internal.aliyuncs.com
OSS_BUCKET_NAME=your-private-bucket
OSS_PREFIX=users
OSS_ECS_RAM_ROLE=DeepAgentsECSRole
```

Object Key 为
`users/<user-id>/jobs/<thread-id>/<component>/workspace/<relative-path>`。ECS 通过实例
RAM Role 获取自动刷新的 STS 临时凭证；不要配置长期 AccessKey。沙箱重建时从持久化快照
恢复 `/workspace`，用户上传、报告下载、ZIP 和邮件附件也使用同一存储后端。

checkpoint 已存入 PostgreSQL；切换前已有的 `checkpoints.sqlite*` 仅作为本地回退备份保留，
新版本不会读取或迁移这些文件。

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
# 仅在已绑定 RAM Role 的 ECS 上运行真实 OSS 集成测试：
$env:RUN_OSS_INTEGRATION='1'
uv run pytest tests/test_workspace_store.py -k real_oss
Set-Location -LiteralPath '.\frontend'
npm test -- --run
npm run build
```
