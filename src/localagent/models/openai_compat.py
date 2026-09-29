"""Streaming client for OpenAI-compatible chat-completions endpoints.

Works with LM Studio, Ollama, vLLM, llama.cpp and hosted OpenAI-compatible
APIs. Handles the non-standard ``reasoning_content`` stream, tool-call deltas,
per-request thinking toggles, and tool calls that a model wrote as plain text.
"""

import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from localagent.models.limiter import provider_slot
from localagent.models.registry import ModelSpec, Provider
from localagent.models.thinking import ThinkingGuard
from localagent.models.types import (
    Cancelled,
    ChatResponse,
    ContextOverflowError,
    ModelError,
    ToolCall,
    Usage,
)

DeltaCallback = Callable[[str, str], None]

_OVERFLOW = re.compile(
    r"context (length|window|size)|n_ctx|too many tokens|exceeds? the (maximum|context)|context.*overflow",
    re.IGNORECASE,
)
_TOOL_CALL_BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_XML_FUNCTION = re.compile(r"<function=([\w.-]+)>(.*?)</function>", re.DOTALL)
_XML_PARAMETER = re.compile(r"<parameter=([\w.-]+)>\n?(.*?)\n?</parameter>", re.DOTALL)


def _new_call_id() -> str:
    return "call_" + uuid.uuid4().hex[:24]


def _parse_json_call(text: str, tool_names: set[str]) -> ToolCall | None:
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("name") not in tool_names:
        return None
    args = obj.get("arguments", obj.get("parameters", {}))
    if isinstance(args, str):
        return ToolCall(_new_call_id(), obj["name"], args)
    return ToolCall(_new_call_id(), obj["name"], json.dumps(args, ensure_ascii=False))


def _parse_xml_call(text: str, tool_names: set[str]) -> ToolCall | None:
    match = _XML_FUNCTION.search(text)
    if not match or match.group(1) not in tool_names:
        return None
    args = {name: value for name, value in _XML_PARAMETER.findall(match.group(2))}
    return ToolCall(_new_call_id(), match.group(1), json.dumps(args, ensure_ascii=False))


def extract_text_tool_calls(content: str, tool_names: set[str]) -> tuple[list[ToolCall], str]:
    """Recover tool calls a model wrote into its text instead of the API field.

    Understands Hermes/Qwen ``<tool_call>{json}</tool_call>`` blocks, the
    Qwen3-Coder ``<function=...><parameter=...>`` XML form, and a reply that is
    nothing but one JSON call object. Only names in *tool_names* are accepted.

    Returns:
        The recovered calls and the content with those blocks removed.
    """
    calls: list[ToolCall] = []
    for block in _TOOL_CALL_BLOCK.findall(content):
        call = _parse_json_call(block, tool_names) or _parse_xml_call(block, tool_names)
        if call is not None:
            calls.append(call)
    if calls:
        return calls, _TOOL_CALL_BLOCK.sub("", content).strip()
    stripped = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if stripped.startswith("{"):
        call = _parse_json_call(stripped, tool_names)
        if call is not None:
            return [call], ""
    return [], content


class _ToolCallAccumulator:
    """Reassemble streamed ``tool_calls`` deltas, which arrive in fragments by index."""

    def __init__(self) -> None:
        self._parts: dict[int, dict[str, Any]] = {}

    def add(self, deltas: Iterable[dict[str, Any]]) -> None:
        for position, delta in enumerate(deltas):
            index = delta.get("index", position)
            part = self._parts.setdefault(index, {"id": "", "name": "", "arguments": "", "extra": None})
            if delta.get("id"):
                part["id"] = delta["id"]
            if delta.get("extra_content"):
                part["extra"] = delta["extra_content"]
            function = delta.get("function") or {}
            if function.get("name"):
                part["name"] += function["name"]
            if function.get("arguments"):
                args = function["arguments"]
                part["arguments"] += args if isinstance(args, str) else json.dumps(args)

    def calls(self) -> list[ToolCall]:
        return [
            ToolCall(p["id"] or _new_call_id(), p["name"], p["arguments"], p["extra"])
            for _, p in sorted(self._parts.items())
            if p["name"]
        ]


IMAGE_OMITTED = "(image not shown: the current model cannot view images)"
# Gemini's documented placeholder for function calls it did not produce itself (earlier
# turns by another model, after set_model): without a signature Gemini 3 rejects them.
GEMINI_SKIP_SIGNATURE = {"google": {"thought_signature": "skip_thought_signature_validator"}}


def is_gemini(base_url: str) -> bool:
    """Whether *base_url* is Google's OpenAI-compatible Gemini endpoint."""
    return "generativelanguage.googleapis.com" in base_url or "aiplatform.googleapis.com" in base_url


def _text_only(content: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """Replace image parts with a note; a content list that is all text becomes one string."""
    parts = [p if p.get("type") != "image_url" else {"type": "text", "text": IMAGE_OMITTED} for p in content]
    if all(p.get("type") == "text" for p in parts):
        return "\n\n".join(p.get("text", "") for p in parts)
    return parts


def adapt_messages(messages: list[dict[str, Any]], vision: bool, gemini: bool) -> list[dict[str, Any]]:
    """Fit a conversation to one model; the stored conversation itself is never changed.

    - Models without vision get a note in place of each image, so a conversation
      that a vision model has seen can continue on a text-only one.
    - Tool calls carry ``extra_content`` only for Gemini, which needs a thought
      signature on the first call of each turn; other servers may reject the field.
    """
    adapted = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and not vision:
            message = {**message, "content": _text_only(content)}
        calls = message.get("tool_calls")
        if calls:
            if gemini:
                if not any(c.get("extra_content") for c in calls):
                    calls = [{**calls[0], "extra_content": GEMINI_SKIP_SIGNATURE}, *calls[1:]]
            else:
                calls = [{k: v for k, v in c.items() if k != "extra_content"} for c in calls]
            message = {**message, "tool_calls": calls}
        adapted.append(message)
    return adapted


def _usage_from(data: dict[str, Any]) -> Usage:
    details = data.get("completion_tokens_details") or {}
    prompt_details = data.get("prompt_tokens_details") or {}
    return Usage(
        prompt_tokens=int(data.get("prompt_tokens") or 0),
        completion_tokens=int(data.get("completion_tokens") or 0),
        reasoning_tokens=int(details.get("reasoning_tokens") or 0),
        cached_tokens=int(prompt_details.get("cached_tokens") or 0),
    )


def _raise_for_error(status: int, body: str) -> None:
    message = f"HTTP {status}: {body[:500]}"
    if _OVERFLOW.search(body):
        raise ContextOverflowError(message, status=status)
    raise ModelError(message, retryable=status == 429 or status >= 500, status=status)


class OpenAICompatClient:
    """Chat client bound to one model on one provider."""

    def __init__(self, provider: Provider, model: ModelSpec):
        self.provider = provider
        self.model = model
        self._http = httpx.Client(timeout=httpx.Timeout(provider.timeout, connect=15.0))

    def close(self) -> None:
        self._http.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.provider.headers}
        key = self.provider.resolve_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        thinking: bool,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """Return the JSON body for a streamed chat-completions request."""
        payload: dict[str, Any] = {
            "model": self.model.model_id,
            "messages": adapt_messages(messages, self.model.vision, is_gemini(self.provider.base_url)),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": max_tokens or self.model.max_output_tokens,
        }
        if tools:
            payload["tools"] = tools
        payload.update(self.model.extra_params)
        if not thinking and self.model.thinking == "toggle":
            payload.update(self.model.thinking_off)
        return payload

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        thinking: bool = True,
        max_tokens: int | None = None,
        on_delta: DeltaCallback | None = None,
        on_wait: Callable[[], None] | None = None,
        stop_event: threading.Event | None = None,
        guard: ThinkingGuard | None = None,
    ) -> ChatResponse:
        """Send one streamed request and return the assembled response.

        Args:
            messages: OpenAI-format conversation.
            tools: OpenAI-format tool schemas.
            thinking: Whether the model may reason before answering.
            max_tokens: Output cap (default: the model's ``max_output_tokens``).
            on_delta: Receives ``("reasoning" | "content", text)`` as it streams.
            on_wait: Called if the request must wait for a provider slot.
            stop_event: When set, the request is aborted with ``Cancelled``.
            guard: Watches the thinking as it streams; when it has gone on too long or begun
                repeating itself the reply ends there with ``finish_reason="thinking"`` (closing the
                stream also stops the server generating), and the guard says why. Usage is left
                empty: the server never sent its final chunk.
        """
        payload = self.build_payload(messages, tools, thinking, max_tokens)
        url = self.provider.base_url.rstrip("/") + "/chat/completions"
        with provider_slot(self.provider.name, self.provider.max_concurrency, on_wait):
            start = time.monotonic()
            try:
                with self._http.stream("POST", url, headers=self._headers(), json=payload) as response:
                    if response.status_code >= 400:
                        _raise_for_error(response.status_code, response.read().decode("utf-8", "replace"))
                    result = self._consume(response, start, on_delta, stop_event, guard)
            except httpx.TransportError as e:
                raise ModelError(f"{type(e).__name__}: {e}", retryable=True) from e
        if not result.tool_calls and tools and result.content:
            names = {t["function"]["name"] for t in tools}
            result.tool_calls, result.content = extract_text_tool_calls(result.content, names)
        return result

    def _consume(
        self,
        response: httpx.Response,
        start: float,
        on_delta: DeltaCallback | None,
        stop_event: threading.Event | None,
        guard: ThinkingGuard | None = None,
    ) -> ChatResponse:
        result = ChatResponse()
        content: list[str] = []
        reasoning: list[str] = []
        stopped = False  # the guard ended the reply itself
        calls = _ToolCallAccumulator()
        for line in response.iter_lines():
            if stop_event is not None and stop_event.is_set():
                raise Cancelled()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except ValueError:
                continue  # a malformed chunk must not kill a task that is otherwise fine
            if not isinstance(chunk, dict):
                continue  # ...nor a stray value where an object was expected
            if chunk.get("error"):
                error = chunk["error"]
                _raise_for_error(500, error.get("message", str(error)) if isinstance(error, dict) else str(error))
            if chunk.get("usage"):
                result.usage = _usage_from(chunk["usage"])
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                think = delta.get("reasoning_content") or delta.get("reasoning")
                text = delta.get("content")
                if (think or text or delta.get("tool_calls")) and result.ttft is None:
                    result.ttft = time.monotonic() - start
                if think:
                    reasoning.append(think)
                    if on_delta:
                        on_delta("reasoning", think)
                    if guard is not None and guard.feed(think):
                        result.finish_reason = "thinking"
                        stopped = True
                        break
                if text:
                    content.append(text)
                    if on_delta:
                        on_delta("content", text)
                if delta.get("tool_calls"):
                    calls.add(delta["tool_calls"])
                if choice.get("finish_reason"):
                    result.finish_reason = choice["finish_reason"]
            if stopped:
                break
        result.content = "".join(content)
        result.reasoning = "".join(reasoning)
        result.tool_calls = calls.calls()
        result.elapsed = time.monotonic() - start
        return result
