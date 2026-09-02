# PostgreSQL 迁移与角色分权

应用结构由 Alembic 管理。`deep_data_research_agent_migrator` 持有数据库和 `public` schema，
`deep_data_research_agent_app` 只拥有业务表 DML 与必要序列权限。

## 初始化或升级

在被 Git 忽略的 `.env.postgres-admin` 中配置：

```dotenv
POSTGRES_ADMIN_URI=postgresql://postgres:<管理员密码>@127.0.0.1:5432/postgres
# POSTGRES_MIGRATOR_PASSWORD=可选固定密码
# POSTGRES_APP_PASSWORD=可选固定密码
```

停止旧版本 API 和 Celery，备份数据库后执行：

```powershell
uv run setup-agent-postgres
```

空数据库执行 Alembic `upgrade head`。没有 `alembic_version` 的存量数据库会先检查当前模型
要求的表、列、类型、nullable、主键、外键和索引，兼容才 stamp 基线；不兼容时不会转移对象
所有权。LangGraph checkpoint 的官方迁移仍由 `AsyncPostgresSaver.setup()` 执行，但只存在于
该部署命令中。

## 发布约束

1. 备份 PostgreSQL。
2. 使用迁移凭据运行 `alembic upgrade head` 和 checkpoint setup。
3. 启动 API、Celery worker 和 Beat。
4. 确认 `/health/ready` 返回 200。

运行进程只校验 Alembic head 和 checkpoint 表可读性，并幂等初始化已注册用户的 Token 桶数据；
不会执行任何 DDL。回滚迁移必须人工评估数据影响，不在应用启动或失败恢复中自动 downgrade。
