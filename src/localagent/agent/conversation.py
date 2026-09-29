"""An append-only conversation.

Local inference servers reuse cached prompt computation only while the start
of the prompt is unchanged. Measured on an RTX 2080 Ti, appending a turn to an
11.7K-token conversation took 0.8 s to first token, while changing its first
line took 48 s. This class therefore offers no way to modify earlier messages.
"""

import copy
from typing import Any


class Conversation:
    """OpenAI-format messages that can only grow."""

    def __init__(self, system_prompt: str):
        self._messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    def append(self, message: dict[str, Any]) -> None:
        """Add a message at the end (a deep copy, so callers cannot mutate history)."""
        self._messages.append(copy.deepcopy(message))

    def messages(self) -> list[dict[str, Any]]:
        """Return a copy of the messages to send."""
        return copy.deepcopy(self._messages)

    def __len__(self) -> int:
        return len(self._messages)
