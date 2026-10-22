# 运维指南

本文覆盖 PostgreSQL、Redis、RabbitMQ 和可选的 PostgreSQL MCP 数据源。命令均在项目根目录的 PowerShell 中执行；完整配置见 [环境变量模板](../../.env.example)。MongoDB 和 OpenSandbox 需提前准备并确认可用。

## 首次部署

先将 `.env.example` 复制为 `.env` 并完成配置，再依次初始化下面三个基础服务。凭据保存在本地环境文件及 `.secrets/`，不要提交到 Git。

### PostgreSQL

在 `.env.postgres-admin` 中填写管理员连接：

```dotenv
POSTGRES_ADMIN_URI=postgresql://postgres:<管理员密码>@127.0.0.1:5432/postgres
```

```powershell
uv run setup-agent-postgres
```

脚本创建迁移角色和应用角色，执行 Alembic 与 LangGraph checkpoint 初始化，并写入应用连接配置。迁移角色负责结构变更，应用角色只拥有业务读写权限。已有数据库须先停止 API、Celery 并备份；无迁移版本的旧库只有通过结构兼容检查后才会标记基线。

### Redis

```powershell
.\scripts\setup\redis.ps1
```

脚本生成 `.secrets/redis_password`，构建并启动 `ddra-redis`，验证健康状态和认证。**若检测到本项目旧的 `redis` 容器，会删除它及 `redis-data` 卷，切换前须备份。** 当前使用的 `ddra-redis-data` 卷保留。

默认连接为 `127.0.0.1:16379`、用户 `ddra`、DB 0，仅允许访问 `ddra:*`。Redis 用于限流、运行准入和沙箱协调，开启 AOF/RDB 持久化；内存上限 256 MB，满时拒绝写入。

### RabbitMQ

项目复用已有 RabbitMQ，先启动该服务，再初始化项目专用 vhost 和队列：

```powershell
.\scripts\setup\rabbitmq.ps1
# 非默认实例可指定参数，并同步修改 .env 中的连接地址。
# .\scripts\setup\rabbitmq.ps1 -ContainerName rabbitmq -VHost ddra -User guest
```

本地 `.env` 默认配置：

```dotenv
CELERY_BROKER_URL=amqp://guest:guest@127.0.0.1:5672/ddra
```

初始化可重复执行，不清空消息或修改密码。管理界面为 [本地 RabbitMQ](http://127.0.0.1:15672)，选择 `ddra` vhost。生产环境沿用外部 RabbitMQ，需提前初始化 vhost、队列，并配置后端容器可达的地址及实例账号。

## 启动与升级

依赖就绪后启动开发环境：

```powershell
.\scripts\dev.ps1
```

脚本启动 Redis、OpenSandbox、后端、Celery Worker/Beat 和前端，并检查已有 RabbitMQ。默认通过 WSL Debian 启动 OpenSandbox；其他环境需调整。工作台地址为 [本地工作台](http://127.0.0.1:5174)，日志位于 `%TEMP%\deep-data-research-agent\dev-logs/`。

单独运行 Celery 时，在两个终端分别启动 Worker 和 Beat：

```powershell
$env:PYTHONUTF8='1'
uv run celery -A deep_data_research_agent.workers.app:celery_app worker --pool=solo -Q memory,mail,maintenance --loglevel=INFO --without-mingle --without-gossip
```

```powershell
$env:PYTHONUTF8='1'
uv run celery -A deep_data_research_agent.workers.app:celery_app beat --loglevel=INFO --schedule data/celerybeat-schedule
```

Windows 使用 `solo`；关闭 mingle/gossip 可避免 RabbitMQ 4.3 拒绝其内部临时队列。

升级顺序：停止旧 API 和 Celery → 备份 PostgreSQL → 执行 `uv run setup-agent-postgres` 完成迁移 → 启动 API、Worker、Beat → 确认 `/health/ready` 返回 200。应用启动不执行结构迁移；回滚须人工评估数据影响。

### 沙箱预热与 Redis ACL

Supervisor 和 crawl-worker 共用预热池，默认空闲容量为 2、TTL 为 1800 秒、补充间隔为 5 秒。配置项见 `.env.example` 中的 `SANDBOX_POOL_*`；将 `SANDBOX_POOL_MAX_IDLE` 设为 0 可暂停预热，池为空时仍可按需创建。

更新 Redis ACL 后，重新运行 `scripts/setup/redis.ps1` 构建并重建容器，保留当前数据卷。OpenSandbox 服务端需支持 SDK 0.1.15 的 metadata patch API。Redis 协调失败时拒绝分配沙箱。

## 可选：PostgreSQL 只读数据源

PostgreSQL MCP 用于业务数据分析及工作台「数据源与查询」。在 `.env.postgres-mcp` 中配置只读账号：

```dotenv
DATABASE_URI=postgresql://olist_mcp_reader:<密码>@host.docker.internal:5432/olist
```

首次创建容器：

```powershell
docker run -d `
  --name deep-data-postgres-mcp `
  --restart unless-stopped `
  --add-host host.docker.internal:host-gateway `
  --env-file .env.postgres-mcp `
  -p 127.0.0.1:8000:8000 `
  crystaldba/postgres-mcp:latest `
  --access-mode=restricted `
  --transport=sse `
  --sse-host=0.0.0.0 `
  --sse-port=8000
```

随后在 `.env` 中启用并重启应用：

```dotenv
POSTGRES_MCP_ENABLED=true
POSTGRES_MCP_URL=http://127.0.0.1:8000/sse
```

已有容器用 `docker start deep-data-postgres-mcp` 启动。保持数据库只读权限、`restricted` 模式及本机端口绑定；`--add-host` 用于解析宿主机数据库地址。所有登录用户共享该只读账号可访问的数据范围。

接入后检查容器日志，并通过 `database_list_schemas`、`database_list_objects` 和聚合查询验证访问。表和字段的中文说明由数据库注释提供；Olist 注释维护使用 [comment_olist.sql](../../scripts/maintenance/comment_olist.sql)，以表所有者身份在 **olist 数据库**执行。

## 检查与排障

```powershell
# 应用就绪状态应为 200。
(Invoke-WebRequest -Uri 'http://127.0.0.1:2024/health/ready').StatusCode
# Redis 应为 healthy；匿名访问应返回 NOAUTH，认证检查由容器健康检查执行。
docker inspect ddra-redis --format '{{.State.Health.Status}}'
docker exec ddra-redis redis-cli --no-auth-warning PING
# 启用 MCP 时查看连接错误。
docker logs --tail 80 deep-data-postgres-mcp
```

| 现象 | 处理 |
| --- | --- |
| API 未就绪、数据库版本不匹配 | 检查迁移结果、应用角色权限和 checkpoint 表；完成迁移后重启 |
| Redis 连接失败或请求返回 503 | 查看 `docker logs --tail 80 ddra-redis`，检查认证、ACL 和内存；Redis 不可用时新任务会被拒绝 |
| 后台任务不执行 | 检查 RabbitMQ 连接、`ddra` vhost、Worker 和 Beat；开发脚本不会自动启动 RabbitMQ |
| MCP 无法连接数据库 | 检查只读账号权限、宿主机地址映射及容器日志 |
| 数据导出超限 | 缩小筛选范围，或按部署容量调整 `POSTGRES_MCP_EXPORT_ROWS` / `POSTGRES_MCP_EXPORT_BYTES` |

### 任务重试与死信

| 业务队列 | 死信队列 | 用途 |
| --- | --- | --- |
| `memory` | `memory.dead` | 失败经验整理 |
| `mail` | `mail.dead` | 报告邮件发送 |
| `maintenance` | `maintenance.dead` | 周期恢复扫描 |

任务执行后确认消息；可重试错误延迟重试，终态失败进入死信队列。根据消息中的任务 ID 查询 MongoDB/PostgreSQL 错误记录，再按业务状态修复和重新触发，不能直接重放死信。邮件状态为 `uncertain` 时须人工核对是否已送达，避免重复发送。

## 备份与恢复

- PostgreSQL 在升级前备份；Token 余额以其中的账本为准，Redis 丢失后会在下次准入时自动加载。
- Redis 备份对象为 `ddra-redis-data` 卷。为获得一致副本，停止应用写入及 Redis 后备份；恢复时核对备份文件并替换卷内数据，再启动 Redis、检查 `healthy` 后恢复应用。不要删除当前数据卷或提交 `.redis-backups/`。
- 旧 PostgreSQL 表 `rate_limit_buckets` 已停用，至少观察一个发布周期并确认无需回滚后，才可由管理员备份并删除。
- Redis 与 MCP 默认仅绑定本机；跨主机使用 Redis 时配置 TLS 或托管服务，并使用 `rediss://`。
