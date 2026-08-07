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
