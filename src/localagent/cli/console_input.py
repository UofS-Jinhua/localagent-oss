"""One reader for the terminal's input while a task runs.

A line typed while the agent works is sent to it as a steering message,
unless a yes/no question (command approval) is waiting, in which case the
line answers the question. A single reader avoids two threads competing for
stdin.
"""

import queue
import sys
import threading
from collections.abc import Callable

from rich.console import Console


class ConsoleInput:
    """Routes stdin lines to a pending question or to ``on_message``."""

    def __init__(self, console: Console, on_message: Callable[[str], None]):
        self.console = console
        self.on_message = on_message
        self._lock = threading.Lock()
        self._answers: queue.Queue[str] | None = None

    def start(self) -> None:
        threading.Thread(target=self._pump, name="console-input", daemon=True).start()

    def _pump(self) -> None:
        for raw in sys.stdin:
            line = raw.rstrip("\r\n")
            with self._lock:
                answers = self._answers
            if answers is not None:
                answers.put(line)
            elif line.strip():
                self.on_message(line.strip())

    def _next_line(self, prompt: str) -> str:
        """Print *prompt* and take the next typed line as its answer, not as steering."""
        answers: queue.Queue[str] = queue.Queue()
        with self._lock:
            self._answers = answers
        try:
            self.console.print(prompt, end="")
            return answers.get()
        finally:
            with self._lock:
                self._answers = None

    def confirm(self, command: str, reason: str) -> bool:
        """Ask a yes/no question and wait for the next typed line."""
        return self._next_line(f"[yellow]Allow[/] [bold]{command}[/]? It {reason}. [y/N] ").strip().lower() in ("y", "yes")

    def ask(self, question: str, options: list[str]) -> str:
        """Put the agent's question to the user and wait for their answer (AskUser)."""
        self.console.print()
        self.console.print(f"[bold cyan]The agent asks:[/] {question}")
        for index, option in enumerate(options, 1):
            self.console.print(f"  {index}. {option}")
        answer = self._next_line("[cyan]your answer:[/] ").strip()
        if options and answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        return answer
