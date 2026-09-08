"""Wire validation for durable Mongo failure reviews."""

import json
from types import SimpleNamespace

import httpx
import pytest
from blockbuster import blockbuster_ctx
from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain_core.messages import HumanMessage

from deep_data_research_agent.core.config import HarnessChatOpenAI
from deep_data_research_agent.core.review_capture import (
    ACTIVE_CAPTURE,
    TransientCapture,
    capture_request,
    capture_response,
    usage_values,
)
from deep_data_research_agent.memory import service


@pytest.mark.asyncio
async def test_stream_capture_preserves_reply_and_cache_usage():
    # SDK 负责拼接回复；捕获只保留完成状态和原始 usage，不再额外累计消息。
    events = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "reasoning_content": "核对"},
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {"index": 0, "delta": {"content": "完成"}, "finish_reason": None}
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "total_tokens": 1010,
                "prompt_cache_hit_tokens": 900,
            },
        },
    ]
    body = (
        "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        + "data: [DONE]\n\n"
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, text=body, headers={"content-type": "text/event-stream"}
            )
        ),
        event_hooks={"request": [capture_request], "response": [capture_response]},
    ) as client:
        model = HarnessChatOpenAI(
            model="test", api_key="offline", http_async_client=client, max_retries=0
        )
        capture = TransientCapture()
        token = ACTIVE_CAPTURE.set(capture)
        try:
            chunks = [chunk async for chunk in model.astream("核对数据")]
        finally:
            ACTIVE_CAPTURE.reset(token)
    assert "".join(chunk.content for chunk in chunks) == "完成"
    assert any(
        chunk.additional_kwargs.get("reasoning_content") == "核对" for chunk in chunks
    )
    assert capture.complete(capture.calls[0])
    assert usage_values(capture.calls[0]["response"])["cached_tokens"] == 900
    assert (
        not {"message", "messages", "kwargs", "constructed_request"}
        & capture.calls[0].keys()
    )


@pytest.mark.asyncio
async def test_supervisor_captures_and_enqueues_wire_without_local_report(monkeypatch):
    queued, sent = [], []

    class Queue:
        async def get_memory_settings(self, _identity_hash):
            return service.MemorySettings()

        async def enqueue(self, **kwargs):
            queued.append(kwargs)
            return True

    monkeypatch.setattr(service, "MEMORY_QUEUE", Queue())
    monkeypatch.setattr(service, "user_hash", lambda _runtime: "a" * 64)
    monkeypatch.setattr(service, "user_identity", lambda _runtime: "offline-user")
    monkeypatch.setattr(service, "thread_id_from_runtime", lambda _runtime: "thread-a")
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(failure_review_bundle_max_bytes=2 * 1024 * 1024),
    )

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"x-request-id": "req-front"},
            json={
                "id": "chat-test",
                "object": "chat.completion",
                "created": 0,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "你好",
                        },
                    }
                ],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        event_hooks={"request": [capture_request], "response": [capture_response]},
    ) as client:
        model = HarnessChatOpenAI(
            model="test",
            api_key="offline-secret",
            base_url="https://offline.invalid",
            http_async_client=client,
            max_retries=0,
        )
        agent = create_deep_agent(
            model=model,
            tools=[],
            backend=StateBackend(),
            middleware=[
                service.FailureReviewMiddleware(
                    agent_name="supervisor",
                    reviewable_tools=set(),
                )
            ],
        )
        # 验证真实 Agent 中间件链，不依赖已经移除的本地捕获上下文。
        with blockbuster_ctx():
            await agent.ainvoke({"messages": [HumanMessage(content="你好")]})

    assert len(sent) == len(queued) == 1
    snapshot = queued[0]["payload"]["snapshot"]
    assert queued[0]["scope"] == "supervisor"
    assert snapshot["actual_request"] == sent[0]
    assert snapshot["actual_response"]["choices"][0]["message"]["content"] == "你好"
    assert snapshot["foreground_request_id"] == "req-front"
    assert ACTIVE_CAPTURE.get() is None


def test_transient_capture_allows_only_stream_and_output_limit_changes():
    baseline = {
        "request": {
            "messages": [{"role": "user", "content": "original"}],
            "tools": [{"type": "function", "function": {"name": "inspect"}}],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": 8192,
        },
        "response": {},
    }
    capture = TransientCapture(phase="review", baseline=baseline)
    capture.begin()
    capture.wire_request(
        {
            "messages": [
                {"role": "user", "content": "original"},
                {"role": "user", "content": "review"},
            ],
            "tools": baseline["request"]["tools"],
            "stream": False,
            "max_tokens": 4096,
        }
    )
    assert capture.prefix_verified is True

    changed = TransientCapture(phase="review", baseline=baseline)
    changed.begin()
    with pytest.raises(ValueError, match="unexpected_parameter_difference"):
        changed.wire_request(
            {
                "messages": [
                    {"role": "user", "content": "original"},
                    {"role": "user", "content": "review"},
                ],
                "tools": [],
                "stream": False,
                "max_tokens": 4096,
            }
        )
    assert changed.calls[-1]["validation_error"].startswith(
        "unexpected_parameter_difference:"
    )
