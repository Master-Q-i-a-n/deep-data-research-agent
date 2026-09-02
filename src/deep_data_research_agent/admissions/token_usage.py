"""PostgreSQL-backed model token accounting with Redis cache synchronization."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage
from langgraph.config import get_config

from deep_data_research_agent.admissions import redis_limits
from deep_data_research_agent.core.config import get_settings
from deep_data_research_agent.core.identity import (
    user_identity,
    user_identity_from_config,
)
from deep_data_research_agent.database import repository as database
from deep_data_research_agent.providers.context_usage import (
    effective_request_messages,
    estimate_messages_tokens,
)

logger = logging.getLogger(__name__)


def estimate_tokens(model: Any, messages: Sequence[Any]) -> int:
    """Estimate without calling a Provider-specific tokenizer."""

    del model
    return estimate_messages_tokens(messages)


def _output_reservation(model: Any, model_settings: Mapping[str, Any] | None) -> int:
    values: list[Any] = []
    if model_settings:
        values.extend(
            model_settings.get(name) for name in ("max_output_tokens", "max_tokens")
        )
    values.extend(
        getattr(model, name, None) for name in ("max_output_tokens", "max_tokens")
    )
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return get_settings().token_reservation_output_tokens


async def _usage_from_message(
    message: AIMessage,
    model: Any,
) -> tuple[int, int, int, str]:
    usage = message.usage_metadata or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    if total_tokens > 0:
        return input_tokens, output_tokens, total_tokens, "provider"
    if input_tokens > 0 or output_tokens > 0:
        return input_tokens, output_tokens, input_tokens + output_tokens, "provider"
    estimated_output = await asyncio.to_thread(estimate_tokens, model, [message])
    return 0, estimated_output, estimated_output, "estimated"


def _runtime_fields(config: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    raw = dict(config or {})
    configurable = raw.get("configurable")
    metadata = raw.get("metadata")
    configurable = configurable if isinstance(configurable, Mapping) else {}
    metadata = metadata if isinstance(metadata, Mapping) else {}
    thread_id = configurable.get("thread_id") or metadata.get("thread_id")
    root_run_id = metadata.get("token_budget_session_id") or metadata.get("run_id")
    return (
        str(root_run_id) if root_run_id else None,
        str(thread_id) if thread_id else None,
    )


async def _sync_bucket(user_id: str, bucket: database.TokenBucketRecord) -> None:
    try:
        await redis_limits.sync_token_bucket(
            user_id,
            balance_tokens=bucket.balance_tokens,
            last_refill_hour=bucket.last_refill_hour,
            version=bucket.version,
        )
    except Exception:
        # PostgreSQL is authoritative. Existing runs must not fail only because
        # the Redis cache is temporarily unavailable.
        logger.warning("模型 Token 账本已写入，但 Redis 缓存同步失败", exc_info=True)


async def _reserve(
    *,
    user_id: str,
    agent_name: str,
    model: Any,
    messages: Sequence[Any],
    tools: Sequence[Any] = (),
    model_settings: Mapping[str, Any] | None,
    root_run_id: str | None,
    thread_id: str | None,
) -> database.TokenUsageReservation:
    estimated_input = await asyncio.to_thread(
        estimate_messages_tokens,
        messages,
        tools=tools,
    )
    reserved = estimated_input + _output_reservation(model, model_settings)
    reservation = await database.reserve_model_tokens(
        call_id=str(uuid4()),
        user_id=user_id,
        root_run_id=root_run_id,
        thread_id=thread_id,
        agent_name=agent_name,
        model_name=str(
            getattr(model, "model_name", None) or getattr(model, "model", "unknown")
        ),
        reserved_tokens=reserved,
    )
    await _sync_bucket(user_id, reservation.bucket)
    return reservation


async def _settle(
    reservation: database.TokenUsageReservation,
    *,
    user_id: str,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int,
    usage_source: str,
    status: str = "settled",
) -> None:
    bucket = await database.settle_model_tokens(
        call_id=reservation.call_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        usage_source=usage_source,
        status=status,
    )
    await _sync_bucket(user_id, bucket)


class TokenUsageMiddleware(AgentMiddleware):
    """Meter every Agent model call without stopping an already-admitted run."""

    def __init__(self, *, agent_name: str) -> None:
        self._agent_name = agent_name

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        # LangGraph Runtime intentionally has no `config` attribute. Runnable
        # configuration remains available through the context variable.
        try:
            config = get_config()
        except RuntimeError:
            config = getattr(request.runtime, "config", {}) or {}
        user_id = (
            user_identity(request.runtime)
            if request.runtime is not None
            else user_identity_from_config(config)
        )
        root_run_id, thread_id = _runtime_fields(config)
        execution_info = getattr(request.runtime, "execution_info", None)
        root_run_id = root_run_id or getattr(execution_info, "run_id", None)
        thread_id = thread_id or getattr(execution_info, "thread_id", None)
        effective_messages = effective_request_messages(request)
        messages = ([request.system_message] if request.system_message else []) + list(
            effective_messages
        )
        reservation = await _reserve(
            user_id=user_id,
            agent_name=self._agent_name,
            model=request.model,
            messages=messages,
            tools=request.tools,
            model_settings=request.model_settings,
            root_run_id=root_run_id,
            thread_id=thread_id,
        )
        try:
            response = await handler(request)
        except (Exception, asyncio.CancelledError):
            # A server reload cancels in-flight calls with CancelledError, which is
            # outside Exception on modern Python. Keep the conservative reservation
            # but always leave a terminal ledger row.
            await _settle(
                reservation,
                user_id=user_id,
                input_tokens=None,
                output_tokens=None,
                total_tokens=reservation.reserved_tokens,
                usage_source="reserved",
                status="failed",
            )
            raise

        message = next(
            (item for item in reversed(response.result) if isinstance(item, AIMessage)),
            None,
        )
        if message is None:
            usage = (0, 0, reservation.reserved_tokens, "reserved")
        else:
            usage = await _usage_from_message(message, request.model)
            if usage[3] == "estimated":
                # Provider-less output estimates still need the already estimated input.
                estimated_input = await asyncio.to_thread(
                    estimate_tokens,
                    request.model,
                    messages,
                )
                usage = (
                    estimated_input,
                    usage[1],
                    estimated_input + usage[1],
                    "estimated",
                )
        await _settle(
            reservation,
            user_id=user_id,
            input_tokens=usage[0],
            output_tokens=usage[1],
            total_tokens=usage[2],
            usage_source=usage[3],
        )
        return response


async def metered_model_ainvoke(
    model: Any,
    input_value: Any,
    *,
    user_id: str,
    agent_name: str,
    root_run_id: str | None = None,
    thread_id: str | None = None,
    config: dict[str, Any] | None = None,
    model_settings: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Meter direct background-model calls that do not pass through an Agent."""

    messages = input_value if isinstance(input_value, list) else [input_value]
    reservation = await _reserve(
        user_id=user_id,
        agent_name=agent_name,
        model=model,
        messages=messages,
        model_settings=model_settings,
        root_run_id=root_run_id,
        thread_id=thread_id,
    )
    try:
        result = await model.ainvoke(input_value, config=config, **kwargs)
    except (Exception, asyncio.CancelledError):
        await _settle(
            reservation,
            user_id=user_id,
            input_tokens=None,
            output_tokens=None,
            total_tokens=reservation.reserved_tokens,
            usage_source="reserved",
            status="failed",
        )
        raise
    raw_message = result if isinstance(result, AIMessage) else None
    if isinstance(result, Mapping) and isinstance(result.get("raw"), AIMessage):
        raw_message = result["raw"]
    if raw_message is None:
        input_tokens, output_tokens = await asyncio.gather(
            asyncio.to_thread(estimate_tokens, model, messages),
            asyncio.to_thread(estimate_tokens, model, [result]),
        )
        usage = (input_tokens, output_tokens, input_tokens + output_tokens, "estimated")
    else:
        usage = await _usage_from_message(raw_message, model)
        if usage[3] == "estimated":
            input_tokens = await asyncio.to_thread(estimate_tokens, model, messages)
            usage = (input_tokens, usage[1], input_tokens + usage[1], "estimated")
    await _settle(
        reservation,
        user_id=user_id,
        input_tokens=usage[0],
        output_tokens=usage[1],
        total_tokens=usage[2],
        usage_source=usage[3],
    )
    return result
