"""Commit messages written by the ``commit_message`` role, with a plain fallback.

Follows KISS Sorcar's ``commit_message.py``: the model phrases the subject in
terms of the user's intent, and the task and its result are appended to the
body for traceability.
"""

import logging

from localagent.models import make_client
from localagent.models.registry import Registry, RegistryError, effective_thinking
from localagent.models.types import Cancelled, ModelError

logger = logging.getLogger(__name__)

PROMPT = """\
Write a git commit message for the change below.
- First line: a conventional-commit subject such as "fix: handle empty input", at most 72 \
characters, describing what the user wanted.
- Then, only if useful, a blank line and at most 5 short bullet points.
Reply with the message only, without quotes or code fences.

What the user asked for:
{task}

The change (possibly truncated):
{diff}"""

MAX_SUBJECT = 72


def fallback_subject(task: str) -> str:
    first = next((line.strip() for line in task.splitlines() if line.strip()), "agent changes")
    subject = f"la: {first}"
    return subject if len(subject) <= MAX_SUBJECT else subject[: MAX_SUBJECT - 1] + "…"


def _clean(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("text").strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def with_trailer(message: str, task: str, result: str = "") -> str:
    """Append the task (and result) to the message body."""
    body = f"{message.rstrip()}\n\nTask: {task.strip()[:800]}"
    if result.strip():
        body += f"\n\nResult: {result.strip()[:800]}"
    return body


def with_tasks(message: str, tasks: list[str]) -> str:
    """Append the list of chat tasks to a squash-merge message."""
    listed = "\n".join(f"- {t.strip().splitlines()[0][:200]}" for t in tasks if t.strip())
    return f"{message.rstrip()}\n\nTasks:\n{listed}" if listed else message


def write_message(registry: Registry | None, task: str, diff: str, fallback: str | None = None,
                  task_model: str | None = None) -> str:
    """Ask the ``commit_message`` role for a message about *diff*.

    Falls back to *fallback*, or to the task's first line, when there is no
    registry or the model fails. With *task_model*, a role left at ``auto`` uses
    the model that did the task when it is local (``Registry.helper``).
    """
    fallback = fallback or fallback_subject(task)
    if registry is None:
        return fallback
    try:
        model, thinking = registry.helper("commit_message", task_model)
        resolved = registry.resolve("commit_message", model)
        if model is not None:
            resolved.thinking = effective_thinking(resolved.model, thinking)
    except RegistryError:
        return fallback
    client = make_client(resolved)
    try:
        response = client.chat(
            [{"role": "user", "content": PROMPT.format(task=task.strip()[:2000], diff=diff)}],
            thinking=resolved.thinking, max_tokens=400,
        )
        message = _clean(response.content)
    except (ModelError, Cancelled) as e:
        logger.info("commit message generation failed: %s", e)
        return fallback
    finally:
        client.close()
    return message or fallback


def merge_message_for(registry: Registry | None, tasks: list[str], branch: str):
    """The ``message_for`` callback for squash-merging a chat whose tasks were *tasks*."""
    tasks = [t for t in tasks if t.strip()] or [branch]
    summary = "\n".join(f"- {t.strip().splitlines()[0][:200]}" for t in tasks)
    fallback = fallback_subject(tasks[0])
    if len(tasks) > 1:
        fallback = f"{fallback} (+{len(tasks) - 1} more)"

    def message_for(diff: str) -> str:
        return with_tasks(write_message(registry, summary, diff, fallback), tasks)

    return message_for
