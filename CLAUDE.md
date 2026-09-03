# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

DeepAgents-based web-data research agent MVP (Python 3.13, uv, LangGraph Agent Server). A user submits a
natural-language research task; the **supervisor** graph plans it and delegates: synchronous `data-analyst`
(CSV/TSV/XLSX + PostgreSQL read-only analysis, returns a JSON contract), synchronous read-only
`analysis-reviewer` (verifies the report's JSON), and an asynchronous **crawl-worker** (Tavily
Search/Crawl/Extract inside a network-isolated OpenSandbox, returns a cited Markdown analysis). The
supervisor manages user Skills via a single `assign_skill` tool: Skills are created/downloaded and
tested in the supervisor sandbox under `/skill-manage/`, then assigned to target agents and persisted
to MongoDB.

Runtime is multi-user: each user configures their own OpenAI-compatible model provider (encrypted at
rest, injected per request) or uses the default `OPENAI_*` model in CLI/evals. Persistence topology:
PostgreSQL (accounts, sessions, thread ownership, token ledger, email deliveries, LangGraph
checkpoints), MongoDB (Skills + long-term memories), Redis (rate limits, run admission, sandbox
registry, token-bucket cache, Celery broker), local `data/` or Aliyun OSS for workspace snapshots.

UI text, system prompts, and error messages are written in Simplified Chinese; keep that convention when
editing prompts/strings. Never commit real API keys — `.env` is gitignored.

## Common commands

Backend + sandbox must both be running for any agent execution. Infra (PostgreSQL/MongoDB/Redis), `.env`,
and `.secrets/` must be configured first (README 配置 section). After first-time setup, `.\scripts\dev.ps1`
starts Redis + OpenSandbox + LangGraph + Celery worker/Beat + frontend in one shot (logs in
`%TEMP%\deep-data-research-agent\dev-logs/`); add `-SkipRedis -SkipFrontend` to run backend only.

```powershell
# 1. OpenSandbox server (required; agents fail without it)
uvx opensandbox-server --config ~/.sandbox.toml

# 2. LangGraph dev server (PYTHONUTF8 is mandatory on Windows — avoids a GBK OpenAPI read error)
$env:PYTHONUTF8='1'
uv run langgraph dev --n-jobs-per-worker 4 --no-browser
# Windows: do NOT add --no-reload (see gotchas)

# 3. Celery worker + Beat (memory / mail / maintenance queues, solo pool on Windows)
$env:PYTHONUTF8='1'
uv run celery -A deep_data_research_agent.workers.app:celery_app worker --pool=solo -Q memory,mail,maintenance --loglevel=INFO
uv run celery -A deep_data_research_agent.workers.app:celery_app beat --loglevel=INFO --schedule data/celerybeat-schedule
```

Frontend (separate shell, port 5174, defaults to `http://127.0.0.1:2024` supervisor):

```powershell
Set-Location '.\frontend'
Copy-Item -LiteralPath '.env.example' -Destination '.env.local'
npm install
npm run dev
```

Lint and tests:

```powershell
uv run ruff check .
uv run pytest
# Integration tests are gated behind env vars (require a live OpenSandbox / OSS):
$env:RUN_OPENSANDBOX_INTEGRATION='1'
uv run pytest tests/test_sandbox_manager.py -k real_opensandbox
$env:RUN_OSS_INTEGRATION='1'
uv run pytest tests/test_workspace_store.py -k real_oss
```

Frontend checks: `npm test -- --run` and `npm run build` (build runs `tsc --noEmit` on both tsconfigs).

Ops scripts (`[project.scripts]` + `scripts/`):

```powershell
.\scripts\setup\redis.ps1                     # provision Redis ACL user; password to .secrets/redis_password
uv run setup-agent-postgres                   # create migrator/app roles, stamp base, write .env
$env:POSTGRES_MIGRATION_URI='postgresql://<migrator>@...'; uv run alembic check; uv run alembic upgrade head
uv run reset-agent-memory                     # wipe memories; NOT users/skills/checkpoints
uv run run-agent-eval                          # single-run eval harness (evals/cases.yaml)
uv run python .\scripts\maintenance\sync_skills.py --yes   # resync repo seed skills to MongoDB
```

## Architecture

### Two graphs, one server

`langgraph.json` registers exactly two graphs (no Skill graph, no synchronous `task` subagents), plus a
LangGraph Store (MongoDB), a PostgreSQL checkpointer, custom auth, and an API app:

- **`supervisor`** (`src/deep_data_research_agent/agents/supervisor.py:graph`) — the user entry point. A
  `create_deep_agent` whose middleware chain ordering matters (see below). Registers `assign_skill`, the
  sync `task` subagents `data-analyst` / `analysis-reviewer` (not registered as graphs), and an async
  subagent pointing at `crawl-worker` (same-deployment ASGI transport, no URL).
- **`crawl-worker`** (`src/deep_data_research_agent/agents/crawl_worker.py:graph`) — a `StateGraph` wrapper
  around the crawl agent: `ensure_sandbox → crawl_agent → export_workspace`, then a final
  `_build_structured_result` node appends a `CrawlTaskResult` JSON (status/summary/artifacts/sources/warnings).
  Only this graph may call Tavily tools.

### Model runtime and per-user providers

`config.py` builds `ChatOpenAI` (default `qwen-plus`, temperature 0) from `.env` for CLI/evals, and the
**placeholder** models (`create_graph_placeholder_model`, API key `"not-configured"`) that graphs import
with. Online runs replace the placeholder in `ProviderSummarizationMiddleware` (`providers/models.py`)
with a per-user model resolved from `providers/service.py` — encrypted provider settings, LRU-cached
(TTL 900s), host allowlist, `follow_redirects=False`. Agent and memory callers pass a generic
`ModelExecutionProfile`; the Provider layer does not branch on Agent names. `ResponsesWebSearchChatOpenAI`
(providers/responses.py) uses
the Responses API when `model_profiles.yaml` declares it, and only requests `include` values declared
for that exact model. Execution profiles pass an opaque `harness_provider`; unified OpenAI and
Anthropic adapters forward it to DeepAgents without branching on application roles.

Every middleware that touches models is ordered as: `ProviderSummarizationMiddleware` →
`TokenUsageMiddleware` → `ContextUsageMiddleware`. The first middleware injects the runtime model and
delegates request-aware summarization. `ContextUsageMiddleware`
(`providers/context_usage.py`) anchors token counts to a replayable-prefix fingerprint so the frontend
gets cheap `context_usage` stream events without re-tokenizing the whole history. `TokenUsageMiddleware`
(`admissions/token_usage.py`) reserves before the call and settles after: PostgreSQL ledger is
authoritative, Redis is only a cache, and failures keep the reservation as `failed` — a run already
admitted is never stopped. Direct background model calls go through `metered_model_ainvoke` instead.

### Middleware ordering (supervisor)

In `supervisor.py` (and `crawl_worker.py`), order is significant — `after_model` hooks run in reverse:

1. `ProviderSummarizationMiddleware` — inject the per-user model and compact long history.
2. `TokenUsageMiddleware` — account model calls in the PostgreSQL token ledger.
3. `ContextUsageMiddleware` — stream context usage to the frontend, persist anchor.
4. `TodoListMiddleware` — DeepAgents' `write_todos` planning (omitted for analysis-reviewer).
5. `SandboxLifecycleMiddleware` — ensure supervisor sandbox before, export after (supervisor only).
6. `MemoryRefreshMiddleware` — refresh per-run memory files from MongoDB.
7. `MongoSkillsRestoreMiddleware` — restore public + user Skills from MongoDB to the sandbox.
8. `SkillToolErrorMiddleware` — converts expected Skill tool failures into recoverable `ToolMessage`s.
9. `MetadataPropagatingAsyncSubAgentMiddleware` — `start_async_task` with metadata propagation, task registry in `async_tasks` state.
11. `AsyncTaskBridgeMiddleware` — normalizes `check_async_task` nested JSON for the supervisor.
12. `ReloadableSkillsMiddleware` — strips cached `skills_metadata` on every run so new Skill assignments take effect on existing threads.
13. `FailureReviewMiddleware` — enqueues automatic failure-review jobs (tool-pair evidence only).

The crawl-worker uses the same set minus sandbox-lifecycle (the outer StateGraph's `_ensure_sandbox`
node does that) minus async-tools, plus `SubagentModelCallLimitMiddleware` (30 LLM calls max; the
supervisor applies the same to data-analyst/reviewer, and `ReviewerResultValidationMiddleware` /
`ReviewerToolGuardMiddleware` bound the reviewer's tool set and JSON contract). All contract JSONs
(status/summary/artifacts/…) live in `agents/contracts.py`.

### Backends and sandbox lifecycle

`backends.py` builds one `CompositeBackend` per (thread, component) with routed storage:

- default → OpenSandbox (`RestartSafeSandboxBackend`, lazily resolved from the runtime)
- `/state/` → `StateBackend` (artifacts root, exported deliverables)
- `/memories/user/` and `/memories/agent/{agent}/` → read-only `StoreBackend` over MongoDB (`/archive/` hidden)
- `/skills/public/{agent}/` → read-only public Skills (`("public", "skills", agent_name)`)
- `/skills/user/{agent}/` → read-only user Skills (per-user `assigned_skill_namespace`)
- `/workspace/input/**` is read-only (uploaded inputs)

Unmatched paths such as `/workspace/**` and `/skill-manage/**` go directly to the default sandbox; there
is no special staging route or virtual-to-physical mapping. `sandbox_manager.py` owns one OpenSandbox per
`(thread_id, component)`, lazily created and reused (renewed on health check, recreated on failure).
Components: `supervisor` (network-enabled so Skill download/install works via `execute`) and
`crawl-worker` (network-isolated — Tavily calls always run in the host process). Successful workspaces
are exported to `data/jobs/<thread_id>/<component>/workspace/` (or OSS) and restored into fresh
sandboxes. All lifecycle operations are LangSmith-traced (`sandbox.ensure`, `sandbox.restore`,
`sandbox.skills.sync`, `sandbox.export`) with inputs/outputs deliberately stripped of secrets and file
contents. The shared Redis registry lets another worker process re-adopt an existing sandbox on HITL resume.

### User Skill flow

The complete flow is driven by `skill-manage/SKILL.md` (repo seed: `skills/supervisor/`), which the
Supervisor is prompted to read in full before touching any Skill. The flow is deliberately simple:

create/download under `/skill-manage/{name}/` (via `write_file`/`execute` in the networked supervisor
sandbox) → test manually (`ls`/`read_file`/`execute`, `pip install` if needed) →
`assign_skill(name, targets)` persists every file to MongoDB under `(user_hash, "skills", "assigned",
target)` as `/active/{name}/**` plus a `/manifests/{name}.json` marker, then cleans up the staging dir.

Invariants: candidates must live under `/skill-manage/`; SKILL.md frontmatter must be exactly
`{name, description}` with `name == dir name`; the only tool in `tools/skills.py` is `assign_skill`
(exported as `ASSIGN_SKILL_TOOL`); available targets are read dynamically from `langgraph.json` graphs
plus `SKILL_AGENT_NAMES`, and the tool's error lists them on mismatch. `skill_system/storage.py` owns
the StoreBackend v2 value encoding, both namespace layouts
(`("public", "skills", agent)` vs `("user_hash", "skills", agent)`), and `rewrite_candidate_content`
which rewrites candidate `{{SKILL_ROOT}}` placeholders into the assignee's physical root. `skill_system/sync.py`
idempotently migrates the old `"skills", "assigned"` namespace to `("user_hash", "skills", agent)` and
syncs repo seed skills into the public namespace; sync failure blocks startup.

Next run, `MongoSkillsRestoreMiddleware` restores `/active/` entries into `/skills/{public,user}/{agent}/active/`
and `ReloadableSkillsMiddleware` re-scans skill metadata so the assignment takes effect on existing threads.

`identity.py` derives user identity and skill/memory namespaces from the sha256-authenticated user
injected by LangGraph Auth. Every environment refuses to operate without an authenticated identity.

### Long-term memory and admission controls

`memory/service.py` is the agent-facing memory layer (MongoDB-backed, read-only to agents):
`/memories/user/MEMORY.md` holds user preferences/feedback (written only by `capture_user_memory`),
`/memories/agent/{agent}/` holds cross-agent public failure lessons (each agent per run records at most
three). `FailureReviewMiddleware` enqueues a review job automatically; sensitive data is stripped by
regex before enqueue. Jobs are processed asynchronously by the Celery `memory` queue with leases and
retries (no Celery result backend — results live in MongoDB/PostgreSQL). Users can disable their
contribution or clear memories; `uv run reset-agent-memory` wipes all memories and job collections.

`admissions/redis_limits.py` is the protection layer: Redis-backed sliding windows (login/register,
question rate limits), per-user run admission (permits + reservations in Redis), and token-bucket
read-through from PostgreSQL. Internal async subagents are excluded via a server-signed internal-run
marker (`issue_internal_run_marker`). Redis failure is fail-closed for limits; the token ledger remains
authoritative and only its Redis cache is allowed to fail. `infrastructure/postgres/` owns the PostgreSQL
checkpointer and the Alembic setup (`setup-agent-postgres` creates migrator/app roles; the app never
runs `create_all()` or checkpoint `setup()` — migrations run explicitly by deploy). `infrastructure/redis/`
owns client + lock + key-prefix helpers (`ddra:` app keyspace, `ddra-celery:` Celery keyspace).

The API layer is in `api/`: custom routes (health in `api/health.py`, auth in `api/auth.py`,
provider settings, email delivery status) are mounted in `api/app.py`, `enable_custom_route_auth=true`
with `middleware_order=auth_first` in `langgraph.json`. `evaluation/runner.py` is a single-run eval
harness (fresh thread per case, credentials in memory, never approves the email tool).

## Conventions and gotchas

- **Two-turn async pattern:** Supervisor starts a crawl-worker, returns the full `task_id`, and must not poll
  in the same turn. The user later says "check task `<task_id>`"; Supervisor reads the result's first-line
  business `status` (not just the run `success`) before reporting — worker output is a `CrawlTaskResult` JSON.
- **`PYTHONUTF8=1`** is required to start `langgraph dev` on Windows.
- Windows: do not disable the dev server reload (`--no-reload` makes Uvicorn use a `ProactorEventLoop`,
  incompatible with psycopg's async pool which needs `SelectorEventLoop`).
- Backend factories and the sandbox manager are process-global singletons keyed by sanitized thread ID;
  `thread_id_from_runtime` reads `execution_info.thread_id` (LangGraph Runtime) and falls back to
  `config.configurable.thread_id` (ToolRuntime).
- Unit tests monkeypatch `sandbox_manager.SANDBOX_MANAGER` (e.g. `test_backends.py`, `test_tavily_tools.py`)
  and use `blockbuster` to block real network; graph-shape tests inspect
  `graph.nodes["tools"].bound.tools_by_name`; named tests in `test_subagent_contracts.py` cover the
  contracts, `tests/test_context_usage.py` the token estimation.
- Production is aggressive by default (`config.py` validator): `RATE_LIMIT_KEY_SECRET` ≥ 32 chars, Redis
  users must be set, `WORKSPACE_STORAGE_BACKEND=oss` is required, and provider host allowlist/ever
  HTTPS rules apply. Auth uses Argon2id passwords and SHA-256 token digests; login tokens never leave
  the backend, and browser storage never holds Provider API keys.
- `docs/architecture/design.md` documents the (partially future) architecture — some sections describe
  `analysis-worker`, `site-profiler`, and interrupt flows that are **not yet implemented**; the README
  "当前能力" list is the source of truth for what exists.
