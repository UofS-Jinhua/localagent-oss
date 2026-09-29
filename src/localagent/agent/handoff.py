"""A deterministic session record, used when the model cannot write its own handoff notes.

KISS Sorcar handles a crashed or overflowing session by dumping the trajectory
to a file and running a summarizer agent over it. With a local model that
costs a cold prefill plus several tool steps, so LocalAgent first asks for
notes inside the (already cached) conversation and only falls back to this
free, mechanical digest when that is impossible.
"""

import json
import re
from typing import Any

MAX_DIGEST_CHARS = 6000
_STATUS_LINE = re.compile(r"\n*\[step \d+/\d+ · context [^\]]*\]\s*$")
_EXIT_CODE = re.compile(r"\[exit code -?\d+\]|\[tests \w+[^\]]*\]|\[timed out[^\]]*\]")


def _short_args(arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        return arguments[:160]
    if not isinstance(args, dict):
        return str(args)[:160]
    parts = []
    for key, value in args.items():
        text = str(value).replace("\n", "\\n")
        parts.append(f"{key}={text[:60] + '…' if len(text) > 60 else text}")
    return ", ".join(parts)[:200]


def _outcome(content: str) -> str:
    body = _STATUS_LINE.sub("", content or "").strip()
    if not body:
        return "(no output)"
    first = body.splitlines()[0][:160]
    codes = _EXIT_CODE.findall(body)
    if codes and codes[-1] not in first:
        return f"{first} … {codes[-1]}"
    return first


def session_digest(messages: list[dict[str, Any]]) -> str:
    """Summarize a session's tool calls and outcomes, newest kept when over budget."""
    results = {m.get("tool_call_id"): m.get("content", "") for m in messages if m.get("role") == "tool"}
    lines = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            outcome = _outcome(results.get(call.get("id"), ""))
            lines.append(f"- {function.get('name')}({_short_args(function.get('arguments', ''))}) -> {outcome}")
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) > MAX_DIGEST_CHARS:
            kept.append(f"- ... {len(lines) - len(kept)} earlier steps omitted")
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    last_text = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "assistant" and (m.get("content") or "").strip()),
        "",
    )
    text = "The previous session ran out of context before writing notes. Its tool calls were:\n" + "\n".join(kept)
    if last_text:
        text += f"\nIts last message: {last_text.strip()[:500]}"
    return text
