# PostgreSQL MCP 本地部署

数据库账号只保存在本地 `.env.postgres-mcp`，该文件已被 Git 忽略：

```env
DATABASE_URI=postgresql://olist_mcp_reader:<密码>@host.docker.internal:5432/olist
```

在 PowerShell 中启动 restricted 模式的 SSE 服务：

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

随后在应用 `.env` 中设置：

```env
POSTGRES_MCP_ENABLED=true
POSTGRES_MCP_URL=http://127.0.0.1:8000/sse
```

首次接入需确认 PostgreSQL 18.4 兼容性：检查容器日志，并通过应用的
`database_list_schemas`、`database_list_objects` 和聚合查询完成只读验收。

```powershell
docker logs deep-data-postgres-mcp
docker stop deep-data-postgres-mcp
docker start deep-data-postgres-mcp
```

当前仅绑定 `127.0.0.1`，不要直接暴露到局域网或公网。数据库只读角色和
`--access-mode=restricted` 必须同时保留。`--add-host` 用于确保 MCP 容器可以
解析宿主机上的 PostgreSQL；即使某些 Docker Desktop 版本会自动注入该主机名，
保留此参数也能避免不同环境下出现 `Name or service not known`。

## 业务注释与结构探查

`database_list_schemas` 在 MCP 的 schema 列表中补充 `schema_comment`。
`database_get_object_details` 对表和视图补充 `basic.comment`、各字段的
`comment`，并以系统目录中的实际约束更新 `constraints`。约束包含 `name`、
`type`（PostgreSQL 的 p/u/f/c/x 类型代码）和 `definition`；非空性仍查看
`is_nullable`。两个工具的补充查询均通过原有只读 MCP `execute_sql` 执行。

外层 `result` 仍是字符串，增强成功时内容为 JSON。`metadata_status=complete`
表示补充信息读取成功（注释为 null 代表未填写）；`partial` 时保留原始基础结果，
同时返回 `warnings`，不得将查询失败解释为不存在注释或约束。
基础调用失败仍返回 `status=error`。序列和扩展的详情保持原行为。

Olist 数据字典维护脚本：`scripts/maintenance/comment_olist.sql`。
使用表所有者的管理连接，在 **olist 数据库**执行该文件，例如：

```powershell
psql -h 127.0.0.1 -p 5432 -U <表所有者> -d olist -v ON_ERROR_STOP=1 -f scripts/maintenance/comment_olist.sql
```

凭据使用本机安全配置或交互输入，不写入脚本。脚本在事务内补充 schema、原有
9 张表及 53 个字段的空注释；已有非空注释保留，目标不存在则回滚，可重复执行。
营销两表的注释由导入步骤提供。维护完成后应有 1 条 schema 注释、11 条表注释、
71 条字段注释。管理写入与 Agent 只读查询分别使用各自账号。
