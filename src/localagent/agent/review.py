"""A second pass over the work: one model writes, another reads the diff.

KISS Sorcar gets this by asking for it in the prompt ("use model A to code and
model B to review"). Here it is a switch on the run, because a small local model
does not reliably orchestrate that itself, and because the reviewer should be
unable to edit: it reads, runs tests, and reports.

The verdict is mechanical rather than parsed from prose: the reviewer ends with
``finish(success=True)`` when the change is good and ``finish(success=False)``
with a numbered list when it is not.
"""

import threading

from localagent.agent.loop import Agent, EventSink
from localagent.models.registry import Registry
from localagent.tools.shell import Approver

MAX_REVIEW_STEPS = 30
REVIEW_TASK = """\
You are reviewing another model's change before it is committed. Do not edit anything.

# The task it was given
{task}

# What it changed (diff)
```diff
{diff}
```

# How to review
- Read the files it touched, and the callers or tests around them.
- Check every requirement of the task against what the diff actually does.
- Look for bugs it introduced, code it forgot to update, and claims it did not verify.
- **Run the tests with RunTests and read the output yourself.** A claim in the summary is not \
evidence. If they fail, that is the first finding. Only skip this if there is no test command at all.
- **Judge whether the tests prove anything.** A test that replaces the thing under test with a \
stub, asserts on the stub, or only checks that a function was called proves nothing; so does a test \
that would still pass with the change reverted. Say so when you see it, and say what the test should \
exercise instead (the real file, the real command, the real request). Faking a genuinely external \
dependency — a paid API, a remote host, the clock — is fine; faking the code under test is not.
- If the change adds behaviour and no test covers it, that is a finding.
- A failing test is only this change's fault if the diff explains it. Tests that fail in code the diff does not touch were most likely already failing; say so instead of blaming the change. You cannot run anything but the project's test command, so reason from the diff.

When you are done, call finish:
- No real problem: finish(success=true, summary="a sentence on what you checked").
- Otherwise: finish(success=false, summary=<numbered list; each item names the file and what is \
wrong, concretely enough to fix>). Report only real problems, not style preferences.\
"""

FIX_TASK = """\
A reviewer read your changes and found problems. Fix them.

# The original task
{task}

# What the reviewer found
{findings}

Fix each item, verify as usual, then finish with what you changed."""


def review_changes(
    registry: Registry,
    workspace,
    task: str,
    diff: str,
    *,
    role: str = "reviewer",
    on_event: EventSink | None = None,
    stop_event: threading.Event | None = None,
    approver: Approver | None = None,
    task_model: str | None = None,
) -> tuple[bool, str, str]:
    """Read the diff with the reviewer's model. Returns ``(clean, findings, model name)``.

    A ``reviewer`` role left at ``auto`` reviews on the task's own model when that is local
    (``Registry.helper``); naming a model -- a cloud one, say -- gives a cross-vendor review.
    """
    model, thinking = registry.helper(role, task_model)
    agent = Agent(registry, workspace, role=role, model=model, thinking=thinking, on_event=on_event,
                  max_steps=MAX_REVIEW_STEPS, max_sessions=1, stop_event=stop_event, read_only=True,
                  approver=approver)
    try:
        result = agent.run(REVIEW_TASK.format(task=task, diff=diff))
        return result.success, result.summary.strip(), agent.resolved.model.name
    finally:
        agent.close()
