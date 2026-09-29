"""Deliver events from any thread to every connected WebSocket client."""

import asyncio
import threading
from typing import Any

DELTA_TYPES = frozenset({"reasoning_delta", "content_delta", "tool_output"})
MAX_BACKLOG_EVENTS = 600  # a client that connects late gets this much of the running task
MAX_TEXT_CHARS = 20_000  # ...and this much of any single streamed block


class Hub:
    """Fan-out of JSON events, plus a compact backlog of the running task for clients that connect late."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()
        self._backlog: list[dict[str, Any]] = []

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> tuple[asyncio.Queue[dict[str, Any]], list[dict[str, Any]]]:
        """Register a client; returns its queue and the running task's events so far."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        with self._lock:
            self._queues.add(queue)
            return queue, list(self._backlog)

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._queues.discard(queue)

    def reset_backlog(self) -> None:
        with self._lock:
            self._backlog.clear()

    def _remember(self, event: dict[str, Any]) -> None:
        """Keep enough of the running task to bring a late client up to date, but bounded.

        A long task streams megabytes; without the caps the backlog would hold all
        of it in memory and be sent in full to every client that connects.
        """
        last = self._backlog[-1] if self._backlog else None
        if (event["type"] in DELTA_TYPES and last is not None and last["type"] == event["type"]
                and last.get("id") == event.get("id")):
            text = last["text"] + event["text"]  # coalesce streamed text
            if len(text) > MAX_TEXT_CHARS:
                text = text[-MAX_TEXT_CHARS:]
            self._backlog[-1] = {**last, "text": text}
            return
        self._backlog.append(event)
        if len(self._backlog) > MAX_BACKLOG_EVENTS:
            # Drop the oldest, but keep the task's opening events: they say what is running.
            del self._backlog[2:2 + len(self._backlog) - MAX_BACKLOG_EVENTS]

    def publish(self, event: dict[str, Any], remember: bool = True) -> None:
        """Send *event* to every client; safe to call from any thread."""
        with self._lock:
            if remember:
                self._remember(event)
            queues = list(self._queues)
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        for queue in queues:
            loop.call_soon_threadsafe(queue.put_nowait, event)
