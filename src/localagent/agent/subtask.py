"""Hand a piece of work to a sub-agent with its own context.

This is a context filter, not parallelism. On a single-GPU server two agents
running at once split one KV pool and one stream of tokens, so nothing gets
faster; what helps is that a wide search costing 10K tokens of tool output
happens in *another* conversation and comes back as a few hundred tokens of
summary. The main agent's context stays small, so its later steps stay fast and
its handoff comes later.

Two rules keep that from backfiring:

* the sub-agent runs while the caller waits (one request in flight at a time);
* its window is capped, and the cap is what is actually left over, so the two
  conversations together stay inside the server's shared KV budget. Evicting the
  caller's cached prefix would cost far more than the sub-task ever saves.
"""

from collections.abc import Callable

from localagent.tools.base import Tool, ToolError

#: ``(instructions, read_only, budget) -> report``; the budget is the context the sub-agent may use.
Delegate = Callable[[str, bool, int], str]

MAX_SUBTASK_CONTEXT = 32_768
MIN_SUBTASK_CONTEXT = 8_192
MAX_SUBTASK_STEPS = 25

SUBTASK_PROMPT = """\
You are a sub-agent working for another agent, in the same project. You cannot ask it anything and \
you cannot see its conversation, so work with what is written here.

# What to do
{instructions}

# How to report
Call finish(success, summary). Your summary is the only thing the caller receives: put the answer \
itself in it (the facts, file paths and line numbers it needs), not a description of what you did.\
"""

NO_ROOM = (
    "Error: not enough context left to run a sub-agent ({budget:,} tokens free, {minimum:,} needed). "
    "Do this yourself, or finish the current line of work and let a fresh session continue."
)


def make_task_tool(delegate: Delegate, budget: Callable[[], int],
                   cap: Callable[[], int] = lambda: MAX_SUBTASK_CONTEXT, parallel: int = 1) -> Tool:
    """The Task tool: run *instructions* in a sub-agent and return only its report.

    Args:
        budget: What is left of the caller's window for a sub-agent right now.
        cap: The most a sub-agent may get even when more is left. 32K suits a server whose KV pool
            is one conversation's worth; a model on a server with a much larger pool sets its own
            (``ModelSpec.max_subtask_context``).
        parallel: How many read-only Task calls of one turn the caller runs at once; only said in the
            description, the caller does the scheduling (``Agent._run_subtasks_in_parallel``).
    """

    def task(instructions: str, read_only: bool = True) -> str:
        text = instructions.strip()
        if not text:
            raise ToolError("instructions must say what the sub-agent should do and what to report back")
        available = budget()
        if available < MIN_SUBTASK_CONTEXT:
            return NO_ROOM.format(budget=max(0, available), minimum=MIN_SUBTASK_CONTEXT)
        return delegate(text, read_only, min(available, max(MIN_SUBTASK_CONTEXT, cap())))

    return Tool(
        "Task",
        "Give a self-contained piece of work to a sub-agent that has its own context, and get back "
        "only its report. Use it for work whose tool output would otherwise fill your context: "
        "searching the project, reading long files, checking how something is used. It cannot see "
        "this conversation, so write down everything it needs and exactly what to report back. It "
        "runs while you wait." + (
            f" Read-only Task calls made in the same turn run at the same time (up to {parallel}), each with a "
            "share of the room: split broad work into separate, self-contained parts." if parallel > 1 else ""),
        {
            "type": "object",
            "properties": {
                "instructions": {
                    "type": "string",
                    "description": "The whole job: what to do, where to look, and what to report back.",
                },
                "read_only": {
                    "type": "boolean",
                    "description": "Keep it to reading and searching (default true). False lets it change files.",
                },
            },
            "required": ["instructions"],
        },
        task,
    )
