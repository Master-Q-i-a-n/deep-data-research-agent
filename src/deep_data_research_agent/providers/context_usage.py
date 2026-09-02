"""Provider-neutral context-window estimation and checkpoint state."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, NotRequired, TypedDict
from uuid import uuid4

from langchain.agents.middleware import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain.agents.middleware.types import (
    AgentState,
    OmitFromInput,
    PrivateStateAttr,
)
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.config import get_stream_writer
from langgraph.types import Command

_ASCII_TOKEN_WEIGHT = 0.3
_NON_ASCII_TOKEN_WEIGHT = 0.6
_MESSAGE_OVERHEAD = 4
_TOOL_OVERHEAD = 8
_IMAGE_TOKEN_ESTIMATE = 85


class ContextTokenAnchor(TypedDict):
    """Token estimate anchored to an exact replayable prefix in one thread."""

    provider_version: int
    model_name: str
    prompt_fingerprint: str
    prefix_fingerprint: str
    last_message_id: str
    current_tokens: int


class ContextUsageSnapshot(TypedDict):
    """Small public state exposed to the frontend through LangGraph values."""

    used_tokens: int
    max_input_tokens: int
    provider_version: int


class ContextUsageState(AgentState):
    """Persist the anchor privately while exposing only the display snapshot."""

    _context_token_anchor: Annotated[
        NotRequired[ContextTokenAnchor | None], PrivateStateAttr
    ]
    context_usage: NotRequired[Annotated[ContextUsageSnapshot | None, OmitFromInput]]


def estimate_text_tokens(text: str) -> int:
    """Estimate text using the project-wide English/Chinese character ratios."""

    weighted = sum(
        _ASCII_TOKEN_WEIGHT if ord(character) < 128 else _NON_ASCII_TOKEN_WEIGHT
        for character in text
    )
    return math.ceil(weighted)


def _usage_value(message: AIMessage, key: str) -> int:
    usage = message.usage_metadata or {}
    try:
        return max(0, int(usage.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _has_replayable_reasoning(message: AIMessage) -> bool:
    """Require encrypted reasoning when Provider usage says hidden reasoning exists."""

    details = (message.usage_metadata or {}).get("output_token_details") or {}
    try:
        reasoning_tokens = int(details.get("reasoning") or 0)
    except (AttributeError, TypeError, ValueError):
        reasoning_tokens = 0
    if reasoning_tokens <= 0:
        return True
    if not isinstance(message.content, list):
        return False
    return any(
        isinstance(block, Mapping)
        and block.get("type") == "reasoning"
        and isinstance(block.get("encrypted_content"), str)
        and bool(block["encrypted_content"])
        for block in message.content
    )


def _contains_hosted_web_search(messages: Sequence[Any]) -> bool:
    """Detect Responses API search calls whose internal search input is not replayed."""

    return any(
        isinstance(message, AIMessage)
        and isinstance(message.content, list)
        and any(
            isinstance(block, Mapping) and block.get("type") == "web_search_call"
            for block in message.content
        )
        for message in messages
    )


def _text_payload(value: Any) -> str:
    """Serialize visible replay payload without counting encrypted blobs as text."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, Mapping):
        parts: list[str] = []
        for key, item in value.items():
            if key in {"encrypted_content", "usage_metadata", "response_metadata"}:
                continue
            if key in {"image_url", "file", "input_image"}:
                parts.append(
                    " " * math.ceil(_IMAGE_TOKEN_ESTIMATE / _ASCII_TOKEN_WEIGHT)
                )
                continue
            parts.append(str(key))
            parts.append(_text_payload(item))
        return " ".join(parts)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return " ".join(_text_payload(item) for item in value)
    return str(value)


def estimate_message_tokens(message: Any, *, prefer_usage: bool = True) -> int:
    """Estimate one replayed message, using its real output usage when safe."""

    if (
        isinstance(message, AIMessage)
        and prefer_usage
        and _has_replayable_reasoning(message)
    ):
        output_tokens = _usage_value(message, "output_tokens")
        if output_tokens > 0:
            return output_tokens

    if isinstance(message, BaseMessage):
        payload: dict[str, Any] = {
            "type": message.type,
            "content": message.content,
        }
        for name in ("name", "tool_call_id", "tool_calls", "invalid_tool_calls"):
            value = getattr(message, name, None)
            if value:
                payload[name] = value
    else:
        payload = {"content": message}
    return max(1, estimate_text_tokens(_text_payload(payload)) + _MESSAGE_OVERHEAD)


def estimate_messages_tokens(
    messages: Sequence[Any],
    *,
    tools: Sequence[Any] = (),
) -> int:
    """Estimate messages and the tool definitions sent with the request."""

    count = sum(estimate_message_tokens(message) for message in messages)
    for tool in tools:
        schema = _tool_schema(tool)
        count += estimate_text_tokens(_text_payload(schema)) + _TOOL_OVERHEAD
    return max(1, count)


def _tool_schema(tool: Any) -> Any:
    """Create one stable schema for estimation and anchor validation."""

    try:
        return tool if isinstance(tool, Mapping) else convert_to_openai_tool(tool)
    except (AttributeError, TypeError, ValueError):
        return str(tool)


def effective_request_messages(request: ModelRequest) -> list[Any]:
    """Apply the current DeepAgents summary event before estimating input."""

    messages = list(request.messages)
    event = request.state.get("_summarization_event")
    if not isinstance(event, Mapping):
        return messages
    summary = event.get("summary_message")
    cutoff = event.get("cutoff_index")
    if summary is None or not isinstance(cutoff, int) or cutoff < 0:
        return messages
    if cutoff > len(messages):
        return [summary]
    return [summary, *messages[cutoff:]]


def request_messages(
    request: ModelRequest, messages: Sequence[Any] | None = None
) -> list[Any]:
    """Return the complete model input, including the system message."""

    selected = (
        list(messages) if messages is not None else effective_request_messages(request)
    )
    return ([request.system_message] if request.system_message else []) + selected


def _message_identity(message: Any) -> str:
    message_id = getattr(message, "id", None)
    return str(message_id) if message_id else ""


def _fingerprint_value(value: Any) -> Any:
    if isinstance(value, BaseMessage):
        result: dict[str, Any] = {
            "type": value.type,
            "id": _message_identity(value),
            "content": value.content,
        }
        for name in ("name", "tool_call_id", "tool_calls", "invalid_tool_calls"):
            item = getattr(value, name, None)
            if item:
                result[name] = item
        return result
    if isinstance(value, Mapping):
        return {
            str(key): _fingerprint_value(item)
            for key, item in value.items()
            if key not in {"usage_metadata", "response_metadata"}
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_fingerprint_value(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _fingerprint_value(model_dump(mode="json", exclude_none=True))
        except (TypeError, ValueError):
            return str(value)
    return (
        value
        if isinstance(value, (str, int, float, bool)) or value is None
        else str(value)
    )


def fingerprint(values: Sequence[Any]) -> str:
    payload = json.dumps(
        _fingerprint_value(list(values)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prompt_fingerprint(request: ModelRequest) -> str:
    return fingerprint(
        [
            request.system_message,
            [_tool_schema(tool) for tool in request.tools],
            request.model_settings,
        ]
    )


def model_context_limit(model: Any) -> int | None:
    profile = getattr(model, "profile", None)
    value = profile.get("max_input_tokens") if isinstance(profile, Mapping) else None
    return value if isinstance(value, int) and value > 0 else None


def provider_version(model: Any) -> int:
    value = getattr(model, "_deep_data_provider_version", 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def model_name(model: Any) -> str:
    return str(getattr(model, "model_name", None) or getattr(model, "model", "unknown"))


def estimate_request_tokens(
    request: ModelRequest,
    messages: Sequence[Any] | None = None,
    *,
    allow_anchor: bool = True,
) -> int:
    """Use a trusted prefix anchor, otherwise estimate the entire effective request."""

    complete = request_messages(request, messages)
    return _estimate_complete_tokens(request, complete, allow_anchor=allow_anchor)


def _estimate_complete_tokens(
    request: ModelRequest,
    complete: Sequence[Any],
    *,
    allow_anchor: bool,
) -> int:
    anchor = request.state.get("_context_token_anchor")
    if allow_anchor and isinstance(anchor, Mapping):
        expected = (
            anchor.get("provider_version") == provider_version(request.model)
            and anchor.get("model_name") == model_name(request.model)
            and anchor.get("prompt_fingerprint") == prompt_fingerprint(request)
        )
        last_id = anchor.get("last_message_id")
        if expected and isinstance(last_id, str) and last_id:
            for index in range(len(complete) - 1, -1, -1):
                if _message_identity(complete[index]) != last_id:
                    continue
                prefix = complete[: index + 1]
                if fingerprint(prefix) == anchor.get("prefix_fingerprint"):
                    remainder = complete[index + 1 :]
                    return max(
                        1,
                        int(anchor.get("current_tokens") or 0)
                        + estimate_messages_tokens(remainder),
                    )
                break
    return estimate_messages_tokens(complete, tools=request.tools)


class ContextTokenCounter:
    """DeepAgents token counter that anchors only the complete model request."""

    def __init__(self, request: ModelRequest) -> None:
        self.request = request
        self._expected_fingerprint: str | None = None

    def bind(self, effective_messages: Sequence[Any]) -> None:
        self._expected_fingerprint = fingerprint(
            request_messages(self.request, effective_messages)
        )

    def __call__(self, messages: Sequence[Any], **kwargs: Any) -> int:
        system = kwargs.get("system") or kwargs.get("system_message")
        tools = kwargs.get("tools") or ()
        complete = ([system] if system else []) + list(messages)
        if (
            self._expected_fingerprint
            and fingerprint(complete) == self._expected_fingerprint
        ):
            return _estimate_complete_tokens(
                self.request,
                complete,
                allow_anchor=True,
            )
        return estimate_messages_tokens(complete, tools=tools)


def _stream_writer():
    try:
        return get_stream_writer()
    except RuntimeError:
        return None


def _usage_event(snapshot: ContextUsageSnapshot, phase: str) -> dict[str, Any]:
    return {"type": "context_usage", "phase": phase, **snapshot}


class ContextUsageMiddleware(AgentMiddleware):
    """Persist a replayable-context anchor and stream Supervisor context usage."""

    state_schema = ContextUsageState

    def __init__(self, role: str) -> None:
        self.role = role

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        used_before = estimate_request_tokens(request, request.messages)
        limit = model_context_limit(request.model)
        version = provider_version(request.model)
        writer = _stream_writer() if self.role == "supervisor" else None
        if writer is not None and limit is not None:
            writer(
                _usage_event(
                    ContextUsageSnapshot(
                        used_tokens=used_before,
                        max_input_tokens=limit,
                        provider_version=version,
                    ),
                    "before_model",
                )
            )

        response = await handler(request)
        model_response = (
            response.model_response
            if isinstance(response, ExtendedModelResponse)
            else response
        )
        result = list(model_response.result)
        # LangGraph would assign IDs while reducing messages. Assign them here
        # so the checkpoint anchor references those same persisted messages.
        for item in result:
            if isinstance(item, BaseMessage) and not _message_identity(item):
                item.id = str(uuid4())
        ai_message = next(
            (message for message in reversed(result) if isinstance(message, AIMessage)),
            None,
        )
        update: dict[str, Any] = {"_context_token_anchor": None}
        replayable_result_tokens = sum(estimate_message_tokens(item) for item in result)
        used_after = used_before + replayable_result_tokens
        contains_hosted_search = _contains_hosted_web_search(result)
        can_anchor_prefix = contains_hosted_search
        if ai_message is not None:
            input_tokens = _usage_value(ai_message, "input_tokens")
            if input_tokens > 0 and not contains_hosted_search:
                # Ordinary calls expose an input count that matches the replayed
                # request, so it is the strongest available calibration point.
                used_after = input_tokens + replayable_result_tokens
                can_anchor_prefix = True

        if can_anchor_prefix:
            complete_after = [*request_messages(request, request.messages), *result]
            last_id = next(
                (
                    _message_identity(item)
                    for item in reversed(result)
                    if _message_identity(item)
                ),
                "",
            )
            if last_id:
                # A hosted search may count provider-internal page material in
                # input_tokens. Anchor its replayable projection instead; the next
                # ordinary response will replace it with a real input-token anchor.
                update["_context_token_anchor"] = ContextTokenAnchor(
                    provider_version=version,
                    model_name=model_name(request.model),
                    prompt_fingerprint=prompt_fingerprint(request),
                    prefix_fingerprint=fingerprint(complete_after),
                    last_message_id=last_id,
                    current_tokens=used_after,
                )

        if self.role == "supervisor":
            snapshot = (
                ContextUsageSnapshot(
                    used_tokens=max(1, used_after),
                    max_input_tokens=limit,
                    provider_version=version,
                )
                if limit is not None
                else None
            )
            update["context_usage"] = snapshot
            if writer is not None and snapshot is not None:
                writer(_usage_event(snapshot, "after_model"))

        return ExtendedModelResponse(
            model_response=model_response,
            command=Command(update=update),
        )


__all__ = [
    "ContextTokenCounter",
    "ContextUsageMiddleware",
    "ContextUsageSnapshot",
    "ContextUsageState",
    "effective_request_messages",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "estimate_request_tokens",
    "estimate_text_tokens",
    "model_context_limit",
]
