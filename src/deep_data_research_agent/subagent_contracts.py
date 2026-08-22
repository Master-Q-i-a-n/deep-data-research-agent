"""Bounded result contracts and execution guards for specialized subagents."""

from __future__ import annotations

import json
import posixpath
import re
from typing import Annotated, Any, Literal, NotRequired

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    AgentState,
    PrivateStateAttr,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.channels.untracked_value import UntrackedValue
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ShortText = Annotated[str, Field(max_length=500)]
ArtifactDescription = Annotated[str, Field(max_length=300)]
_REVIEW_ROLE_LINE_RE = re.compile(r"审查角色[：:]\s*(.+)")
_REVIEW_ROLES = frozenset(
    {
        "numeric_consistency",
        "methodology_validity",
        "evidence_and_limitations",
    }
)
_REVIEW_TOOL_BUDGET = 30
_REVIEW_FILE_TOOLS = frozenset({"read_file", "grep"})
_REVIEW_EVIDENCE_TOOLS = _REVIEW_FILE_TOOLS | {"execute"}
_REVIEW_OUTPUT_ROOT = "/workspace/output"
_REVIEW_SCRIPTS_ROOT = "/workspace/scripts"
_REVIEW_PATH_RE = re.compile(
    r"/workspace/(?:output|scripts)/[^\s`'\"<>，。；：、]+"
)
_REVIEW_GUARD_KEY = "reviewer_guard_blocked"


class DataArtifact(BaseModel):
    """One real data-analyst artifact exposed to the Supervisor."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(max_length=500, pattern=r"^/workspace/")
    description: ArtifactDescription


class DataAnalystResult(BaseModel):
    """Compact structured result returned by each data-analyst delegation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "needs_input", "failed"]
    summary: str = Field(max_length=1_500)
    findings: list[ShortText] = Field(default_factory=list, max_length=12)
    artifacts: list[DataArtifact] = Field(default_factory=list, max_length=30)
    warnings: list[ShortText] = Field(default_factory=list, max_length=10)
    required_inputs: list[ShortText] = Field(default_factory=list, max_length=10)


class ReviewIssue(BaseModel):
    """One mandatory, evidence-backed report defect."""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["high", "medium"]
    category: Literal[
        "consistency",
        "methodology",
        "evidence",
        "limitation",
    ]
    description: ShortText
    evidence: ShortText
    suggested_fix: Annotated[str, Field(max_length=300)]


class AnalysisReviewerResult(BaseModel):
    """Compact structured result returned by the read-only reviewer."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "revision_required", "failed"]
    revision_mode: Literal[
        "none",
        "analysis_revision",
    ]
    summary: str = Field(max_length=1_000)
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=10)
    checked_artifacts: list[Annotated[str, Field(max_length=500)]] = Field(
        default_factory=list,
        max_length=30,
    )
    warnings: list[ShortText] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_revision_mode(self) -> AnalysisReviewerResult:
        if self.status != "revision_required" and self.revision_mode != "none":
            raise ValueError("仅 revision_required 可以指定修订模式")
        return self


def reviewer_result_contract_prompt() -> str:
    """Render the canonical Reviewer output contract from its Pydantic model."""

    schema = json.dumps(
        AnalysisReviewerResult.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # Build the example through Pydantic so contract changes fail loudly in tests.
    example = AnalysisReviewerResult(
        status="passed",
        revision_mode="none",
        summary="审查完成。",
        issues=[],
        checked_artifacts=[],
        warnings=[],
    ).model_dump_json()
    return (
        "以下 Pydantic JSON Schema 是唯一有效的最终输出合约。忽略委派描述中任何其他"
        "返回格式、字段定义或 JSON 示例；不得省略必填字段，也不得增加 Schema 外字段。\n"
        f"JSON Schema：{schema}\n"
        f"最小合法示例：{example}"
    )


class SubagentCallLimitState(AgentState):
    """Private per-invocation model-call counter."""

    subagent_model_call_count: NotRequired[
        Annotated[int, UntrackedValue, PrivateStateAttr]
    ]


class ReviewerResultState(AgentState):
    """Private state used for the Reviewer's one JSON correction attempt."""

    reviewer_json_retry_count: NotRequired[
        Annotated[int, UntrackedValue, PrivateStateAttr]
    ]


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict)
        )
    return str(content or "")


def _delegation_text(state: dict[str, Any]) -> str:
    """Return the first human delegation message from an ephemeral subagent run."""

    for message in state.get("messages", []):
        if isinstance(message, HumanMessage):
            return _message_text(message.content)
        if isinstance(message, dict) and str(
            message.get("type") or message.get("role")
        ).lower() in {"human", "user"}:
            return _message_text(message.get("content"))
    return ""


def is_revision_request(state: dict[str, Any]) -> bool:
    """Recognize a Reviewer-driven data-analyst revision without a mode marker."""

    delegation = _delegation_text(state)
    return "reviewer" in delegation.lower() and "修订" in delegation


def reviewer_roles(state: dict[str, Any]) -> frozenset[str]:
    """Read the explicit Reviewer role marker from the delegation message."""

    for line in _delegation_text(state).splitlines():
        match = _REVIEW_ROLE_LINE_RE.fullmatch(line.strip())
        if match:
            return frozenset(
                role for role in _REVIEW_ROLES if role in match.group(1)
            )
    return frozenset()


def reviewer_tool_budget(state: dict[str, Any]) -> int:
    """Use one uniform actual-tool budget for every Reviewer invocation."""

    del state
    return _REVIEW_TOOL_BUDGET


def _limit_failure_content(agent_name: str, limit: int) -> str:
    warning = f"达到本次委派的 LLM 调用上限（{limit}），任务未完整完成。"
    if agent_name == "analysis-reviewer":
        payload = {
            "status": "failed",
            "revision_mode": "none",
            "summary": warning,
            "issues": [],
            "checked_artifacts": [],
            "warnings": [warning],
        }
        return json.dumps(payload, ensure_ascii=False)
    if agent_name == "crawl-worker":
        return f"status: failed\n{warning} 已生成的工作区文件保持不变。"
    payload = {
        "status": "failed",
        "summary": warning,
        "findings": [],
        "artifacts": [],
        "warnings": [warning + " 已生成的工作区文件保持不变。"],
        "required_inputs": [],
    }
    return json.dumps(payload, ensure_ascii=False)


class SubagentModelCallLimitMiddleware(AgentMiddleware):
    """End one subagent invocation with a valid failure result at its call cap."""

    state_schema = SubagentCallLimitState

    def __init__(
        self,
        *,
        agent_name: str,
        run_limit: int = 30,
        revision_run_limit: int | None = None,
    ) -> None:
        self._agent_name = agent_name
        self._run_limit = run_limit
        self._revision_run_limit = revision_run_limit

    def _active_limit(self, state: dict[str, Any]) -> int:
        if self._revision_run_limit is not None and is_revision_request(state):
            return self._revision_run_limit
        return self._run_limit

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        del runtime
        limit = self._active_limit(state)
        count = int(state.get("subagent_model_call_count", 0))
        if count < limit:
            return None
        return {
            "jump_to": "end",
            "messages": [
                AIMessage(content=_limit_failure_content(self._agent_name, limit))
            ],
        }

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        return self.before_model(state, runtime)

    def after_model(self, state, runtime):
        del runtime
        return {
            "subagent_model_call_count": int(
                state.get("subagent_model_call_count", 0)
            )
            + 1
        }

    async def aafter_model(self, state, runtime):
        return self.after_model(state, runtime)


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        function = tool.get("function", {})
        return str(tool.get("name") or function.get("name") or "")
    return str(getattr(tool, "name", "") or "")


def _reviewer_allowed_paths(state: dict[str, Any]) -> frozenset[str]:
    """Extract exact evidence paths from the self-contained delegation."""

    roles = reviewer_roles(state)
    paths: set[str] = set()
    for match in _REVIEW_PATH_RE.finditer(_delegation_text(state)):
        raw_path = match.group(0).rstrip(".,;:)]}）】")
        normalized = posixpath.normpath(raw_path)
        if normalized.startswith(f"{_REVIEW_OUTPUT_ROOT}/") or (
            roles == {"methodology_validity"}
            and normalized.startswith(f"{_REVIEW_SCRIPTS_ROOT}/")
        ):
            paths.add(normalized)
    return frozenset(paths)


def _normalized_reviewer_call(
    state: dict[str, Any],
    tool_call: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Normalize file paths and enforce the delegation's exact allowlist."""

    name = str(tool_call.get("name", ""))
    raw_args = tool_call.get("args", {})
    args = dict(raw_args) if isinstance(raw_args, dict) else {}
    if name not in _REVIEW_FILE_TOOLS:
        return {**tool_call, "args": args}, None

    path_key = "file_path" if name == "read_file" else "path"
    raw_path = args.get(path_key)
    if not isinstance(raw_path, str) or not raw_path.startswith("/"):
        return {**tool_call, "args": args}, "文件工具必须使用委派中明确列出的绝对文件路径。"

    normalized = posixpath.normpath(raw_path)
    args[path_key] = normalized
    if normalized not in _reviewer_allowed_paths(state):
        return (
            {**tool_call, "args": args},
            "Reviewer 只能读取委派中为当前角色明确列出的证据文件。",
        )
    return {**tool_call, "args": args}, None


def _reviewer_call_fingerprint(tool_call: dict[str, Any]) -> str:
    args = tool_call.get("args", {})
    return f"{tool_call.get('name', '')}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"


def _guard_message(tool_call: dict[str, Any], reason: str, message: str) -> ToolMessage:
    return ToolMessage(
        content=json.dumps({"status": "blocked", "error": message}, ensure_ascii=False),
        tool_call_id=str(tool_call.get("id", "")),
        name=str(tool_call.get("name", "")) or None,
        status="error",
        additional_kwargs={_REVIEW_GUARD_KEY: reason},
    )


class ReviewerToolGuardMiddleware(AgentMiddleware):
    """Constrain Reviewer tools, deduplicate calls, and enforce role budgets."""

    def _visible_tools(self, request):
        state = request.state if isinstance(request.state, dict) else {}
        roles = reviewer_roles(state)
        if int(state.get("reviewer_json_retry_count", 0)) > 0:
            return []
        allowed_names = set(_REVIEW_FILE_TOOLS)
        if roles == {"numeric_consistency"}:
            allowed_names.add("execute")
        return [tool for tool in request.tools if _tool_name(tool) in allowed_names]

    def wrap_model_call(self, request, handler):
        return handler(request.override(tools=self._visible_tools(request)))

    async def awrap_model_call(self, request, handler):
        return await handler(request.override(tools=self._visible_tools(request)))

    @staticmethod
    def _completed_calls(state: dict[str, Any]) -> tuple[set[str], int]:
        results = {
            message.tool_call_id: message
            for message in state.get("messages", [])
            if isinstance(message, ToolMessage)
        }
        fingerprints: set[str] = set()
        actual_count = 0
        for message in state.get("messages", []):
            if not isinstance(message, AIMessage):
                continue
            for call in message.tool_calls:
                result = results.get(str(call.get("id", "")))
                if result is None:
                    continue
                normalized, _error = _normalized_reviewer_call(state, call)
                fingerprints.add(_reviewer_call_fingerprint(normalized))
                if not result.additional_kwargs.get(_REVIEW_GUARD_KEY):
                    actual_count += 1
        return fingerprints, actual_count

    def _decision(
        self,
        state: dict[str, Any],
        current_call: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None, str | None]:
        roles = reviewer_roles(state)
        budget = reviewer_tool_budget(state)
        correction_only = int(state.get("reviewer_json_retry_count", 0)) > 0
        prior_fingerprints, actual_count = self._completed_calls(state)

        current_ai = next(
            (
                message
                for message in reversed(state.get("messages", []))
                if isinstance(message, AIMessage)
                and any(
                    str(call.get("id", "")) == str(current_call.get("id", ""))
                    for call in message.tool_calls
                )
            ),
            None,
        )
        batch = current_ai.tool_calls if current_ai is not None else [current_call]
        seen = set(prior_fingerprints)
        used = actual_count
        for call in batch:
            normalized, scope_error = _normalized_reviewer_call(state, call)
            name = str(normalized.get("name", ""))
            fingerprint = _reviewer_call_fingerprint(normalized)
            reason: str | None = None
            message: str | None = None
            if correction_only:
                reason = "role"
                message = "JSON 纠正轮次禁止继续调用证据工具；只返回修正后的最终 JSON。"
            elif name not in _REVIEW_EVIDENCE_TOOLS:
                reason = "role"
                message = "Reviewer 只允许使用只读证据工具，请立即整理现有证据。"
            elif name == "execute" and roles != {"numeric_consistency"}:
                reason = "role"
                message = "execute 只允许 numeric_consistency Reviewer 使用，请直接整理现有证据。"
            elif scope_error:
                reason = "scope"
                message = scope_error
            elif fingerprint in seen:
                reason = "duplicate"
                message = "已有完全相同的工具证据，禁止重复调用；请立即整理并返回最终 JSON。"
            elif used >= budget:
                reason = "budget"
                message = f"当前审查角色的工具预算已用尽（{budget} 次）；请立即返回最终 JSON。"
            else:
                seen.add(fingerprint)
                used += 1

            if str(call.get("id", "")) == str(current_call.get("id", "")):
                return normalized, reason, message
        return current_call, "scope", "无法识别当前工具调用，请直接返回最终 JSON。"

    def wrap_tool_call(self, request, handler):
        state = request.state if isinstance(request.state, dict) else {}
        normalized, reason, message = self._decision(state, request.tool_call)
        if reason:
            return _guard_message(normalized, reason, message or "工具调用被阻止。")
        return handler(request.override(tool_call=normalized))

    async def awrap_tool_call(self, request, handler):
        state = request.state if isinstance(request.state, dict) else {}
        normalized, reason, message = self._decision(state, request.tool_call)
        if reason:
            return _guard_message(normalized, reason, message or "工具调用被阻止。")
        return await handler(request.override(tool_call=normalized))


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    # Tolerate one fenced JSON payload surrounded by accidental prose. Multiple
    # blocks remain invalid because selecting one would be ambiguous.
    blocks = re.findall(
        r"```(?:json)?\s*([\s\S]*?)\s*```",
        stripped,
        re.IGNORECASE,
    )
    return blocks[0].strip() if len(blocks) == 1 else stripped


def _review_guard_warnings(state: dict[str, Any]) -> list[str]:
    labels = {
        "duplicate": "审查中阻止了完全相同的重复工具调用。",
        "scope": "审查中阻止了委派证据清单之外的文件读取。",
        "budget": "审查已达到当前角色的工具预算。",
        "role": "审查中阻止了当前角色无权使用的工具调用。",
    }
    found = {
        str(message.additional_kwargs.get(_REVIEW_GUARD_KEY))
        for message in state.get("messages", [])
        if isinstance(message, ToolMessage)
        and message.additional_kwargs.get(_REVIEW_GUARD_KEY)
    }
    warnings = [labels[key] for key in labels if key in found]
    if len(reviewer_roles(state)) != 1:
        warnings.append("委派未包含唯一可识别的审查角色，未开放 execute，且只允许读取明确列出的 output 文件。")
    return warnings


class ReviewerResultValidationMiddleware(AgentMiddleware):
    """Validate plain Reviewer JSON and allow exactly one format correction."""

    state_schema = ReviewerResultState

    @staticmethod
    def _validate(state: dict[str, Any]):
        last = next(
            (
                message
                for message in reversed(state.get("messages", []))
                if isinstance(message, AIMessage)
            ),
            None,
        )
        if last is None or last.tool_calls or last.invalid_tool_calls:
            return None
        text = _strip_json_fence(_message_text(last.content))
        return AnalysisReviewerResult.model_validate_json(text)

    @staticmethod
    def _normalized_result(state: dict[str, Any], result: AnalysisReviewerResult) -> str:
        warnings = list(result.warnings)
        for warning in _review_guard_warnings(state):
            if warning not in warnings and len(warnings) < 10:
                warnings.append(warning)
        return result.model_copy(update={"warnings": warnings}).model_dump_json()

    @hook_config(can_jump_to=["model", "end"])
    def after_model(self, state, runtime):
        del runtime
        messages = state.get("messages", [])
        last = next(
            (message for message in reversed(messages) if isinstance(message, AIMessage)),
            None,
        )
        if last is None or last.tool_calls or last.invalid_tool_calls:
            return None
        try:
            result = self._validate(state)
        except (ValidationError, ValueError) as exc:
            retry_count = int(state.get("reviewer_json_retry_count", 0))
            if retry_count == 0:
                error = str(exc).replace("\n", " ")[:600]
                return {
                    "jump_to": "model",
                    "reviewer_json_retry_count": 1,
                    "messages": [
                        HumanMessage(
                            content=(
                                "最终 JSON 校验失败。不要再调用任何证据工具；只返回修正后的单个 "
                                "JSON 对象。保留已经得到的审查结论，只修正输出结构。\n"
                                f"{reviewer_result_contract_prompt()}\n"
                                f"校验错误：{error}"
                            )
                        )
                    ],
                }
            failure = AnalysisReviewerResult(
                status="failed",
                revision_mode="none",
                summary="Reviewer 连续两次未返回符合合约的 JSON。",
                issues=[],
                checked_artifacts=[],
                warnings=_review_guard_warnings(state)[:9]
                + ["第二次 JSON 格式校验失败，已确定性结束审查。"],
            )
            return {"jump_to": "end", "messages": [AIMessage(content=failure.model_dump_json())]}

        if result is None:
            return None
        return {
            "jump_to": "end",
            "messages": [AIMessage(content=self._normalized_result(state, result))],
        }

    async def aafter_model(self, state, runtime):
        return self.after_model(state, runtime)


def compact_crawl_summary(text: str, *, limit: int = 4_000) -> str:
    """Deterministically bound the worker summary while retaining a clear marker."""

    if len(text) <= limit:
        return text
    marker = "\n…[摘要已截断，完整内容见 /workspace/crawl_report.md]"
    return text[: limit - len(marker)].rstrip() + marker
