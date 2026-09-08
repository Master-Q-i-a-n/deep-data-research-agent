"""In-memory wire capture for durable failure reviews, isolated per run."""

from __future__ import annotations

import asyncio
import copy
import json
from contextvars import ContextVar

import httpx
from langchain_core.messages import AIMessage

ACTIVE_CAPTURE: ContextVar = ContextVar("failure_review_capture", default=None)


class TransientCapture:
    """In-memory capture used by durable Mongo review snapshots."""

    def __init__(self, *, phase: str = "foreground", baseline: dict | None = None):
        self.phase = phase
        self.baseline = baseline
        self.calls: list[dict] = []
        self.prefix_verified = False

    def begin(self):
        # 仅保留实际 wire 请求与响应；本地报告已移除，无需复制中间消息。
        self.calls.append({})

    def wire_request(self, payload):
        call = self.calls[-1]
        call["request"] = copy.deepcopy(payload)
        if self.phase != "review" or self.baseline is None:
            return
        base = self.baseline["request"]
        field = "input" if "input" in base else "messages"
        if payload.get(field, [])[: len(base[field])] != base[field]:
            call["validation_error"] = "prefix_mismatch"
            raise ValueError(call["validation_error"])
        # Only the streaming flags and output limit may differ during review.
        # Normalize protocol-specific locations before comparing the rest.
        before = self._stable_parameters(base, field)
        after = self._stable_parameters(payload, field)
        if before != after:
            changed = sorted(
                key
                for key in before.keys() | after.keys()
                if before.get(key) != after.get(key)
            )
            call["validation_error"] = "unexpected_parameter_difference:" + ",".join(
                changed
            )
            raise ValueError(call["validation_error"])
        self.prefix_verified = True

    @staticmethod
    def _stable_parameters(payload, content_field):
        normalized = copy.deepcopy(payload)
        normalized.pop(content_field, None)
        for key in (
            "stream",
            "stream_options",
            "max_tokens",
            "max_completion_tokens",
            "max_output_tokens",
        ):
            normalized.pop(key, None)
        extra_body = normalized.get("extra_body")
        if isinstance(extra_body, dict):
            extra_body.pop("max_tokens", None)
            extra_body.pop("max_output_tokens", None)
            if not extra_body:
                normalized.pop("extra_body", None)
        return normalized

    def raw(self, value):
        body = value.get("response", value)
        if body.get("usage") is not None or "output" in body or "choices" in body:
            self.calls[-1]["response"] = copy.deepcopy(body)
        choices = value.get("choices") or []
        if choices and choices[0].get("finish_reason"):
            self.calls[-1]["finish_reason"] = choices[0]["finish_reason"]

    def complete(self, call):
        body = call.get("response", {})
        if "input" in call.get("request", {}):
            return body.get("status") == "completed"
        choices = body.get("choices") or []
        finish = call.get("finish_reason") or (
            choices[0].get("finish_reason") if choices else None
        )
        return finish == "stop"


def usage_values(body: dict) -> dict:
    usage = body.get("usage") or {}
    inputs = usage.get("input_tokens", usage.get("prompt_tokens"))
    cached = usage.get("prompt_cache_hit_tokens")
    if cached is None:
        cached = (
            usage.get("input_tokens_details")
            or usage.get("prompt_tokens_details")
            or {}
        ).get("cached_tokens")
    output = usage.get("output_tokens", usage.get("completion_tokens"))
    return {
        "input_tokens": inputs,
        "cached_tokens": cached,
        "uncached_tokens": inputs - cached
        if type(inputs) is int and type(cached) is int
        else None,
        "output_tokens": output,
    }


async def capture_request(request: httpx.Request) -> None:
    capture = ACTIVE_CAPTURE.get()
    if capture is not None:
        await asyncio.to_thread(capture.wire_request, json.loads(request.content))


async def capture_response(response: httpx.Response) -> None:
    capture = ACTIVE_CAPTURE.get()
    if capture is None:
        return
    call = capture.calls[-1]
    call["request_id"] = response.headers.get("x-request-id")
    call["http_status"] = response.status_code
    if response.is_success and "text/event-stream" in response.headers.get(
        "content-type", ""
    ):
        if response.is_stream_consumed:
            # MockTransport can return an already-buffered body.
            for line in response.content.splitlines():
                if line.startswith(b"data:") and line[5:].strip() != b"[DONE]":
                    await asyncio.to_thread(capture.raw, json.loads(line[5:]))
        else:
            response.stream = _CaptureStream(response.stream, capture)
    else:
        await response.aread()
        try:
            await asyncio.to_thread(capture.raw, response.json())
        except ValueError:
            # 非 JSON 错误响应由 SDK 处理，捕获不改变调用结果。
            pass


class _CaptureStream(httpx.AsyncByteStream):
    """Tee SSE bytes without pre-reading or delaying delivery to the SDK."""

    def __init__(self, stream, capture):
        self.stream, self.capture = stream, capture

    async def __aiter__(self):
        pending = b""
        async for chunk in self.stream:
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if line.startswith(b"data:"):
                    value = line[5:].strip()
                    if value and value != b"[DONE]":
                        try:
                            await asyncio.to_thread(self.capture.raw, json.loads(value))
                        except ValueError:
                            # 原始字节继续交给 SDK，捕获忽略无法解析的事件。
                            pass
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class ReviewCaptureMixin:
    """Capture after serialization; preserve compatible-provider reasoning."""

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        capture = ACTIVE_CAPTURE.get()
        if capture is None:
            return payload
        messages = self._convert_input(input_).to_messages()
        if "messages" in payload:
            for message, wire in zip(messages, payload["messages"], strict=True):
                if (
                    isinstance(message, AIMessage)
                    and "reasoning_content" in message.additional_kwargs
                ):
                    wire["reasoning_content"] = message.additional_kwargs[
                        "reasoning_content"
                    ]
        # Responses store=False may strip plaintext reasoning on serialization.
        # For replay, use the exact captured wire prefix and raw final output.
        if capture.phase == "review":
            base = capture.baseline
            field = "input" if "input" in base["request"] else "messages"
            if field == "input":
                output = base.get("response", {}).get("output")
                if output is None:
                    raise ValueError("missing_raw_responses_output")
                payload[field] = (
                    copy.deepcopy(base["request"][field])
                    + copy.deepcopy(output)
                    + [payload[field][-1]]
                )
        capture.begin()
        return payload

    def _convert_chunk_to_generation_chunk(
        self, chunk, default_chunk_class, base_generation_info
    ):
        result = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if ACTIVE_CAPTURE.get() is not None and result is not None:
            choices = chunk.get("choices") or []
            if choices:
                reasoning = (choices[0].get("delta") or {}).get("reasoning_content")
                if reasoning is not None:
                    result.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        if ACTIVE_CAPTURE.get() is not None:
            body = response if isinstance(response, dict) else response.model_dump()
            for generation, choice in zip(
                result.generations, body["choices"], strict=True
            ):
                if "reasoning_content" in choice["message"]:
                    generation.message.additional_kwargs["reasoning_content"] = choice[
                        "message"
                    ]["reasoning_content"]
        return result
