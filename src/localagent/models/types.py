"""Plain data types shared by model clients and the agent loop."""

import json
from dataclasses import dataclass, field
from typing import Any


class ModelError(Exception):
    """A model request failed."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ContextOverflowError(ModelError):
    """The request did not fit in the model's context window."""


class Cancelled(Exception):
    """The user stopped the running request."""


@dataclass
class ToolCall:
    """One function call requested by the model."""

    id: str
    name: str
    arguments: str
    # Provider data that must be sent back with the call, e.g. Gemini's
    # {"google": {"thought_signature": ...}} in the OpenAI-compatible API.
    extra_content: dict[str, Any] | None = None

    def parsed_arguments(self) -> dict[str, Any]:
        """Decode the JSON arguments; raise ``ValueError`` when they are not a JSON object."""
        if not self.arguments.strip():
            return {}
        value = json.loads(self.arguments)
        if not isinstance(value, dict):
            raise ValueError("arguments must be a JSON object")
        return value

    def to_message(self) -> dict[str, Any]:
        """Return the OpenAI chat-completions representation."""
        message: dict[str, Any] = {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }
        if self.extra_content:
            message["extra_content"] = self.extra_content
        return message


@dataclass
class Usage:
    """Token accounting for one request."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ChatResponse:
    """The fully streamed result of one chat request."""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    ttft: float | None = None
    elapsed: float = 0.0

    @property
    def generation_seconds(self) -> float:
        """Seconds spent generating after the first token arrived."""
        return self.elapsed - (self.ttft or 0.0)

    @property
    def tokens_per_second(self) -> float | None:
        seconds = self.generation_seconds
        if seconds <= 0 or not self.usage.completion_tokens:
            return None
        return self.usage.completion_tokens / seconds
