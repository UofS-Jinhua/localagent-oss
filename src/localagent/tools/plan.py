"""The Plan tool: the agent's plan for a larger piece of work, kept where the next step can see it.

Measured on the 27B models (docs/architecture.md, invariant 42): given a new project, the first reply
designed the whole thing in thinking -- 50,000 to 100,000 characters of algorithms and even whole
source files -- and then called ``ls``. Thinking is not sent back to the model, so the next step could
not see any of it and designed it all again, step after step. A plan passed to a tool is part of the
conversation instead: later steps read it, a fresh session gets it with its first message, and a task
that stopped hands it to the next task of the chat.

The tool itself only checks and records; the agent keeps the latest plan (see ``Agent._record_plan``).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from localagent.tools.base import Tool, ToolError

STATUSES = ("pending", "in_progress", "completed")
_SAME = {"done": "completed", "complete": "completed", "finished": "completed", "ok": "completed",
         "in-progress": "in_progress", "in progress": "in_progress", "active": "in_progress",
         "doing": "in_progress", "current": "in_progress", "todo": "pending", "not_started": "pending"}
_TEXT_KEYS = ("step", "content", "title", "description", "task", "text")  # TodoWrite and others say "content"
MARKS = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}

DESCRIPTION = (
    "Record your plan for a new project or for work spanning several files, and keep it current. "
    "List the steps in order -- for each, what to build or change, in which files, and how to check "
    "it -- and put the decisions later steps rely on (file layout, names, algorithms, parameters) in "
    "notes. A plan holds parts and decisions, not code: code goes into Write. Your thinking is "
    "discarded after each step, but the plan stays in the conversation and is carried over to later "
    "sessions, so work from it instead of working the design out again. Call Plan again with the "
    "whole list whenever a step is completed or the plan changes; the status line under each tool "
    "result shows how many steps you have marked completed. Skip it for small single-file work."
)


@dataclass
class Plan:
    """Steps as ``(text, status)`` pairs, in order, and the decisions that go with them."""

    steps: list[tuple[str, str]] = field(default_factory=list)
    notes: str = ""

    def completed(self) -> int:
        return sum(1 for _, status in self.steps if status == "completed")

    def next_step(self) -> str | None:
        """The step in progress, else the first one not yet completed."""
        for wanted in ("in_progress", "pending"):
            for text, status in self.steps:
                if status == wanted:
                    return text
        return None

    def render(self) -> str:
        """The plan as a checklist, for the model's messages: ``[x]`` completed, ``[>]`` in progress."""
        lines = [f"- {MARKS[status]} {text}" for text, status in self.steps]
        if self.notes:
            lines.append(f"\nNotes:\n{self.notes}")
        return "\n".join(lines)

    def to_event(self) -> dict[str, Any]:
        return {"steps": [{"step": text, "status": status} for text, status in self.steps], "notes": self.notes}


def _status(value: Any) -> str:
    text = str(value or "pending").strip().lower()
    text = _SAME.get(text, text.replace("-", "_").replace(" ", "_"))
    return text if text in STATUSES else "pending"


def _step(item: Any) -> tuple[str, str] | None:
    if isinstance(item, dict):
        text = next((str(item[k]) for k in _TEXT_KEYS if isinstance(item.get(k), str) and item[k].strip()), "")
        return (text.strip(), _status(item.get("status"))) if text.strip() else None
    text = str(item).strip()
    for mark, status in (("[x]", "completed"), ("[X]", "completed"), ("[>]", "in_progress"), ("[ ]", "pending")):
        if text.lstrip("-* ").startswith(mark):  # a checklist line, as render() writes it
            return text.lstrip("-* ")[len(mark):].strip(), status
    return (text.lstrip("-* ").strip(), "pending") if text.strip("-* ") else None


def parse_plan(steps: Any, notes: Any = "") -> Plan:
    """Build a plan from what a model passed, accepting the usual near-misses.

    *steps* may be a list of ``{"step", "status"}`` objects (or TodoWrite's ``content``), a list of
    strings, a JSON string of either, or a checklist with one step per line.
    """
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except ValueError:
            steps = steps.splitlines()
    if isinstance(steps, dict):
        steps = [steps]
    if not isinstance(steps, list):
        raise ToolError("steps must be a list of {step, status} objects")
    parsed = [s for s in (_step(item) for item in steps) if s is not None]
    if not parsed:
        raise ToolError("the plan has no steps; list them in order, each with a status")
    return Plan(parsed, str(notes or "").strip())


def make_plan_tool(on_update: Callable[[Plan], None]) -> Tool:
    """Return the Plan tool; *on_update* receives every plan the model records."""

    def plan(steps: Any, notes: str = "") -> str:
        recorded = parse_plan(steps, notes)
        on_update(recorded)
        done, total = recorded.completed(), len(recorded.steps)
        upcoming = recorded.next_step()
        if upcoming is None:
            return f"Plan recorded: all {total} steps completed."
        return f"Plan recorded: {done} of {total} steps completed. Next: {upcoming}"

    return Tool(
        "Plan",
        DESCRIPTION,
        {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "Every step of the plan, in order, each time (not just the changed ones).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step": {"type": "string", "description": "What to do, in which files, and how to check it."},
                            "status": {"type": "string", "enum": list(STATUSES)},
                        },
                        "required": ["step", "status"],
                    },
                },
                "notes": {
                    "type": "string",
                    "description": "Decisions the later steps depend on: layout, names, algorithms, parameters. No code.",
                },
            },
            "required": ["steps"],
        },
        plan,
    )
