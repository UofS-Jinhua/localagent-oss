"""Running a sub-agent for a task, and keeping its transcript.

The caller only ever sees the sub-agent's report. Everything it did is stored as
a task of its own (``parent_id`` points at the caller's task), so the work can be
traced later without any of it sitting in the caller's context. In practice
``git diff`` answers "what changed" and this transcript answers "why".
"""

import threading
from collections.abc import Callable
from typing import Any

from localagent.agent.loop import Agent, EventSink
from localagent.agent.subtask import MAX_SUBTASK_STEPS, SUBTASK_PROMPT
from localagent.models.registry import Registry
from localagent.store import Store
from localagent.tools.shell import Approver
from localagent.tools.workspace import Workspace

HEADLINE_CHARS = 72


def _headline(instructions: str) -> str:
    """A short name for the sub-task, for task lists and the UI card.

    Instructions start with an order ("Read these 6 files and report ..."), not
    a title, so this is a clipped first line: the card shows the instructions in
    full when you open it.
    """
    first = " ".join(instructions.strip().splitlines()[0].split()) if instructions.strip() else "sub-task"
    return first if len(first) <= HEADLINE_CHARS else first[: HEADLINE_CHARS - 1].rstrip(" ,.;:") + "…"


def _own_workspace(parent: Workspace) -> Workspace:
    """The same directory and the same rules, but its own read-before-edit record.

    Every session starts by forgetting what it read, so sharing the object would
    make a sub-task wipe the caller's record and force it to Read again files it
    still has in its conversation. Anything the sub-agent does change is still
    caught: ``staleness`` compares the content hash, not just the fact of a read.
    """
    return Workspace(parent.root, venv_roots=parent.venv_roots, protected_roots=parent.protected_roots,
                     hidden_env=parent.hidden_env, background=parent.background)


def make_delegate(
    registry: Registry,
    workspace: Workspace,
    store: Store,
    chat_id: str,
    parent_task_id: Callable[[], str | None],
    on_event: EventSink,
    *,
    role: str = "subagent",
    approver: Approver | None = None,
    stop_event: threading.Event | None = None,
    git_note: str | None = None,
    task_model: Callable[[], str | None] = lambda: None,
) -> Callable[[str, bool, int], str]:
    """Build the callable behind the Task tool.

    Args:
        task_model: The caller's model at the moment it delegates (it can switch mid-task);
            see ``Registry.helper`` for when the sub-agent follows it.
    """

    def delegate(instructions: str, read_only: bool, budget: int) -> str:
        headline = _headline(instructions)
        child: dict[str, str] = {}

        def record(event: dict[str, Any]) -> None:
            store.record_event(child["id"], event)  # its steps stay out of the caller's transcript

        model, thinking = registry.helper(role, task_model())
        agent = Agent(
            registry, _own_workspace(workspace), role=role, model=model, thinking=thinking, on_event=record,
            max_steps=MAX_SUBTASK_STEPS, max_sessions=1, git_note=git_note, approver=approver,
            stop_event=stop_event, read_only=read_only, context_limit=budget,
        )
        child["id"] = child_id = store.add_task(
            chat_id, instructions, agent.resolved.model.name, str(workspace.root),
            parent_id=parent_task_id() or "",
        )
        on_event({"type": "subtask_start", "task_id": child_id, "instructions": instructions,
                  "read_only": read_only, "budget": budget, "headline": headline,
                  "model": agent.resolved.model.name})
        try:
            result = agent.run(SUBTASK_PROMPT.format(instructions=instructions))
        except Exception as e:  # a broken sub-task must not end the caller's task
            store.finish_task(child_id, success=False, result=str(e), reason="error")
            on_event({"type": "subtask_finish", "task_id": child_id, "headline": headline, "success": False,
                      "steps": 0, "total_tokens": 0, "summary": str(e)})
            return f"The sub-agent failed: {e}. Do this yourself."
        finally:
            agent.close()
        store.finish_task(child_id, success=result.success, result=result.summary, reason=result.reason,
                          steps=result.steps, sessions=result.sessions, total_tokens=result.total_tokens,
                          elapsed=result.elapsed, cost=result.cost)
        on_event({"type": "subtask_finish", "task_id": child_id, "headline": headline,
                  "success": result.success, "steps": result.steps, "total_tokens": result.total_tokens,
                  "summary": result.summary})
        verdict = "reports" if result.success else f"did not finish ({result.reason}) and reports"
        return (f"The sub-agent {verdict}:\n\n{result.summary}\n\n"
                f"[it used {result.steps} step(s) and {result.total_tokens:,} tokens of its own context]")

    return delegate
