"""OpenAI Responses streaming extensions for hosted Web Search."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import openai
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models import base as openai_base
from langgraph.config import get_stream_writer

_WEB_SEARCH_PHASES = {
    "response.web_search_call.in_progress": "in_progress",
    "response.web_search_call.searching": "searching",
    "response.web_search_call.completed": "completed",
}
_MAX_WEB_SEARCH_SOURCES = 20


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(exclude_none=True, mode="json")
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _safe_source(source: Any) -> dict[str, str] | None:
    raw = _as_dict(source)
    url = raw.get("url")
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    title = raw.get("title")
    return {
        "title": title.strip() if isinstance(title, str) and title.strip() else parsed.netloc,
        "url": url,
    }


def normalize_web_search_event(chunk: Any) -> dict[str, Any] | None:
    """Convert raw OpenAI events into the stable custom-stream contract."""

    chunk_type = getattr(chunk, "type", None)
    phase = _WEB_SEARCH_PHASES.get(chunk_type)
    item: dict[str, Any] = {}
    if phase is None:
        if chunk_type != "response.output_item.done":
            return None
        item = _as_dict(getattr(chunk, "item", None))
        if item.get("type") != "web_search_call":
            return None
        phase = "completed"

    item_id = item.get("id") or getattr(chunk, "item_id", None)
    output_index = getattr(chunk, "output_index", None)
    sequence_number = getattr(chunk, "sequence_number", None)
    if not isinstance(item_id, str) or not isinstance(output_index, int):
        return None
    if not isinstance(sequence_number, int):
        return None

    event: dict[str, Any] = {
        "type": "web_search_progress",
        "phase": phase,
        "item_id": item_id,
        "output_index": output_index,
        "sequence_number": sequence_number,
    }
    action = item.get("action")
    if isinstance(action, dict):
        normalized_action = {
            key: action[key]
            for key in ("type", "query", "queries", "url", "pattern")
            if key in action
        }
        if normalized_action:
            event["action"] = normalized_action
        sources: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for source in action.get("sources", []):
            normalized = _safe_source(source)
            if normalized is None or normalized["url"] in seen_urls:
                continue
            seen_urls.add(normalized["url"])
            sources.append(normalized)
            if len(sources) >= _MAX_WEB_SEARCH_SOURCES:
                break
        if sources:
            event["sources"] = sources
    return event


def _stream_writer():
    # Direct model invocations (including unit tests) do not have a LangGraph writer.
    try:
        return get_stream_writer()
    except RuntimeError:
        return None


class ResponsesWebSearchChatOpenAI(ChatOpenAI):
    """Expose Responses Web Search lifecycle events without changing LC chunks."""

    async def _astream_responses(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        # This mirrors langchain-openai 1.4's converter loop so unsupported raw
        # lifecycle events can be observed while all standard chunks stay intact.
        kwargs["stream"] = True
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        try:
            if self.include_response_headers:
                raw_context_manager = (
                    await self.root_async_client.with_raw_response.responses.create(
                        **payload
                    )
                )
                context_manager = raw_context_manager.parse()
                headers = {"headers": dict(raw_context_manager.headers)}
            else:
                context_manager = await self.root_async_client.responses.create(**payload)
                headers = {}
            original_schema_obj = kwargs.get("response_format")
            writer = _stream_writer()

            async with context_manager as response:
                is_first_chunk = True
                current_index = -1
                current_output_index = -1
                current_sub_index = -1
                has_reasoning = False
                async for chunk in openai_base._astream_with_chunk_timeout(
                    response,
                    self.stream_chunk_timeout,
                    model_name=self.model_name,
                ):
                    if writer is not None:
                        custom_event = normalize_web_search_event(chunk)
                        if custom_event is not None:
                            writer(custom_event)
                    metadata = headers if is_first_chunk else {}
                    (
                        current_index,
                        current_output_index,
                        current_sub_index,
                        generation_chunk,
                    ) = openai_base._convert_responses_chunk_to_generation_chunk(
                        chunk,
                        current_index,
                        current_output_index,
                        current_sub_index,
                        schema=original_schema_obj,
                        metadata=metadata,
                        has_reasoning=has_reasoning,
                        output_version=self.output_version,
                    )
                    if generation_chunk:
                        if run_manager:
                            await run_manager.on_llm_new_token(
                                generation_chunk.text,
                                chunk=generation_chunk,
                            )
                        is_first_chunk = False
                        if "reasoning" in generation_chunk.message.additional_kwargs:
                            has_reasoning = True
                        yield generation_chunk
        except openai.BadRequestError as exc:
            openai_base._handle_openai_bad_request(exc)
        except openai.APIError as exc:
            openai_base._handle_openai_api_error(exc)


__all__ = ["ResponsesWebSearchChatOpenAI", "normalize_web_search_event"]
