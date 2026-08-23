# Redis 限流运维说明

项目使用 `compose.redis.yaml` 管理独立 Redis 7.4，数据保存在外部卷 `redis-data`。应用始终连接
`redis://127.0.0.1:6379/0`，不依赖容器 ID、容器 IP，也不把 ACL 密码写入 `.env`。

## Token 桶一致性

- 每个用户的容量为 1 亿 Token，每个自然整点补充 1000 万，已运行任务允许形成负余额。
- PostgreSQL 的 `user_token_buckets` 和 `model_token_usage` 是权威账本；Redis 的
  `ddra:v1:{user-hash}:token-bucket` 只用于快速准入。
- Redis 数据丢失后无需从备份推算 Token 余额：用户下次准入时会从 PostgreSQL 懒加载。
- Redis 不可用时新 run 采用 fail-closed；已准入模型调用继续写 PostgreSQL，Redis 恢复后按
  状态版本同步，旧版本不能覆盖新余额。

## 首次部署与旧容器切换

在项目根目录执行：

```powershell
.\scripts\setup_redis.ps1
```

脚本会：

1. 创建 `.secrets/redis_password`（64 位随机十六进制密码，已被 Git 忽略）。
2. 校验 Compose 配置。
3. 若检测到旧容器 `f10fedb99816`，先把 `redis-data` 完整备份到
   `.redis-backups/redis-data-<时间>.tar.gz`，备份成功后才停止并移除旧容器。
4. 启动 Compose 服务，并验证健康检查、匿名访问拒绝和 `ddra` 用户认证。

脚本可以重复执行；Compose 已接管容器后不会重复迁移数据卷。不要提交 `.secrets/` 或
`.redis-backups/`。

## 安全与持久化

- 端口只发布到 `127.0.0.1:6379`，不接受外部网络连接。
- `default` 用户关闭，仅启用 `ddra`，且只能访问 `ddra:*` key。
- ACL 只开放 PING、TIME、项目 Lua 脚本及 ZSET、HASH、TTL、锁所需命令。
- AOF 使用 `appendfsync everysec`，同时保留 RDB 快照。
- `maxmemory` 为 256 MB，策略为 `noeviction`；内存耗尽时请求会失败，应用返回 503，避免静默
  丢失限流数据。
- 应用启动必须通过 Redis PING；退出时显式关闭异步连接池。

## 验收命令

```powershell
docker compose -f .\compose.redis.yaml config --quiet
docker inspect redis --format '{{.State.Health.Status}}'
docker exec redis redis-cli --no-auth-warning PING
docker exec redis sh -c 'REDISCLI_AUTH="$(cat /run/secrets/redis_password)" redis-cli --user ddra --no-auth-warning PING'
docker exec redis sh -c 'grep -E "^(appendonly|appendfsync|maxmemory|maxmemory-policy) " /usr/local/etc/redis/redis.conf'
docker exec redis sh -c 'test -d /data/appendonlydir && echo "AOF directory OK"'
```

预期结果依次为：Compose 校验成功、`healthy`、匿名请求返回 `NOAUTH`、认证请求返回 `PONG`，
配置中 AOF 为 `yes`、刷盘策略为 `everysec`、内存策略为 `noeviction`，且 AOF 目录存在。

## 备份与恢复

手工备份可以复用与迁移脚本相同的只读卷挂载方式：

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
docker run --rm --entrypoint sh `
  --volume 'redis-data:/source:ro' `
  --volume "${PWD}/.redis-backups:/backup" `
  redis:7.4-alpine `
  -c "tar -czf /backup/redis-data-$stamp.tar.gz -C /source ."
```

恢复会覆盖当前 Redis 数据，只能在明确选定备份文件并停止服务后操作：

```powershell
docker compose -f .\compose.redis.yaml down
# 将 <备份文件名> 替换为 .redis-backups 中已核验的具体文件。
docker run --rm --entrypoint sh `
  --volume 'redis-data:/target' `
  --volume "${PWD}/.redis-backups:/backup:ro" `
  redis:7.4-alpine `
  -c 'rm -rf /target/* && tar -xzf /backup/<备份文件名> -C /target'
docker compose -f .\compose.redis.yaml up -d
```

恢复后重新执行验收命令，并确认容器进入 `healthy`。

## PostgreSQL 旧表清理

切换不会迁移正在活动的固定窗口，Redis 限额从部署时重新开始。旧表
`rate_limit_buckets` 暂时保留且应用不再读写。Redis 稳定运行至少一个发布周期、确认无需回滚后，
由数据库管理员人工备份并删除该表；部署脚本不会自动执行 `DROP TABLE`。

跨主机部署不应暴露当前明文端口，应改用 TLS 终端或托管 Redis，并将 `REDIS_URL` 改为
`rediss://`。
