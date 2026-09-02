# Redis 限流运维说明

项目使用 `compose.redis.yaml` 构建并管理 `deep-data-research-agent/redis:7.4`。容器名为
`ddra-redis`，数据保存在 `ddra-redis-data` 卷。限流和 Token 桶使用 DB 0；Celery Broker
复用同一容器的 DB 1。两者都不依赖容器 ID、容器 IP，也不把 ACL 密码写入 `.env`。

## Token 桶一致性

- 每个用户的容量为 1 亿 Token，每个自然整点补充 1000 万，已运行任务允许形成负余额。
- PostgreSQL 的 `user_token_buckets` 和 `model_token_usage` 是权威账本；Redis 的
  `ddra:v1:{user-hash}:token-bucket` 只用于快速准入。
- Redis 数据丢失后无需从备份推算 Token 余额：用户下次准入时会从 PostgreSQL 懒加载。
- Redis 不可用时新 run 采用 fail-closed；已准入模型调用继续写 PostgreSQL，Redis 恢复后按
  状态版本同步，旧版本不能覆盖新余额。

## Sandbox 生命周期协调

- 多实例使用 `ddra:v1:{sandbox:<hash>}:lifecycle-lock` 串行化同一用户 thread 的创建和销毁。
- 锁使用随机 owner token、Lua 比较续租/释放；等待或续租失败时 fail-closed。
- Redis HASH 保存各 component 的 Sandbox ID 和配置指纹。AOF 丢失最新记录时，应用通过
  OpenSandbox metadata 找回并接管现有实例，不直接创建重复容器。
- thread 删除期间写入短期墓碑；部分销毁失败保留注册项，重试不会再次处理成功项。

## 首次部署与旧容器切换

在项目根目录执行：

```powershell
.\scripts\setup\redis.ps1
```

脚本会：

1. 创建 `.secrets/redis_password`（64 位随机十六进制密码，已被 Git 忽略）。
2. 校验 Compose 配置。
3. 若检测到本项目旧的 `redis` 容器，直接移除旧容器及 `redis-data` 卷。
4. 构建项目镜像并启动 `ddra-redis`，验证健康检查、匿名访问拒绝以及 `ddra`、
   `ddra-celery` 用户认证。

脚本可以重复执行。不要提交 `.secrets/` 或 `.redis-backups/`。

## 安全与持久化

- 容器内仍监听 6379，宿主机只发布到 `127.0.0.1:16379`，不接受外部网络连接。
- `default` 用户关闭；`ddra` 只能访问 `ddra:*`，`ddra-celery` 只能访问
  `ddra-celery:*`。两个ACL用户复用同一密码文件。
- ACL 只开放 PING、TIME、项目 Lua 脚本及 ZSET、HASH、TTL、锁所需命令。
- Celery消息只保存MongoDB/PostgreSQL任务ID，不保存附件、提示词或邮件正文；业务数据库是
  权威任务状态，Beat每30秒恢复漏发和租约过期任务。
- AOF 使用 `appendfsync everysec`，同时保留 RDB 快照。
- `maxmemory` 为 256 MB，策略为 `noeviction`；内存耗尽时请求会失败，应用返回 503，避免静默
  丢失限流数据。
- 应用启动必须通过 Redis PING；退出时显式关闭异步连接池。

## 验收命令

```powershell
docker compose -f .\compose.redis.yaml config --quiet
docker inspect ddra-redis --format '{{.State.Health.Status}}'
docker exec ddra-redis redis-cli --no-auth-warning PING
docker exec ddra-redis sh -c 'REDISCLI_AUTH="$(cat /run/secrets/redis_password)" redis-cli --user ddra --no-auth-warning PING'
docker exec ddra-redis sh -c 'REDISCLI_AUTH="$(cat /run/secrets/redis_password)" redis-cli --user ddra-celery --no-auth-warning -n 1 PING'
docker exec ddra-redis sh -c 'grep -E "^(appendonly|appendfsync|maxmemory|maxmemory-policy) " /usr/local/etc/redis/redis.conf'
docker exec ddra-redis sh -c 'test -d /data/appendonlydir && echo "AOF directory OK"'
```

预期结果依次为：Compose 校验成功、`healthy`、匿名请求返回 `NOAUTH`、两个认证请求返回 `PONG`，
配置中 AOF 为 `yes`、刷盘策略为 `everysec`、内存策略为 `noeviction`，且 AOF 目录存在。

## 备份与恢复

手工备份可以复用与迁移脚本相同的只读卷挂载方式：

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
docker run --rm --entrypoint sh `
  --volume 'ddra-redis-data:/source:ro' `
  --volume "${PWD}/.redis-backups:/backup" `
  redis:7.4-alpine `
  -c "tar -czf /backup/ddra-redis-data-$stamp.tar.gz -C /source ."
```

恢复会覆盖当前 Redis 数据，只能在明确选定备份文件并停止服务后操作：

```powershell
docker compose -f .\compose.redis.yaml down
# 将 <备份文件名> 替换为 .redis-backups 中已核验的具体文件。
docker run --rm --entrypoint sh `
  --volume 'ddra-redis-data:/target' `
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
