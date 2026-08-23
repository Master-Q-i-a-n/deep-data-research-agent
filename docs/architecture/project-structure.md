# 项目结构

本项目保留 Python 标准的 `src layout`。`src/deep_data_research_agent` 就是后端代码，
不再额外增加一层 `backend/`。

```text
src/deep_data_research_agent/
├─ api/                 FastAPI 应用、认证钩子和请求模型
├─ agents/              Supervisor、crawl-worker、契约和中间件
├─ admissions/          Redis 限流、run 准入和 Token 计量
├─ artifacts/           报告产物解析与打包
├─ core/                配置和用户身份等共享基础能力
├─ database/            应用 PostgreSQL 模型、连接和事务操作
├─ evaluation/          离线评测入口
├─ infrastructure/      MongoDB、Sandbox、checkpointer 等外部适配
├─ memory/              用户记忆、失败回顾和持久任务状态
├─ skill_system/        Skill 存储与同步
├─ skills/              版本化内置 Skill 资源
├─ tools/               暴露给 Agent 的工具
└─ workers/             Celery 应用、发布器和分队列任务
```

仓库级目录：

```text
frontend/               React 前端，按 features 分组
infra/                  Redis、PostgreSQL、Sandbox 部署配置
scripts/setup/          本地服务初始化脚本
scripts/maintenance/    数据同步和维护脚本
docs/architecture/      架构说明和决策
docs/operations/        部署、恢复和外部服务说明
evals/                  评测清单
tests/                  Python 回归测试
```

## 依赖方向

- `core` 不依赖 API、Agent 或 Worker。
- `tools` 可以调用功能模块和基础设施，但不能导入 API 路由。
- `agents` 组合 tools、memory、admissions 和 infrastructure。
- `api` 负责认证、校验和 HTTP 错误转换，不承载 Celery 任务实现。
- `workers/tasks` 只负责认领、调用、结算和重试，业务状态必须先持久化。
- `infrastructure` 不应反向导入 `api`。

`database`、`memory` 和 `artifacts` 保持功能包形式。它们当前调用面较广，后续拆分时应
先建立稳定的 repository/service 接口，再移动内部实现，避免只为目录层级制造转发文件。

## 稳定入口

- LangGraph：根目录 `langgraph.json`。
- FastAPI：`deep_data_research_agent.api.app:app`。
- Celery：`deep_data_research_agent.workers.app:celery_app`。
- 管理命令：统一通过 `pyproject.toml` 的 `[project.scripts]` 调用。

路径常量必须从明确的包根或仓库根计算，不能依赖模块恰好位于后端包第一层。
