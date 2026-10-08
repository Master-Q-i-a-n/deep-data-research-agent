# RabbitMQ 任务队列

项目复用已有 RabbitMQ，不通过项目 Compose 创建或重建容器。本地默认连接：

```dotenv
CELERY_BROKER_URL=amqp://guest:guest@127.0.0.1:5672/ddra
```

账号和密码使用现有容器配置。项目通过 `ddra` vhost 隔离队列、交换机和权限。
Redis 继续用于限流、运行准入、沙箱注册和共享失败经验写入锁；个人偏好同步写入
MongoDB，不经过后台队列。

## 初始化与启动

先启动已有容器，再运行一次初始化脚本：

```powershell
.\scripts\setup\rabbitmq.ps1
# 容器名、vhost 或账号不同时可显式指定；同时调整 .env 的连接地址。
.\scripts\setup\rabbitmq.ps1 -ContainerName rabbitmq -VHost ddra -User guest
```

脚本复用账号，只创建缺失的 vhost、授予该 vhost 的权限，并声明六个持久化 classic
队列；重复执行不会清空消息或修改密码。开发启动脚本会验证 AMQP 连接并检查队列，
RabbitMQ 不可用时停止启动，不自动管理容器。直接启动 Worker 时也要先完成初始化。

```powershell
uv run celery -A deep_data_research_agent.workers.app:celery_app worker --pool=solo -Q memory,mail,maintenance --loglevel=INFO --without-mingle --without-gossip
uv run celery -A deep_data_research_agent.workers.app:celery_app beat --loglevel=INFO --schedule data/celerybeat-schedule
```

本地 Worker 关闭 mingle 和 gossip，避免其内部临时非独占队列被 RabbitMQ 4.3
拒绝；业务任务执行、持久化及完成后 ACK 不受影响。

管理界面通常位于 `http://127.0.0.1:15672`，选择 `ddra` vhost 查看队列。

## ACK、重试与死信

业务任务通过 direct 交换机 `ddra` 路由，消息只包含数据库任务 ID。发布使用持久化
消息和 publisher confirm，Worker 在任务执行后确认。数据库仍保存业务结果，没有
Celery result backend。

| 业务队列 | 死信队列 | 用途 |
| --- | --- | --- |
| `memory` | `memory.dead` | 失败经验回顾与整理 |
| `mail` | `mail.dead` | 报告邮件发送 |
| `maintenance` | `maintenance.dead` | 周期恢复扫描 |

死信交换机为 `ddra.dead`。死信队列持久化，且不在 Worker 的消费列表中。
发布任务前也会声明死信目的地，避免只存在业务队列而没有死信路由。

- 成功、业务取消、已处理的重复投递：正常 ACK。
- 可安全重试的模型或邮件错误：先记录数据库重试状态，再由 Celery 延迟重试。
- 记忆或邮件的终态失败／重试耗尽：先保存数据库失败状态，再拒绝原消息且不重新
  入队，由 RabbitMQ 转入对应死信队列。
- 未捕获异常：延迟确认任务拒绝消息，转入死信；持久化任务仍可由 Beat 的补偿扫描
  恢复。维护任务失败则进入 `maintenance.dead`，下一次定时扫描继续执行。
- Worker 异常中断：未确认消息允许重新投递；数据库 claim 与业务状态继续控制重复
  处理，不能以 ACK 代替幂等性。
- 邮件 SMTP 提交结果不确定：保留数据库 `uncertain` 状态并 ACK，不进入自动重试。

查看死信时，用消息中的任务 ID 查询 MongoDB／PostgreSQL 的错误记录。死信不是自动
重放入口；尤其不能直接重放邮件消息。已标记 `failed` 的任务不会仅靠重新发布消息
再次执行，修复和重新触发需要按业务状态处理。

## 最小生产衔接

生产 Compose 同样连接外部管理的 RabbitMQ，仅透传 `CELERY_BROKER_URL`。
连接地址必须从后端容器可达；同一宿主机可使用 `host.docker.internal:5672`。
在目标实例预先初始化 vhost 和队列，账号访问规则由已有实例配置决定。
本次不新增生产 RabbitMQ 服务，也不调整生产 Worker pool、集群或监控部署。

## 验证

默认测试无需 RabbitMQ。真实消费、延迟 ACK、重试和死信路由测试使用临时测试
vhost，结束后仅清理该测试 vhost，不影响 `ddra`：

```powershell
$env:RUN_RABBITMQ_INTEGRATION='1'
uv run pytest tests/test_rabbitmq.py -q
```

默认使用应用连接账号及 `http://127.0.0.1:15672` 管理 API；测试账号需要有创建临时
vhost 的权限。非默认地址可设置 `TEST_RABBITMQ_MANAGEMENT_URL`。
