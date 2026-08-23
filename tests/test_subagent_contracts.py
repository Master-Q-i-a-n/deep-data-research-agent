import json
from types import SimpleNamespace

import openai
import pytest
from deepagents import AsyncSubAgent
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain.tools import ToolRuntime
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from pydantic import ValidationError

from deep_data_research_agent.agents.async_subagents import (
    MetadataPropagatingAsyncSubAgentMiddleware,
)
from deep_data_research_agent.agents.contracts import (
    AnalysisReviewerResult,
    DataAnalystResult,
    ReviewerResultValidationMiddleware,
    ReviewerToolGuardMiddleware,
    SubagentModelCallLimitMiddleware,
    compact_crawl_summary,
    is_revision_request,
    reviewer_result_contract_prompt,
    reviewer_roles,
    reviewer_tool_budget,
)
from deep_data_research_agent.agents.crawl_worker import CrawlTaskResult
from deep_data_research_agent.agents.prompts import (
    ANALYSIS_REVIEWER_PROMPT,
    DATA_ANALYST_PROMPT,
    SUPERVISOR_PROMPT,
)
from deep_data_research_agent.core.config import (
    create_data_analyst_model,
    create_reviewer_model,
    get_settings,
)


def test_data_analyst_contract_enforces_text_and_collection_bounds() -> None:
    with pytest.raises(ValidationError):
        DataAnalystResult(
            status="success",
            summary="x" * 1_501,
            findings=[],
            artifacts=[],
            warnings=[],
            required_inputs=[],
        )
    with pytest.raises(ValidationError):
        DataAnalystResult(
            status="success",
            summary="ok",
            findings=[],
            artifacts=[
                {"path": f"/workspace/{index}.md", "description": "artifact"}
                for index in range(31)
            ],
            warnings=[],
            required_inputs=[],
        )
    with pytest.raises(ValidationError):
        DataAnalystResult(
            status="needs_input",
            summary="missing",
            findings=[],
            artifacts=[],
            warnings=[],
            required_inputs=["input"] * 11,
        )
    with pytest.raises(ValidationError):
        DataAnalystResult(
            status="success",
            summary="ok",
            findings=["finding"] * 13,
            artifacts=[],
            warnings=[],
            required_inputs=[],
        )


def test_reviewer_contract_rejects_optional_and_artifact_issues() -> None:
    with pytest.raises(ValidationError):
        AnalysisReviewerResult.model_validate(
            {
                "status": "revision_required",
                "revision_mode": "none",
                "summary": "需要修订",
                "issues": [
                    {
                        "severity": "low",
                        "category": "presentation",
                        "description": "可选风格建议",
                        "evidence": "无",
                        "suggested_fix": "润色",
                    }
                ],
                "checked_artifacts": [],
                "warnings": [],
            }
        )
    for removed_category in ("artifact", "path_or_citation"):
        with pytest.raises(ValidationError):
            AnalysisReviewerResult.model_validate(
                {
                    "status": "revision_required",
                    "revision_mode": "none",
                    "summary": "需要修订",
                    "issues": [
                        {
                            "severity": "medium",
                            "category": removed_category,
                            "description": "不属于 Reviewer 职责",
                            "evidence": "报告路径",
                            "suggested_fix": "交由分析执行者自检",
                        }
                    ],
                    "checked_artifacts": [],
                    "warnings": [],
                }
            )
    assert AnalysisReviewerResult.model_validate(
        {
            "status": "revision_required",
            "revision_mode": "analysis_revision",
            "summary": "方法需要修订",
            "issues": [
                {
                    "severity": "high",
                    "category": "methodology",
                    "description": "训练集与验证集发生泄漏",
                    "evidence": "脚本在划分前拟合预处理器",
                    "suggested_fix": "仅在训练集拟合预处理器",
                }
            ],
            "checked_artifacts": [],
            "warnings": [],
        }
    ).issues[0].category == "methodology"
    assert AnalysisReviewerResult.model_validate(
        {
            "status": "revision_required",
            "revision_mode": "none",
            "summary": "主报告存在抄写错误",
            "issues": [
                {
                    "severity": "medium",
                    "category": "consistency",
                    "description": "报告比例与结果表不一致",
                    "evidence": "报告为 31%，结果表为 30%",
                    "suggested_fix": "由 Supervisor 更正主报告",
                }
            ],
            "checked_artifacts": [],
            "warnings": [],
        }
    ).revision_mode == "none"
    with pytest.raises(ValidationError):
        AnalysisReviewerResult(
            status="passed",
            revision_mode="analysis_revision",
            summary="通过",
            issues=[],
            checked_artifacts=[],
            warnings=[],
        )
    with pytest.raises(ValidationError):
        AnalysisReviewerResult(
            status="revision_required",
            revision_mode="none",
            summary="x" * 1_001,
            issues=[],
            checked_artifacts=[],
            warnings=[],
        )
    issue = {
        "severity": "medium",
        "category": "evidence",
        "description": "结论无证据",
        "evidence": "报告第 2 节",
        "suggested_fix": "删除结论",
    }
    with pytest.raises(ValidationError):
        AnalysisReviewerResult(
            status="revision_required",
            revision_mode="none",
            summary="需要修订",
            issues=[issue] * 11,
            checked_artifacts=[],
            warnings=[],
        )


def test_crawl_contract_and_deterministic_summary_truncation() -> None:
    summary = compact_crawl_summary("证据" * 3_000)

    assert len(summary) <= 4_000
    assert "完整内容见 /workspace/crawl_report.md" in summary
    with pytest.raises(ValidationError):
        CrawlTaskResult(
            status="success",
            summary="ok",
            artifacts=[],
            sources=[{"title": str(index), "url": f"https://example.com/{index}"} for index in range(21)],
            warnings=[],
        )
    with pytest.raises(ValidationError):
        CrawlTaskResult(
            status="failed",
            summary="bounded",
            artifacts=[
                {
                    "path": f"/workspace/{index}.md",
                    "type": "file",
                    "mime_type": "text/markdown",
                    "size": 1,
                    "description": "file",
                }
                for index in range(51)
            ],
            sources=[],
            warnings=["warning"] * 11,
        )


def test_model_call_limit_uses_30_and_revision_12() -> None:
    middleware = SubagentModelCallLimitMiddleware(
        agent_name="data-analyst",
        run_limit=30,
        revision_run_limit=12,
    )
    ordinary = {"messages": [HumanMessage(content="执行模式：formal_report")], "subagent_model_call_count": 30}
    revision = {
        "messages": [HumanMessage(content="根据 Reviewer 已确认的问题修订分析")],
        "subagent_model_call_count": 12,
    }

    assert middleware.before_model({**ordinary, "subagent_model_call_count": 29}, None) is None
    ordinary_result = middleware.before_model(ordinary, None)
    revision_result = middleware.before_model(revision, None)
    assert json.loads(ordinary_result["messages"][0].content)["status"] == "failed"
    assert json.loads(revision_result["messages"][0].content)["status"] == "failed"
    assert "30" in ordinary_result["messages"][0].content
    assert "12" in revision_result["messages"][0].content

    reviewer = SubagentModelCallLimitMiddleware(
        agent_name="analysis-reviewer",
        run_limit=12,
    )
    for role in (
        "numeric_consistency",
        "methodology_validity",
        "evidence_and_limitations",
    ):
        reviewer_result = reviewer.before_model(
            {
                "messages": [HumanMessage(content=f"审查角色：{role}")],
                "subagent_model_call_count": 12,
            },
            None,
        )
        reviewer_payload = json.loads(reviewer_result["messages"][0].content)
        assert reviewer_payload["status"] == "failed"
        assert "12" in reviewer_payload["summary"]


def test_reviewer_driven_revision_is_detected_without_an_execution_mode() -> None:
    assert is_revision_request(
        {"messages": [HumanMessage(content="根据 Reviewer 结果定向修订模型分析")]}
    )
    assert not is_revision_request(
        {"messages": [HumanMessage(content="执行模式：formal_report\n生成初始报告")]}
    )


def test_reviewer_roles_and_tool_budgets_are_uniform() -> None:
    numeric = {"messages": [HumanMessage(content="审查角色：numeric_consistency")]}
    methodology = {"messages": [HumanMessage(content="审查角色：methodology_validity")]}
    evidence = {"messages": [HumanMessage(content="审查角色：evidence_and_limitations")]}

    assert reviewer_roles(methodology) == {"methodology_validity"}
    assert reviewer_tool_budget(numeric) == 30
    assert reviewer_tool_budget(methodology) == 30
    assert reviewer_tool_budget(evidence) == 30
    assert reviewer_tool_budget({"messages": [HumanMessage(content="未标注角色")]}) == 30


def _tool_request(state, tool_call):
    return ToolCallRequest(
        tool_call=tool_call,
        tool=SimpleNamespace(name=tool_call["name"]),
        state=state,
        runtime=None,
    )


@pytest.mark.asyncio
async def test_reviewer_guard_requires_exact_paths_and_blocks_scope_escape() -> None:
    middleware = ReviewerToolGuardMiddleware()
    report_path = "/workspace/output/report.md"
    state = {
        "messages": [
            HumanMessage(
                content=(
                    "审查角色：evidence_and_limitations\n"
                    f"允许证据：{report_path}"
                )
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": report_path},
                        "id": "call-1",
                    }
                ],
            ),
        ]
    }
    captured = {}

    async def handler(request):
        captured.update(request.tool_call["args"])
        return ToolMessage(content="ok", tool_call_id=request.tool_call["id"])

    allowed = await middleware.awrap_tool_call(
        _tool_request(state, state["messages"][-1].tool_calls[0]),
        handler,
    )
    assert isinstance(allowed, ToolMessage)
    assert captured == {"file_path": report_path}

    escaped_call = {
        "name": "read_file",
        "args": {"file_path": "/workspace/output/../input/data.csv"},
        "id": "call-2",
    }
    escaped_state = {
        "messages": [
            state["messages"][0],
            AIMessage(content="", tool_calls=[escaped_call]),
        ]
    }
    blocked = await middleware.awrap_tool_call(
        _tool_request(escaped_state, escaped_call),
        handler,
    )
    assert blocked.status == "error"
    assert blocked.additional_kwargs["reviewer_guard_blocked"] == "scope"

    write_call = {
        "name": "write_file",
        "args": {"file_path": "/workspace/output/review.md", "content": "x"},
        "id": "call-3",
    }
    write_state = {
        "messages": [
            state["messages"][0],
            AIMessage(content="", tool_calls=[write_call]),
        ]
    }
    blocked_write = await middleware.awrap_tool_call(
        _tool_request(write_state, write_call),
        handler,
    )
    assert blocked_write.additional_kwargs["reviewer_guard_blocked"] == "role"

    unlisted_call = {
        "name": "read_file",
        "args": {"file_path": "/workspace/output/unlisted.csv"},
        "id": "call-4",
    }
    unlisted_state = {
        "messages": [
            state["messages"][0],
            AIMessage(content="", tool_calls=[unlisted_call]),
        ]
    }
    blocked_unlisted = await middleware.awrap_tool_call(
        _tool_request(unlisted_state, unlisted_call),
        handler,
    )
    assert blocked_unlisted.additional_kwargs["reviewer_guard_blocked"] == "scope"


@pytest.mark.asyncio
async def test_reviewer_guard_allows_only_methodology_to_read_listed_scripts() -> None:
    middleware = ReviewerToolGuardMiddleware()
    script_path = "/workspace/scripts/model.py"
    report_path = "/workspace/output/report.md"
    call = {
        "name": "read_file",
        "args": {"file_path": script_path},
        "id": "script-read",
    }

    async def handler(request):
        return ToolMessage(content="ok", tool_call_id=request.tool_call["id"])

    methodology_state = {
        "messages": [
            HumanMessage(
                content=(
                    "审查角色：methodology_validity\n"
                    f"主报告：{report_path}\n允许脚本：{script_path}"
                )
            ),
            AIMessage(content="", tool_calls=[call]),
        ]
    }
    allowed = await middleware.awrap_tool_call(
        _tool_request(methodology_state, call),
        handler,
    )
    assert allowed.content == "ok"

    evidence_state = {
        "messages": [
            HumanMessage(
                content=(
                    "审查角色：evidence_and_limitations\n"
                    f"主报告：{report_path}\n脚本（禁止读取）：{script_path}"
                )
            ),
            AIMessage(content="", tool_calls=[call]),
        ]
    }
    blocked_role = await middleware.awrap_tool_call(
        _tool_request(evidence_state, call),
        handler,
    )
    assert blocked_role.additional_kwargs["reviewer_guard_blocked"] == "scope"

    unlisted_call = {
        "name": "read_file",
        "args": {"file_path": "/workspace/scripts/unlisted.py"},
        "id": "unlisted-script",
    }
    methodology_state["messages"][-1] = AIMessage(
        content="",
        tool_calls=[unlisted_call],
    )
    blocked_unlisted = await middleware.awrap_tool_call(
        _tool_request(methodology_state, unlisted_call),
        handler,
    )
    assert blocked_unlisted.additional_kwargs["reviewer_guard_blocked"] == "scope"


@pytest.mark.asyncio
async def test_reviewer_guard_blocks_cross_turn_and_same_batch_duplicates() -> None:
    middleware = ReviewerToolGuardMiddleware()
    report_path = "/workspace/output/report.md"
    role = HumanMessage(
        content=f"审查角色：evidence_and_limitations\n允许证据：{report_path}"
    )
    first_call = {
        "name": "read_file",
        "args": {"file_path": report_path, "offset": 0, "limit": 1000},
        "id": "call-1",
    }
    first_result = ToolMessage(content="ok", tool_call_id="call-1")
    repeated = {**first_call, "id": "call-2"}
    repeated_state = {
        "messages": [
            role,
            AIMessage(content="", tool_calls=[first_call]),
            first_result,
            AIMessage(content="", tool_calls=[repeated]),
        ]
    }
    called = False

    async def handler(_request):
        nonlocal called
        called = True
        return "unexpected"

    blocked = await middleware.awrap_tool_call(
        _tool_request(repeated_state, repeated),
        handler,
    )
    assert blocked.additional_kwargs["reviewer_guard_blocked"] == "duplicate"
    assert called is False

    parallel_first = {**first_call, "id": "call-3"}
    parallel_second = {**first_call, "id": "call-4"}
    parallel_state = {
        "messages": [
            role,
            AIMessage(content="", tool_calls=[parallel_first, parallel_second]),
        ]
    }
    blocked_parallel = await middleware.awrap_tool_call(
        _tool_request(parallel_state, parallel_second),
        handler,
    )
    assert blocked_parallel.additional_kwargs["reviewer_guard_blocked"] == "duplicate"
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [
        "numeric_consistency",
        "methodology_validity",
        "evidence_and_limitations",
    ],
)
async def test_reviewer_guard_enforces_actual_call_budget_without_counting_blocks(
    role: str,
) -> None:
    middleware = ReviewerToolGuardMiddleware()
    report_path = "/workspace/output/report.md"
    messages = [
        HumanMessage(
            content=f"审查角色：{role}\n允许证据：{report_path}"
        )
    ]
    for index in range(29):
        call = {
            "name": "read_file",
            "args": {"file_path": report_path, "offset": index, "limit": 1},
            "id": f"done-{index}",
        }
        messages.extend(
            [
                AIMessage(content="", tool_calls=[call]),
                ToolMessage(content="ok", tool_call_id=call["id"]),
            ]
        )
    blocked_history_call = {
        "name": "read_file",
        "args": {"file_path": "/workspace/input/data.csv"},
        "id": "scope-blocked",
    }
    messages.extend(
        [
            AIMessage(content="", tool_calls=[blocked_history_call]),
            ToolMessage(
                content="blocked",
                tool_call_id="scope-blocked",
                status="error",
                additional_kwargs={"reviewer_guard_blocked": "scope"},
            ),
        ]
    )
    thirtieth_call = {
        "name": "grep",
        "args": {"pattern": "metric", "path": report_path},
        "id": "thirtieth-actual",
    }
    messages.append(AIMessage(content="", tool_calls=[thirtieth_call]))

    async def allowed_handler(request):
        return ToolMessage(content="ok", tool_call_id=request.tool_call["id"])

    allowed = await middleware.awrap_tool_call(
        _tool_request({"messages": messages}, thirtieth_call),
        allowed_handler,
    )
    assert allowed.content == "ok"

    messages.append(allowed)
    next_call = {
        "name": "read_file",
        "args": {"file_path": report_path, "offset": 30, "limit": 1},
        "id": "over-budget",
    }
    messages.append(AIMessage(content="", tool_calls=[next_call]))

    async def handler(_request):
        raise AssertionError("over-budget call must not execute")

    blocked = await middleware.awrap_tool_call(
        _tool_request({"messages": messages}, next_call),
        handler,
    )
    assert blocked.additional_kwargs["reviewer_guard_blocked"] == "budget"
    assert "30 次" in blocked.content


def test_reviewer_guard_only_exposes_execute_to_numeric_role() -> None:
    middleware = ReviewerToolGuardMiddleware()
    tools = [
        SimpleNamespace(name="read_file"),
        SimpleNamespace(name="grep"),
        SimpleNamespace(name="ls"),
        SimpleNamespace(name="glob"),
        SimpleNamespace(name="execute"),
    ]

    def visible(role: str):
        request = ModelRequest(
            model=create_reviewer_model(),
            messages=[],
            tools=tools,
            state={"messages": [HumanMessage(content=f"审查角色：{role}")]},
        )
        return middleware.wrap_model_call(request, lambda updated: updated.tools)

    assert [tool.name for tool in visible("evidence_and_limitations")] == [
        "read_file",
        "grep",
    ]
    assert [tool.name for tool in visible("methodology_validity")] == [
        "read_file",
        "grep",
    ]
    assert [tool.name for tool in visible("未识别角色")] == ["read_file", "grep"]
    assert [tool.name for tool in visible("numeric_consistency")] == [
        "read_file",
        "grep",
        "execute",
    ]

    retry_request = ModelRequest(
        model=create_reviewer_model(),
        messages=[],
        tools=tools,
        state={
            "messages": [HumanMessage(content="审查角色：numeric_consistency")],
            "reviewer_json_retry_count": 1,
        },
    )
    assert middleware.wrap_model_call(
        retry_request,
        lambda updated: updated.tools,
    ) == []


def _valid_review_json() -> str:
    return json.dumps(
        {
            "status": "passed",
            "revision_mode": "none",
            "summary": "审查通过",
            "issues": [],
            "checked_artifacts": ["/workspace/output/report.md"],
            "warnings": [],
        },
        ensure_ascii=False,
    )


def test_reviewer_result_contract_prompt_comes_from_pydantic_schema() -> None:
    contract = reviewer_result_contract_prompt()
    for field in AnalysisReviewerResult.model_fields:
        assert f'"{field}"' in contract
    assert "唯一有效的最终输出合约" in contract
    assert '"checked_artifacts":[]' in contract


def test_reviewer_result_validator_normalizes_plain_and_fenced_json() -> None:
    middleware = ReviewerResultValidationMiddleware()
    for content in (
        _valid_review_json(),
        f"```json\n{_valid_review_json()}\n```",
        f"审查完成，结果如下。\n```json\n{_valid_review_json()}\n```",
    ):
        state = {
            "messages": [
                HumanMessage(content="审查角色：evidence_and_limitations"),
                AIMessage(content=content),
            ]
        }
        result = middleware.after_model(state, None)
        assert result["jump_to"] == "end"
        assert json.loads(result["messages"][0].content)["status"] == "passed"


def test_reviewer_result_validator_retries_once_then_returns_failed_json() -> None:
    middleware = ReviewerResultValidationMiddleware()
    initial = {
        "messages": [
            HumanMessage(content="审查角色：evidence_and_limitations"),
            AIMessage(content="not json"),
        ]
    }
    retry = middleware.after_model(initial, None)
    assert retry["jump_to"] == "model"
    assert retry["reviewer_json_retry_count"] == 1
    assert "不要再调用任何证据工具" in retry["messages"][0].content
    assert "唯一有效的最终输出合约" in retry["messages"][0].content
    assert '"revision_mode"' in retry["messages"][0].content
    assert '"checked_artifacts"' in retry["messages"][0].content

    failed = middleware.after_model(
        {
            "messages": [*initial["messages"], *retry["messages"], AIMessage(content="still invalid")],
            "reviewer_json_retry_count": 1,
        },
        None,
    )
    payload = json.loads(failed["messages"][0].content)
    assert failed["jump_to"] == "end"
    assert payload["status"] == "failed"
    assert payload["revision_mode"] == "none"


def test_compiled_reviewer_returns_normal_message_and_corrects_json_once() -> None:
    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(content="invalid"),
            AIMessage(content=_valid_review_json()),
        ]
    )
    agent = create_agent(
        model,
        tools=[],
        middleware=[
            ReviewerResultValidationMiddleware(),
            ReviewerToolGuardMiddleware(),
            SubagentModelCallLimitMiddleware(
                agent_name="analysis-reviewer",
                run_limit=12,
            ),
        ],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="审查角色：evidence_and_limitations")]}
    )

    assert json.loads(result["messages"][-1].content)["status"] == "passed"
    assert any(
        isinstance(message, HumanMessage) and "最终 JSON 校验失败" in message.content
        for message in result["messages"]
    )


def test_prompts_forbid_unrequested_search_and_optional_reviewer_work() -> None:
    assert "默认禁止 GridSearchCV" in DATA_ANALYST_PROMPT
    assert "最多 5 折交叉验证" in DATA_ANALYST_PROMPT
    assert "不输出任何可选增强" in ANALYSIS_REVIEWER_PROMPT
    assert "passed 时 issues 必须为空" in ANALYSIS_REVIEWER_PROMPT
    assert "因 LLM 调用上限返回 failed 时也绝不重试" in SUPERVISOR_PROMPT
    assert "不得增加 Reviewer 未提出的内容" in SUPERVISOR_PROMPT


def test_deepseek_data_analyst_enables_and_round_trips_thinking() -> None:
    if not get_settings().openai_model.startswith("deepseek-v4"):
        pytest.skip("only applies to DeepSeek V4")

    model = create_data_analyst_model()
    assert model.extra_body == {"thinking": {"type": "enabled"}}
    payload = model._get_request_payload(
        [
            AIMessage(
                content="",
                additional_kwargs={"reasoning_content": "继续分析"},
            )
        ]
    )
    assert payload["messages"][0]["reasoning_content"] == "继续分析"


def test_deepseek_reviewer_enables_and_round_trips_thinking() -> None:
    if not get_settings().openai_model.startswith("deepseek-v4"):
        pytest.skip("only applies to DeepSeek V4")

    model = create_reviewer_model()
    assert model.extra_body == {"thinking": {"type": "enabled"}}
    payload = model._get_request_payload(
        [
            AIMessage(
                content="",
                additional_kwargs={"reasoning_content": "核验证据"},
            )
        ]
    )
    assert payload["messages"][0]["reasoning_content"] == "核验证据"

    raw = openai.types.chat.ChatCompletion.model_validate(
        {
            "id": "reviewer-response",
            "choices": [
                {
                    "finish_reason": "stop",
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "继续核验",
                    },
                }
            ],
            "created": 0,
            "model": get_settings().openai_model,
            "object": "chat.completion",
        }
    )
    result = model._create_chat_result(raw)
    assert result.generations[0].message.additional_kwargs["reasoning_content"] == "继续核验"


@pytest.mark.asyncio
async def test_async_launch_propagates_evaluation_metadata(monkeypatch) -> None:
    calls: dict[str, dict] = {}

    class Threads:
        async def create(self, **kwargs):
            calls["thread"] = kwargs
            return {"thread_id": "child-thread"}

    class Runs:
        async def create(self, **kwargs):
            calls["run"] = kwargs
            return {"run_id": "child-run"}

    client = SimpleNamespace(threads=Threads(), runs=Runs())
    monkeypatch.setattr(
        "deep_data_research_agent.agents.async_subagents._ClientCache.get_async",
        lambda _self, _name: client,
    )
    middleware = MetadataPropagatingAsyncSubAgentMiddleware(
        async_subagents=[
            AsyncSubAgent(
                name="crawl-worker",
                description="crawl",
                graph_id="crawl-worker",
            )
        ],
        system_prompt=None,
    )
    tool = next(item for item in middleware.tools if item.name == "start_async_task")
    runtime = ToolRuntime(
        state={},
        context=None,
        config={
            "metadata": {
                "eval_run_id": "eval-run",
                "eval_case_id": "W01",
                "thread_id": "parent-thread",
            }
        },
        stream_writer=lambda _value: None,
        tool_call_id="tool-call",
        store=None,
    )

    await tool.coroutine(
        description="采购调研",
        subagent_type="crawl-worker",
        runtime=runtime,
    )

    expected = {
        "eval_run_id": "eval-run",
        "eval_case_id": "W01",
        "graph_id": "crawl-worker",
        "kind": "async-subagent",
        "parent_thread_id": "parent-thread",
    }
    assert calls["thread"]["metadata"] == expected
    assert calls["run"]["metadata"] == expected
