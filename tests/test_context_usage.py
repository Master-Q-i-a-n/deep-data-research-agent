from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from deep_data_research_agent.admissions import token_usage
from deep_data_research_agent.providers import context_usage


def _model(*, version: int = 3):
    return SimpleNamespace(
        model_name="gpt-test",
        profile={"max_input_tokens": 1_000},
        _deep_data_provider_version=version,
    )


def _request(messages, *, state=None, tools=None, version: int = 3) -> ModelRequest:
    return ModelRequest(
        model=_model(version=version),
        messages=list(messages),
        system_message=SystemMessage(content="system"),
        tools=list(tools or []),
        state=state or {"messages": list(messages)},
    )


def test_character_estimator_uses_approved_ascii_and_non_ascii_ratios() -> None:
    assert context_usage.estimate_text_tokens("abc") == 1
    assert context_usage.estimate_text_tokens("中文") == 2
    assert context_usage.estimate_text_tokens("ab中") == 2


def test_accounting_estimator_never_calls_model_tokenizer() -> None:
    class Model:
        def get_num_tokens_from_messages(self, _messages):
            raise AssertionError("Provider tokenizer must not be called")

    assert token_usage.estimate_tokens(Model(), [HumanMessage(content="hello")]) > 0


def test_hidden_reasoning_usage_is_used_only_when_replayable() -> None:
    hidden = AIMessage(
        content="answer",
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 100,
            "total_tokens": 110,
            "output_token_details": {"reasoning": 80},
        },
    )
    replayable = AIMessage(
        content=[
            {"type": "reasoning", "encrypted_content": "opaque"},
            {"type": "text", "text": "answer"},
        ],
        usage_metadata=hidden.usage_metadata,
    )

    assert context_usage.estimate_message_tokens(hidden) < 100
    assert context_usage.estimate_message_tokens(replayable) == 100


@pytest.mark.asyncio
async def test_context_usage_persists_real_usage_anchor_and_estimates_delta(
    monkeypatch,
) -> None:
    events: list[dict[str, object]] = []
    monkeypatch.setattr(context_usage, "_stream_writer", lambda: events.append)
    human = HumanMessage(content="你好", id="human-1")
    request = _request([human])
    answer = AIMessage(
        content="world",
        id="ai-1",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
        },
    )

    async def handler(_request):
        return ModelResponse(result=[answer])

    response = await context_usage.ContextUsageMiddleware(
        "supervisor"
    ).awrap_model_call(
        request,
        handler,
    )
    update = response.command.update
    assert update["context_usage"] == {
        "used_tokens": 120,
        "max_input_tokens": 1_000,
        "provider_version": 3,
    }
    assert events[-1]["phase"] == "after_model"

    next_human = HumanMessage(content="next", id="human-2")
    state = {
        "messages": [human, answer, next_human],
        "_context_token_anchor": update["_context_token_anchor"],
    }
    next_request = _request([human, answer, next_human], state=state)
    expected = 120 + context_usage.estimate_message_tokens(next_human)
    assert context_usage.estimate_request_tokens(next_request) == expected


@pytest.mark.asyncio
async def test_hosted_search_anchors_only_replayable_context_then_recalibrates(
    monkeypatch,
) -> None:
    events: list[dict[str, object]] = []
    monkeypatch.setattr(context_usage, "_stream_writer", lambda: events.append)
    human = HumanMessage(content="查询最新价格", id="human-1")
    search_request = _request([human])
    used_before = context_usage.estimate_request_tokens(search_request)
    search_answer = AIMessage(
        id="ai-search",
        content=[
            {"type": "reasoning", "encrypted_content": "opaque"},
            {
                "type": "web_search_call",
                "id": "ws-1",
                "status": "completed",
                "action": {"type": "search", "query": "latest price"},
            },
            {"type": "text", "text": "搜索结果摘要"},
        ],
        usage_metadata={
            "input_tokens": 152_744,
            "output_tokens": 7_517,
            "total_tokens": 160_261,
            "output_token_details": {"reasoning": 1_000},
        },
    )

    async def search_handler(_request):
        return ModelResponse(result=[search_answer])

    search_response = await context_usage.ContextUsageMiddleware(
        "supervisor"
    ).awrap_model_call(search_request, search_handler)
    search_update = search_response.command.update
    stable_after_search = used_before + 7_517

    # Provider-internal page input remains usage/accounting data, but must not
    # inflate the replayable context shown in the context-window indicator.
    assert search_update["context_usage"]["used_tokens"] == stable_after_search
    assert search_update["_context_token_anchor"]["current_tokens"] == (
        stable_after_search
    )
    assert events[-1]["used_tokens"] == stable_after_search

    tool_result = ToolMessage(
        content="已保存一条可重放的工具结果",
        tool_call_id="tool-1",
        id="tool-1-result",
    )
    next_messages = [human, search_answer, tool_result]
    next_state = {
        "messages": next_messages,
        "_context_token_anchor": search_update["_context_token_anchor"],
    }
    next_request = _request(next_messages, state=next_state)
    expected_before_next_call = (
        stable_after_search + context_usage.estimate_message_tokens(tool_result)
    )
    assert context_usage.estimate_request_tokens(next_request) == (
        expected_before_next_call
    )

    ordinary_answer = AIMessage(
        content="最终回答",
        id="ai-final",
        usage_metadata={
            "input_tokens": 40_193,
            "output_tokens": 3_665,
            "total_tokens": 43_858,
        },
    )

    async def ordinary_handler(_request):
        return ModelResponse(result=[ordinary_answer])

    ordinary_response = await context_usage.ContextUsageMiddleware(
        "supervisor"
    ).awrap_model_call(next_request, ordinary_handler)
    ordinary_update = ordinary_response.command.update
    assert ordinary_update["context_usage"]["used_tokens"] == 43_858
    assert ordinary_update["_context_token_anchor"]["current_tokens"] == 43_858


def test_anchor_is_rejected_after_summary_or_prompt_change() -> None:
    human = HumanMessage(content="question", id="human-1")
    answer = AIMessage(content="answer", id="ai-1")
    original = _request([human, answer])
    anchor = context_usage.ContextTokenAnchor(
        provider_version=3,
        model_name="gpt-test",
        prompt_fingerprint=context_usage.prompt_fingerprint(original),
        prefix_fingerprint=context_usage.fingerprint(
            context_usage.request_messages(original, original.messages)
        ),
        last_message_id="ai-1",
        current_tokens=900,
    )
    summary = HumanMessage(content="summary", id="summary-1")
    state = {
        "messages": [human, answer],
        "_context_token_anchor": anchor,
        "_summarization_event": {
            "summary_message": summary,
            "cutoff_index": 2,
            "file_path": None,
        },
    }
    summarized = _request([human, answer], state=state)

    assert context_usage.estimate_request_tokens(summarized) < 900

    changed_tools = _request(
        [human, answer],
        state={"messages": [human, answer], "_context_token_anchor": anchor},
        tools=[{"type": "web_search"}],
    )
    assert context_usage.estimate_request_tokens(changed_tools) < 900
