"""Tool definitions and argument checking."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class ToolError(Exception):
    """A tool failed in a way the model should see and correct."""


@dataclass
class ToolOutput:
    """A tool result with images (``data:`` URLs) the agent shows the model after the text, if it can see."""

    text: str
    images: list[str] = field(default_factory=list)


@dataclass
class Tool:
    """A function the model can call, with its JSON schema."""

    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., str | ToolOutput]

    def schema(self) -> dict[str, Any]:
        """Return the OpenAI function-tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def signature(self) -> str:
        """Return a short ``Name(a, b=?)`` description for error messages."""
        props = self.parameters.get("properties", {})
        required = set(self.parameters.get("required", []))
        args = ", ".join(n if n in required else f"{n}=?" for n in props)
        return f"{self.name}({args})"

    def call(self, arguments: dict[str, Any]) -> str | ToolOutput:
        """Validate and coerce *arguments*, then run the tool."""
        return self.fn(**self._coerce(arguments))

    def _coerce(self, arguments: dict[str, Any]) -> dict[str, Any]:
        props = self.parameters.get("properties", {})
        missing = [n for n in self.parameters.get("required", []) if n not in arguments]
        if missing:
            raise ToolError(f"missing required argument(s) {', '.join(missing)}; expected {self.signature()}")
        result = {}
        for name, value in arguments.items():
            if name not in props:
                continue  # small models often add stray arguments; ignore them
            result[name] = _coerce_value(name, value, props[name].get("type"))
        return result


def _coerce_value(name: str, value: Any, kind: str | None) -> Any:
    """Accept the common near-misses small models produce, e.g. ``"20"`` for 20."""
    if kind == "integer" and not isinstance(value, int):
        try:
            return int(str(value).strip())
        except ValueError:
            raise ToolError(f"argument {name} must be an integer, got {value!r}") from None
    if kind == "boolean" and not isinstance(value, bool):
        text = str(value).strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no", ""):
            return False
        raise ToolError(f"argument {name} must be true or false, got {value!r}")
    if kind == "string" and not isinstance(value, str):
        return str(value)
    return value
