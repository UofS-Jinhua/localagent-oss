"""Render agent events in the terminal."""

import json
import threading
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

PREVIEW_LINES = 6


def _kilo(n: int) -> str:
    return f"{n / 1000:.1f}K" if n >= 1000 else str(n)


def describe_call(name: str, args: dict[str, Any]) -> str:
    """One-line human description of a tool call."""
    if name == "Bash":
        return f"$ {args.get('command', '')}"
    if name == "Edit":
        old = str(args.get("old_string", "")).count("\n") + 1
        new = str(args.get("new_string", "")).count("\n") + 1
        return f"{args.get('file_path', '')}  (-{old} +{new} lines)"
    if name in ("Read", "Write"):
        extra = f"  from line {args['start_line']}" if args.get("start_line", 1) not in (1, "1") else ""
        return f"{args.get('file_path', '')}{extra}"
    if name in ("Grep", "Glob"):
        where = args.get("path") or "."
        return f"{args.get('pattern', '')!r} in {where}"
    if name == "Plan":
        return ""  # the checklist follows as a plan event
    return json.dumps(args, ensure_ascii=False)[:200]


PLAN_MARKS = {"completed": ("✔", "green"), "in_progress": ("▸", "bold cyan"), "pending": ("○", "")}


class TerminalRenderer:
    """Callable event sink that prints a readable transcript."""

    def __init__(self, console: Console, show_thinking: bool = False):
        self.console = console
        self.show_thinking = show_thinking
        self._lock = threading.Lock()
        self._inline = False
        self._thinking_shown = False

    def _end_inline(self) -> None:
        if self._inline:
            self.console.print()
            self._inline = False

    def __call__(self, event: dict[str, Any]) -> None:
        with self._lock:
            handler = getattr(self, "on_" + event["type"], None)
            if handler is not None:
                handler(event)

    def on_task_start(self, e: dict[str, Any]) -> None:
        thinking = "on" if e["thinking"] else "off"
        body = Text(e["task"])
        for attachment in e.get("attachments") or []:
            body.append(f"\n📎 {attachment['path']} ({attachment['kind']})", style="dim")
        self.console.print(Panel(
            body,
            title=f"[bold]LocalAgent[/] · {e['model']} @ {e['provider']} · thinking {thinking}",
            subtitle=e["work_dir"], border_style="blue",
        ))
        if any(a["kind"] == "image" for a in e.get("attachments") or []) and not e.get("vision"):
            self.console.print(f"[yellow]  {e['model']} cannot view images; it only gets their file paths[/]")

    def on_model_switch(self, e: dict[str, Any]) -> None:
        self._end_inline()
        who = {"user": "you", "fallback": "the fallback"}.get(e["by"], "the agent")
        where = "local" if e["local"] else "cloud"
        reason = f": {e['reason']}" if e.get("reason") else ""
        self.console.print(f"[magenta]⇄ {who} switched from {e['previous']} to [bold]{e['model']}[/bold] "
                           f"({where}){reason}[/]")

    def on_subtask_start(self, e: dict[str, Any]) -> None:
        self._end_inline()
        self.console.print(Text.assemble(("⌥ sub-agent: ", "cyan"), e["headline"], (" …", "dim")))

    def on_subtask_finish(self, e: dict[str, Any]) -> None:
        self._end_inline()
        mark = "✔" if e["success"] else "✘"
        self.console.print(f"[cyan]⌥ sub-agent {mark} {e['steps']} steps · {_kilo(e['total_tokens'])} tokens[/]")
        for line in e["summary"].strip().splitlines()[:PREVIEW_LINES]:
            self.console.print(Text("  " + line, style="dim"))

    def on_review(self, e: dict[str, Any]) -> None:
        self._end_inline()
        if e["clean"]:
            self.console.print(f"[green]⚖ {e['model']} reviewed the change and found no problems[/]")
        else:
            self.console.print(f"[yellow]⚖ {e['model']} found problems; fixing them:[/]")
            self.console.print(Text(e["findings"], style="yellow"))

    def on_worktree(self, e: dict[str, Any]) -> None:
        state = "new branch" if e["created"] else "restored worktree" if e["restored"] else "continuing"
        extra = " · includes your uncommitted changes" if e["carries_user_changes"] and e["created"] else ""
        self.console.print(Text.assemble(("⎇ ", "cyan"), (e["branch"], "bold cyan"),
                                         f" (from {e['base']}) · {state}{extra}", style="cyan"))

    def on_task_committed(self, e: dict[str, Any]) -> None:
        self._end_inline()
        self.console.print(Text.assemble(("✎ committed ", "cyan"), (e["commit"][:8], "yellow"),
                                         f" {e['subject']} on {e['branch']}"))

    def on_step_start(self, e: dict[str, Any]) -> None:
        self._end_inline()
        self._thinking_shown = False

    def on_reasoning_delta(self, e: dict[str, Any]) -> None:
        if self.show_thinking:
            self.console.print(Text(e["text"], style="dim italic"), end="")
            self._inline = True
        elif not self._thinking_shown:
            self.console.print(Text("  thinking…", style="dim italic"), end="")
            self._thinking_shown = self._inline = True

    def on_content_delta(self, e: dict[str, Any]) -> None:
        if self._thinking_shown and not self.show_thinking:
            self._end_inline()
            self._thinking_shown = False
        self.console.print(Text(e["text"]), end="")
        self._inline = True

    def on_tool_call(self, e: dict[str, Any]) -> None:
        self._end_inline()
        self.console.print(Text.assemble(("▶ ", "cyan"), (e["name"], "bold cyan"), " ", describe_call(e["name"], e["args"])))

    def on_plan(self, e: dict[str, Any]) -> None:
        self._end_inline()
        for step in e["steps"]:
            mark, style = PLAN_MARKS.get(step["status"], PLAN_MARKS["pending"])
            self.console.print(Text.assemble("  ", (mark, style or "dim"), " ", (step["step"], style)))
        if e.get("notes"):
            first = e["notes"].splitlines()[0]
            more = "" if first == e["notes"] else " …"
            self.console.print(Text(f"  notes: {first}{more}", style="dim"))

    def on_tool_output(self, e: dict[str, Any]) -> None:
        for line in e["text"].rstrip("\n").split("\n"):
            self.console.print(Text("  │ " + line, style="dim"))

    def on_tool_result(self, e: dict[str, Any]) -> None:
        lines = e["output"].split("\n")
        if not e["is_error"] and e["name"] == "Bash":
            lines = lines[-1:]  # the output already streamed; show the exit status
        elif not e["is_error"] and e["name"] == "RunTests":
            lines = lines[:1]  # the verdict line; the output already streamed
        style = "red" if e["is_error"] else "dim"
        shown = lines[:PREVIEW_LINES]
        for line in shown:
            self.console.print(Text("  " + line, style=style))
        if len(lines) > len(shown):
            self.console.print(Text(f"  … {len(lines) - len(shown)} more lines", style="dim"))

    def on_usage(self, e: dict[str, Any]) -> None:
        self._end_inline()
        parts = [f"step {e['step']}", f"context {_kilo(e['context_tokens'])}/{_kilo(e['context_limit'])}"]
        if e.get("ttft") is not None:
            parts.append(f"first token {e['ttft']:.1f}s")
        if e.get("tokens_per_second"):
            parts.append(f"{e['tokens_per_second']:.0f} tok/s")
        if e.get("cost"):
            parts.append(f"${e['cost']:.4f}")
        if e.get("reasoning_tokens"):
            parts.append(f"thought {e['reasoning_tokens']} tok")
        self.console.print(Text("  " + " · ".join(parts), style="dim"))

    def on_handoff(self, e: dict[str, Any]) -> None:
        self._end_inline()
        if e["source"] == "digest":  # model-written notes were already streamed as content
            self.console.print(Text(e["notes"], style="magenta dim"))

    def on_continuation(self, e: dict[str, Any]) -> None:
        self._end_inline()
        self.console.rule(f"[magenta]session {e['session']} · fresh context, continuing from the notes above[/]")

    def on_steer(self, e: dict[str, Any]) -> None:
        self._end_inline()
        self.console.print(Text.assemble(("✉ you: ", "bold green"), e["text"]))

    def on_retry(self, e: dict[str, Any]) -> None:
        self._end_inline()
        self.console.print(f"[yellow]  retry {e['attempt']} in {e['delay']}s: {e['error']}[/]")

    def on_notice(self, e: dict[str, Any]) -> None:
        self._end_inline()
        self.console.print(f"[yellow]  {e['text']}[/]")

    def on_finish(self, e: dict[str, Any]) -> None:
        self._end_inline()
        ok = e["success"]
        title = "[bold green]✔ done[/]" if ok else f"[bold red]✘ not completed ({e['reason']})[/]"
        sessions = f" · {e['sessions']} sessions" if e.get("sessions", 1) > 1 else ""
        cost = f" · ${e['cost']:.2f}" if e.get("cost") else ""
        subtitle = f"{e['steps']} steps{sessions} · {_kilo(e['total_tokens'])} tokens{cost} · {e['elapsed']:.0f}s"
        self.console.print(Panel(Markdown(e["summary"]), title=title, subtitle=subtitle,
                                 border_style="green" if ok else "red"))
