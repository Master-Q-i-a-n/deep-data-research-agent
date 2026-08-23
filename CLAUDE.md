# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

DeepAgents-based web-data research agent MVP (Python 3.13, uv, LangGraph Agent Server). A user submits a
natural-language research task; the **supervisor** graph plans it and launches an asynchronous
**crawl-worker** subagent that uses Tavily Search/Crawl/Extract, works inside an OpenSandbox, and returns a
cited Markdown analysis. The supervisor also manages user Skills with a single `assign_skill` tool:
Skills are created/downloaded and tested in the supervisor sandbox under `/skill-manage/`, then assigned
to target agents and persisted to MongoDB.

UI text, system prompts, and error messages are written in Simplified Chinese; keep that convention when
editing prompts/strings. Never commit real API keys — `.env` is gitignored.

## Common commands

Backend + sandbox must both be running for any agent execution:

```powershell
# 1. OpenSandbox server (required; agents fail without it)
uvx opensandbox-server --config ~/.sandbox.toml

# 2. LangGraph dev server (PYTHONUTF8 is mandatory on Windows — avoids a GBK OpenAPI read error)
$env:PYTHONUTF8='1'
uv run langgraph dev --n-jobs-per-worker 4 --no-browser
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
# Integration tests are gated behind env vars (require a live OpenSandbox / MongoDB):
$env:RUN_OPENSANDBOX_INTEGRATION='1'
uv run pytest tests/test_sandbox_manager.py -k real_opensandbox
```

Frontend checks: `npm test -- --run` and `npm run build` (build runs `tsc --noEmit` on both tsconfigs).

## Architecture

### Two graphs, one server

`langgraph.json` registers exactly two graphs (no Skill graph, no synchronous `task` subagents):

- **`supervisor`** (`src/deep_data_research_agent/agents/supervisor.py:graph`) — the user entry point. A
  `create_deep_agent` whose middleware chain ordering matters (see below). Registers the single
  `assign_skill` tool and an `AsyncSubAgent` pointing at `crawl-worker` (same-deployment ASGI transport, no URL).
- **`crawl-worker`** (`src/deep_data_research_agent/agents/crawl_worker.py:graph`) — a `StateGraph` wrapper around
  the crawl agent: `ensure_sandbox → crawl_agent → export_workspace`. Only this graph may call Tavily tools.

`create_chat_model` (config.py) builds an OpenAI-compatible model (default `qwen-plus`, temperature 0) from
`.env`. The worker uses `_WorkerChatOpenAI` solely to select a separate DeepAgents harness profile
(`model_profile.py` registers `openai` and `deep-data-worker`). Placeholder key `"not-configured"` lets
graph imports and static tests run without `.env`.

### Backends and sandbox lifecycle

`backends.py` builds a request-local `CompositeBackend` per (thread, component) with routed storage:

- default → OpenSandbox (`sandbox_manager.SANDBOX_MANAGER.get_backend`)
- `/state/` → `StateBackend` (artifacts root)
- `/skills/` → read-only `FilesystemBackend` over `src/.../skills/` (built-in skills)
- `/persisted-skills/` → `StoreBackend` over MongoDB (user skills), namespaced by `assigned_skill_namespace`

Unmatched paths such as `/workspace/**` and `/skill-manage/**` go directly to the default sandbox;
there is no special staging route or path remapping. `/skills/**` and `/persisted-skills/**` are
read-only; `FILESYSTEM_PERMISSIONS` for supervisor vs
`WORKER_FILESYSTEM_PERMISSIONS` for the worker are identical today.

`sandbox_manager.py` owns one OpenSandbox per `(thread_id, component)`, lazily created and reused (renewed on
health check, recreated on failure). Components: `supervisor` (network-enabled so Skill download/install
works via `execute`) and `crawl-worker` (network-isolated — Tavily calls always run in the host process).
Successful workspaces are exported to
`data/jobs/<thread_id>/<component>/workspace/` and restored into fresh sandboxes. All lifecycle operations
are LangSmith-traced (`sandbox.ensure`, `sandbox.restore`, `sandbox.skills.sync`, `sandbox.export`) with
inputs/outputs deliberately stripped of secrets and file contents.

### Middleware ordering (supervisor)

In `agent.py`, order is significant:

1. `SandboxLifecycleMiddleware` — ensure sandbox before, export after.
2. `SkillsSyncMiddleware` — copy built-in skill files into the sandbox `/skills/`.
3. `UserSkillsRestoreMiddleware` — restore MongoDB active skills into `/persisted-skills/`.
4. `SkillToolErrorMiddleware` — converts expected Skill tool failures into recoverable `ToolMessage`s.
5. `AsyncSubAgentMiddleware` — provides the `crawl-worker` async task tools.
6. `ReloadableSkillsMiddleware` — strips cached `skills_metadata` on every run so new Skill assignments take
   effect on existing threads.

The crawl-worker uses SkillsSync, UserSkillsRestore, and ReloadableSkills (no sandbox-lifecycle middleware —
the outer StateGraph's `_ensure_sandbox` node does that).

### User Skill flow

The complete flow is driven by `skill-manage/SKILL.md`, which the Supervisor is prompted to read in full
before touching any Skill. The flow is deliberately simple:

create/download under `/skill-manage/{name}/` (via `write_file`/`execute` in the networked supervisor
sandbox) → test manually (`ls`/`read_file`/`execute`, `pip install` if needed) →
`assign_skill(name, targets)` persists every file to MongoDB under
`(user_hash, "skills", "assigned", target)` as `/active/{name}/**` plus a `/manifests/{name}.json`
marker, then cleans up the staging dir.

`/skill-manage/{name}/` is handled directly by the default OpenSandbox backend. File tools,
`execute`, and `assign_skill` therefore use the same absolute path, with no virtual-to-physical mapping.
SKILL.md tells the model to verify candidates with `read_file`/`ls` instead of broad glob scans.

Invariants: candidates must live under `/skill-manage/`; SKILL.md frontmatter must be exactly
`{name, description}` with `name == dir name`; the only tool in `skill_tools.py` is `assign_skill`
(exported as `ASSIGN_SKILL_TOOL`); available targets are read dynamically from `langgraph.json` graphs
(supervisor, crawl-worker) and the tool's error lists them on mismatch.

Next run, `UserSkillsRestoreMiddleware` restores `/active/` entries into `/persisted-skills/` and
`ReloadableSkillsMiddleware` re-scans skill metadata so the assignment takes effect.

`identity.py` derives the MongoDB namespace from a sha256 of the authenticated user (LangGraph Server
injected) with an explicit `LOCAL_DEV_USER_ID` fallback in development; production refuses to operate
without an authenticated identity.

## Conventions and gotchas

- **Two-turn async pattern:** Supervisor starts a crawl-worker, returns the full `task_id`, and must not poll
  in the same turn. The user later says "check task `<task_id>`"; Supervisor reads the result's first-line
  business `status` (not just the run `success`) before reporting.
- **`PYTHONUTF8=1`** is required to start `langgraph dev` on Windows.
- Backend factories and the sandbox manager are process-global singletons keyed by sanitized thread ID;
  `thread_id_from_runtime` reads `execution_info.thread_id` (LangGraph Runtime) and falls back to
  `config.configurable.thread_id` (ToolRuntime).
- Unit tests monkeypatch `sandbox_manager.SANDBOX_MANAGER` (e.g. `test_backends.py`, `test_tavily_tools.py`)
  and use `blockbuster` to block real network; graph-shape tests inspect
  `graph.nodes["tools"].bound.tools_by_name`.
- `docs/architecture/design.md` documents the (partially future) architecture — some sections describe `analysis-worker`,
  `site-profiler`, and interrupt flows that are **not yet implemented**; the README "当前能力" list is the
  source of truth for what exists.
