"""The agent: KISS-style layers 1 and 2.

Layer 1 (``_run_session``) is a ReAct loop over one append-only conversation
with native function calling and a ``finish`` tool. Layer 2 (``run``) chains
sessions: when a conversation fills its context budget, the model writes
handoff notes and a fresh session continues from them instead of compacting
the old conversation in place.
"""

import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from localagent.agent.conversation import Conversation
from localagent.agent.handoff import session_digest
from localagent.agent.subtask import MAX_SUBTASK_CONTEXT, MIN_SUBTASK_CONTEXT, Delegate, make_task_tool
from localagent.agent.prompts import (
    FINISH_TOOL,
    HANDOFF_REQUEST,
    POINTER_BLOCKS_FINISH,
    STEERING_BLOCKS_FINISH,
    STEERING_TEMPLATE,
    build_session_message,
    build_system_prompt,
    model_line,
    relevant_notes,
    set_model_tool,
)
from localagent.attachments import Placed, describe, user_content
from localagent.models import make_client
from localagent.models.openai_compat import OpenAICompatClient
from localagent.models.registry import Registry, Resolved, effective_thinking
from localagent.models.thinking import ThinkingGuard
from localagent.models.types import Cancelled, ChatResponse, ContextOverflowError, ModelError, ToolCall
from localagent.skills import Skill, catalogue, load_skills, make_skill_tool
from localagent.tools import Asker, Tool, ToolError, WebSettings, Workspace, make_tools
from localagent.tools.base import ToolOutput
from localagent.tools.plan import Plan, make_plan_tool
from localagent.tools.shell import Approver

logger = logging.getLogger(__name__)

EventSink = Callable[[dict[str, Any]], None]

MAX_RETRIES = 3
MAX_IDLE_TURNS = 2
HANDOFF_MAX_TOKENS = 3000
MIN_HANDOFF_CHARS = 80
MIN_SESSION_ROOM = 0.25  # every session may grow by at least this share of the window before handing off
HARD_CONTEXT_LIMIT = 0.9  # ...but never beyond this share, leaving room to write the notes
SWITCH_FIT = 0.8  # set_model refuses a model whose window the conversation would fill beyond this share
NUDGE = (
    "Your reply had no tool call. Every response must call at least one tool. "
    "If the task is complete, call finish with your final summary now."
)
CUT_OFF_NUDGE = (
    "Your reply was cut off by the output-token limit before you called a tool. "
    "Think more briefly and call a tool now."
)
LOOP_NUDGE = (
    "Your thinking went in circles -- you wrote out a passage you had already been through -- so it "
    "was stopped. Do not analyse further: decide with what you already know and call a tool now."
)
LONG_NUDGE = (
    "Your thinking ran past its limit before you called a tool, so it was stopped. "
    "Do not analyse further: decide with what you already know and call a tool now."
)
FILE_HINT = " If you need to remember a plan, write it to a file instead of rehearsing it."
# Thinking that was stopped is lost; what it decided survives only if it goes into a tool call.
PLAN_HINT = " If you have worked out a plan, record it with Plan now instead of working it out again."
HANDOFF_NOTICE = "context budget reached; writing handoff notes for a fresh session"
STEP_LIMIT_NOTICE = "the step limit was reached; writing notes so this can be continued"
MIN_RETRY_TOKENS = 2048  # a halved budget never goes below this
MIN_RETRY_THINKING = 500  # ...and a halved thinking bound never goes below this many tokens


@dataclass
class AgentResult:
    """How a task ended."""

    success: bool
    summary: str
    reason: str
    steps: int
    sessions: int
    context_tokens: int
    total_tokens: int
    elapsed: float
    cost: float = 0.0  # USD, from the models' prices; 0 when they are free or unpriced
    plan: str = ""  # the last plan the model recorded, as a checklist ("" when it made none)


@dataclass
class _SessionEnd:
    """How one session ended: ``finished``, ``handoff``, ``stopped``, ``error`` or ``max_steps``.

    For a handoff, ``summary`` holds the notes and ``source`` says who wrote them
    (``"model"`` or ``"digest"``).
    """

    kind: str
    summary: str
    success: bool = False
    reason: str = ""
    source: str = ""


def _reads_only(call: ToolCall) -> bool:
    """Whether a Task call asks for a read-only sub-agent (the default); unreadable arguments do not."""
    try:
        value = call.parsed_arguments().get("read_only", True)
    except ValueError:
        return False
    return value is True or str(value).strip().lower() in ("true", "1", "yes")


def handoff_threshold(context_length: int, continue_at: float, session_start: int) -> int:
    """Context size at which a session hands off.

    Normally ``continue_at`` of the window. A session that already starts near
    that mark (long chat history, long notes, a small window) still gets
    ``MIN_SESSION_ROOM`` of the window to work with, up to ``HARD_CONTEXT_LIMIT``;
    otherwise it would hand off again after a step or two.
    """
    soft = continue_at * context_length
    room = session_start + MIN_SESSION_ROOM * context_length
    return int(min(max(soft, room), HARD_CONTEXT_LIMIT * context_length))


def _attachment_meta(placed: list[Placed] | None) -> list[dict[str, Any]]:
    return [{**p.attachment.to_dict(), "path": p.path} for p in placed or []]


def _wrote_visible_text(conversation: Conversation) -> bool:
    """Whether the model wrote any reply text in this session, which the page shows with its step."""
    return any(m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip()
               for m in conversation.messages())


def _assistant_message(response: ChatResponse) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": response.content}
    if response.tool_calls:
        message["tool_calls"] = [c.to_message() for c in response.tool_calls]
    return message


# Phrases that send the reader to a reply other than the summary. Only these: "如下" or "as follows" are
# followed by the content itself, so they are not pointers.
POINTERS = ("详见", "见上文", "见下文", "见正文", "回复正文", "正文中", "见上方", "见下方", "如上所述", "上面的回复",
            "see above", "see below", "see the reply", "see my reply", "in my reply", "reply body",
            "details above", "detailed above", "answer above", "as described above")
POINTER_SUMMARY_CHARS = 800  # a summary longer than this carries its content, whatever it also says


def _points_elsewhere(summary: str) -> str | None:
    """The phrase with which a short summary refers the user to a reply that is not the summary, if any."""
    if len(summary) > POINTER_SUMMARY_CHARS:
        return None
    lowered = summary.lower()
    return next((phrase for phrase in POINTERS if phrase in lowered), None)


def _finish_arguments(call: ToolCall) -> tuple[bool, str]:
    args = call.parsed_arguments()
    success = args.get("success", True)
    if isinstance(success, str):
        success = success.strip().lower() not in ("false", "0", "no")
    summary = args.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("finish needs a non-empty summary")
    return bool(success), summary


class Agent:
    """Runs one task in one working directory with one model."""

    def __init__(
        self,
        registry: Registry,
        work_dir: str | Path | Workspace,
        *,
        role: str = "main",
        model: str | None = None,
        thinking: bool | None = None,
        max_steps: int = 100,
        max_sessions: int = 5,
        git_note: str | None = None,
        on_event: EventSink | None = None,
        approver: Approver | None = None,
        stop_event: threading.Event | None = None,
        allow_cloud: bool = False,
        asker: Asker | None = None,
        read_only: bool = False,
        delegate: Delegate | None = None,
        context_limit: int = 0,
        skills: list[Skill] | None = None,
        web: WebSettings | None = None,
    ):
        self.registry = registry
        self.resolved = registry.resolve(role, model)
        role_spec = registry.roles.get(role) or registry.roles.get("main")
        # The run's thinking preference, applied again to every model the task switches to.
        self._thinking_preference = thinking if thinking is not None else (
            role_spec.thinking if role_spec is not None and model is None else None)
        self.resolved.thinking = effective_thinking(self.resolved.model, self._thinking_preference)
        self._clients: dict[str, OpenAICompatClient] = {}
        self._start_model = self.resolved.model.name  # where a failed switch falls back to
        self.client = self._client_for(self.resolved)
        self.workspace = work_dir if isinstance(work_dir, Workspace) else Workspace(work_dir)
        self.workspace.hidden_env |= registry.secret_env_names()  # commands must not see the model keys
        self.git_note = git_note
        self.max_steps = max_steps
        self.max_sessions = max_sessions
        self.on_event = on_event
        self.stop_event = stop_event or threading.Event()
        self._steering: queue.SimpleQueue[tuple[str, list[Placed]]] = queue.SimpleQueue()
        self._model_requests: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._current_call = ""
        self._steps = self._context = self._total = 0
        self._cost = 0.0
        self._session_start = 0
        self._threshold: int | None = None
        # A sub-agent gets a smaller window than its model's, so both conversations fit the
        # server's shared KV pool; 0 means "the whole window".
        self.context_limit = context_limit
        self._parallel_share = 1  # sub-agents running at once share the room one would get alone
        self._pointer_refused = False  # a finish that only points to a reply is refused once per task
        self._plan: Plan | None = None  # the latest plan the model recorded with the Plan tool
        # The user's skills: the catalogue goes in the prompt, the bodies arrive through the tool.
        self.skills = load_skills(self.workspace.root) if skills is None else skills
        web = WebSettings.load() if web is None else web
        if web.key_env:
            self.workspace.hidden_env.add(web.key_env)  # a search key is no business of the commands either
        tools = make_tools(self.workspace, approver, self._on_tool_output, self.stop_event, asker, read_only, web)
        if not read_only:  # an agent that only reads has nothing to plan
            tools.append(make_plan_tool(self._record_plan))
        if self.skills:
            tools.append(make_skill_tool(self.skills))
        switch = None if read_only else self._set_model_tool(allow_cloud)
        if switch is not None:
            tools.append(switch)
        if delegate is not None:
            tools.append(make_task_tool(delegate, self._delegate_budget,
                                        lambda: self.resolved.model.max_subtask_context or MAX_SUBTASK_CONTEXT,
                                        parallel=self.resolved.model.max_parallel_subtasks))
        self.tools: dict[str, Tool] = {t.name: t for t in tools}
        self.schemas = [t.schema() for t in tools] + [FINISH_TOOL]

    # ----- public API -------------------------------------------------------

    def steer(self, message: str, attachments: list[Placed] | None = None) -> None:
        """Queue a user message (with attachments) for the running task; it is added before the next model call."""
        if message.strip() or attachments:
            self._steering.put((message.strip(), list(attachments or [])))

    def request_model(self, name: str) -> None:
        """Ask the running task to continue on model *name* from its next step (a user's choice)."""
        self._model_requests.put(name)

    @property
    def plan(self) -> str:
        """The latest plan the model recorded, as a checklist, or ``""``."""
        return self._plan.render() if self._plan is not None else ""

    def close(self) -> None:
        """Close the HTTP clients of every model the task used."""
        for client in self._clients.values():
            client.close()

    def run(self, task: str, chat_context: str = "", attachments: list[Placed] | None = None) -> AgentResult:
        """Work on *task* across as many sessions as needed, until ``finish`` or a limit.

        Args:
            task: The user's task.
            chat_context: Earlier tasks of the same chat, prepended to each session's first message.
            attachments: Files the user attached, already placed in the workspace. Every
                session's first message lists them and carries the images.
        """
        start = time.monotonic()
        self._steps = self._context = self._total = 0
        self._cost = 0.0
        self._pointer_refused = False
        self._plan = None
        model = self.resolved.model
        system_prompt = build_system_prompt(self.workspace.root, self.git_note, self.workspace.venv(),
                                            can_ask="AskUser" in self.tools, can_delegate="Task" in self.tools,
                                            can_browse="WebFetch" in self.tools, can_plan="Plan" in self.tools,
                                            skills=catalogue(self.skills))
        self._emit(
            "task_start", task=task, model=model.name, provider=self.resolved.provider.name,
            work_dir=str(self.workspace.root), thinking=self.resolved.thinking, vision=model.vision,
            attachments=_attachment_meta(attachments),
        )
        notes: list[tuple[int, str, str]] = []
        session = 0
        end = _SessionEnd("error", "no session ran")
        for session in range(1, self.max_sessions + 1):
            if session > 1:
                self._emit("continuation", session=session)
            self.workspace.forget_reads()
            conversation = Conversation(system_prompt)
            opening = build_session_message(
                task, chat_context, relevant_notes(notes), continuation=session - 1,
                attachments=describe(attachments or [], vision=self.resolved.model.vision), plan=self.plan,
            )
            conversation.append({"role": "user", "content": user_content(opening, attachments or [])})
            end = self._run_session(conversation)
            if end.kind != "handoff":
                break
            notes.append((session, end.summary, end.source))
        else:
            latest = f"\n\nLatest notes:\n\n{notes[-1][1]}" if notes else ""
            end = _SessionEnd(
                "error", f"Stopped after {self.max_sessions} session(s) without finishing.{latest}",
                reason="sessions_exhausted",
            )
        result = AgentResult(
            success=end.success, summary=end.summary, reason=end.reason or end.kind, steps=self._steps,
            sessions=session, context_tokens=self._context, total_tokens=self._total,
            elapsed=time.monotonic() - start, cost=self._cost, plan=self.plan,
        )
        self._emit("finish", success=result.success, summary=result.summary, reason=result.reason,
                   steps=result.steps, sessions=result.sessions, context_tokens=result.context_tokens,
                   total_tokens=result.total_tokens, elapsed=result.elapsed, cost=result.cost)
        return result

    # ----- layer 1: one session ---------------------------------------------

    def _run_session(self, conversation: Conversation) -> _SessionEnd:
        idle = cut_off = step_in_session = 0
        cut_reason = ""  # "length" (the output budget) or "thinking" (the guard), from the last attempt
        cut_thinking = 0  # ...and how many tokens of thinking that attempt managed
        retry = False  # the last reply called nothing: this is the same step again, not the next one
        self._threshold = None
        while self._steps < self.max_steps:
            if self.stop_event.is_set():
                return _SessionEnd("stopped", "Stopped by the user.")
            self._drain_model_requests(conversation)
            self._drain_steering(conversation)
            if not retry:  # a step is a tool call; thinking that ran long and got nudged is not one of its own
                self._steps += 1
                step_in_session += 1
                self._emit("step_start", step=self._steps)
            retry = False
            try:
                budget, think, quiet = self._step_budget(step_in_session, cut_off, cut_reason, cut_thinking)
                guard = ThinkingGuard(think)
                response = self._call_model(conversation, thinking=self.resolved.thinking and not quiet,
                                            max_tokens=budget, guard=guard)
            except Cancelled:
                return _SessionEnd("stopped", "Stopped by the user.")
            except ContextOverflowError:
                self._emit("notice", text="the conversation overflowed the context window; starting a fresh session")
                notes = session_digest(conversation.messages())
                self._emit("handoff", source="digest", notes=notes)
                return _SessionEnd("handoff", notes, source="digest")
            except ModelError as e:
                if self._revert_model(conversation, str(e)):
                    continue  # the model this task switched to is unusable; carry on with the original one
                return _SessionEnd("error", f"Model request failed: {e}")
            # A reply stopped mid-stream never reports usage, and a threshold from a zero prompt would
            # hand off at once.
            if self._threshold is None and response.usage.prompt_tokens:
                self._session_start = response.usage.prompt_tokens
                self._update_threshold()
            self._account(response)
            conversation.append(_assistant_message(response))
            if response.content.strip() or response.reasoning.strip():
                # The complete text, so stored transcripts can be replayed (deltas are not stored).
                self._emit("assistant", text=response.content, reasoning=response.reasoning)

            if not response.tool_calls:
                idle += 1
                pending = not self._steering.empty()
                if response.finish_reason in ("length", "thinking"):
                    cut_off += 1
                    cut_reason = response.finish_reason
                    cut_thinking = guard.tokens
                    self._report_cut_off(step_in_session, cut_off, cut_reason, cut_thinking,
                                         guard if cut_reason == "thinking" else None)
                if idle >= MAX_IDLE_TURNS and response.content.strip() and not pending:
                    return _SessionEnd("finished", response.content.strip(), success=True, reason="implicit_finish")
                if idle > MAX_IDLE_TURNS and not pending:
                    return _SessionEnd("error", self._stuck_message(cut_off))
                if not pending:
                    conversation.append({"role": "user", "content": self._nudge(response.finish_reason, guard)})
                    retry = True
                continue
            idle = cut_off = cut_thinking = 0
            cut_reason = ""

            finished = self._run_tool_calls(conversation, response.tool_calls)
            if finished is not None:
                return _SessionEnd("finished", finished[1], success=finished[0], reason="finished")
            if self._threshold is not None and self._context >= self._threshold:
                notes, source = self._handoff_notes(conversation)
                return _SessionEnd("handoff", notes, source=source)
        # The task is unfinished but the work is real: the notes are what the user continues from.
        notes, _ = self._handoff_notes(conversation, STEP_LIMIT_NOTICE)
        return _SessionEnd("max_steps", f"Stopped after reaching the step limit ({self.max_steps}). "
                                        f"Where it got to:\n\n{notes}")

    def _run_tool_calls(self, conversation: Conversation, calls: list[ToolCall]) -> tuple[bool, str] | None:
        """Execute one turn's calls, append their results, and return finish's arguments if it succeeded.

        Images a tool returned (Read of an image file) follow the results as a
        user message, because tool messages can only hold text.
        """
        finished: tuple[bool, str] | None = None
        results: list[tuple[ToolCall, str]] = []
        images: list[tuple[str, str]] = []
        done_early = self._run_subtasks_in_parallel(calls)
        for call in calls:
            if call.name != "finish":
                output, shown = done_early.pop(call.id) if call.id in done_early else self._execute(call)
                if shown and not self.resolved.model.vision:
                    output += " The current model cannot view images, so it is not shown."
                elif shown:
                    output += " It is shown in the next message."
                    images += [(f"[image returned by {call.name} {self._describe_args(call)}]", url) for url in shown]
                results.append((call, output))
            elif not self._steering.empty():
                results.append((call, STEERING_BLOCKS_FINISH))
            else:
                try:
                    finishing = _finish_arguments(call)
                except ValueError as e:
                    results.append((call, f"Error: {e}. Call finish(success, summary) again."))
                    continue
                pointer = _points_elsewhere(finishing[1])
                if pointer and not self._pointer_refused and not _wrote_visible_text(conversation):
                    # The answer was drafted in thinking, which is not shown and not sent back, and finish only
                    # says where it supposedly is. Once: a model that insists may know better.
                    self._pointer_refused = True
                    self._emit("notice", text=f"finish pointed to a reply that does not exist ({pointer!r}); asked again")
                    results.append((call, POINTER_BLOCKS_FINISH.format(pointer=pointer)))
                    continue
                finished = finishing
                results.append((call, "Task finished."))
        status = f"[step {self._steps}/{self.max_steps} · context {self._context:,}/{self._window():,} tokens"
        if self._plan is not None:  # measured: a plan nobody is shown again is never updated
            status += f" · plan {self._plan.completed()}/{len(self._plan.steps)} done"
        status += "]"
        for index, (call, output) in enumerate(results):
            content = f"{output}\n\n{status}" if index == len(results) - 1 else output
            conversation.append({"role": "tool", "tool_call_id": call.id, "content": content})
        if images:
            parts: list[dict[str, Any]] = []
            for label, url in images:
                parts += [{"type": "text", "text": label}, {"type": "image_url", "image_url": {"url": url}}]
            conversation.append({"role": "user", "content": parts})
        return finished

    @staticmethod
    def _describe_args(call: ToolCall) -> str:
        try:
            args = call.parsed_arguments()
        except ValueError:
            return ""
        return str(args.get("file_path", "")).strip()

    def _drain_steering(self, conversation: Conversation) -> None:
        while not self._steering.empty():
            message, attachments = self._steering.get()
            text = STEERING_TEMPLATE.format(message=message or "(see the attachments)")
            if attachments:
                text += "\n\n" + describe(attachments, heading="# Attached to this message")
            conversation.append({"role": "user", "content": user_content(text, attachments)})
            self._emit("steer", text=message, attachments=_attachment_meta(attachments))

    # ----- switching models -------------------------------------------------

    def _client_for(self, resolved: Resolved) -> OpenAICompatClient:
        client = self._clients.get(resolved.model.name)
        if client is None:
            client = self._clients[resolved.model.name] = make_client(resolved)
        return client

    def _set_model_tool(self, allow_cloud: bool) -> Tool | None:
        """The set_model tool, when there is a model to switch to (the task's own model is always offered back)."""
        targets = self.registry.switch_targets(allow_cloud)
        current = self.resolved.model
        if current.name not in [m.name for m in targets]:
            targets = [current, *targets]
        if len(targets) < 2:
            return None
        lines = [
            model_line(m.name, self.registry.providers[m.provider].is_local(), m.context_length, m.vision, m.notes)
            for m in targets
        ]
        allowed = [m.name for m in targets]
        schema = set_model_tool(lines, allowed)["function"]

        def set_model(model: str, reason: str = "") -> str:
            if model not in allowed:
                raise ToolError(f"unknown model {model!r}; choose one of: {', '.join(allowed)}")
            return self._switch_model(model, by="agent", reason=reason)

        return Tool("set_model", schema["description"], schema["parameters"], set_model)

    def _drain_model_requests(self, conversation: Conversation) -> None:
        while not self._model_requests.empty():
            name = self._model_requests.get()
            try:
                result = self._switch_model(name, by="user")
            except ToolError as e:
                self._emit("notice", text=f"cannot switch to {name}: {e}")
                continue
            if len(conversation) > 2:  # tell the new model; before the first step there is nothing to carry over
                conversation.append({"role": "user", "content": f"[The user moved this task to another model] {result}"})

    def _switch_model(self, name: str, *, by: str, reason: str = "") -> str:
        """Continue the task on model *name*; raise ``ToolError`` when it cannot take over."""
        if name == self.resolved.model.name:
            return f"Already using {name}."
        try:
            resolved = self.registry.resolve(model=name)
        except Exception as e:
            raise ToolError(str(e)) from None
        target = resolved.model
        if self._context > SWITCH_FIT * target.context_length:
            raise ToolError(
                f"the conversation already holds about {self._context:,} tokens, too many for {name}'s "
                f"{target.context_length:,}-token window."
            )
        resolved.thinking = effective_thinking(target, self._thinking_preference)
        previous = self.resolved.model.name
        self.resolved = resolved
        self.client = self._client_for(resolved)
        if self._threshold is not None:
            self._update_threshold()
        local = resolved.provider.is_local()
        self._emit("model_switch", model=name, previous=previous, provider=resolved.provider.name, by=by,
                   reason=reason, local=local, vision=target.vision, thinking=resolved.thinking,
                   context_limit=target.context_length)
        where = "local" if local else "cloud"
        sees = "can view images" if target.vision else "text only"
        return (f"Switched from {previous} to {name} ({where}, {target.context_length:,}-token context, {sees}). "
                "The conversation so far carries over.")

    def _revert_model(self, conversation: Conversation, error: str) -> bool:
        """After a failed request on a model the task switched to, go back to the one it started with.

        A cloud model can be unreachable, out of quota or (as seen) reject its
        key; that must not end a task the original model can still finish.
        """
        failed = self.resolved.model.name
        if failed == self._start_model:
            return False
        try:
            self._switch_model(self._start_model, by="fallback", reason=f"{failed} failed: {error}")
        except ToolError:
            return False
        self._emit("notice", text=f"{failed} failed ({error}); continuing with {self._start_model}")
        conversation.append({"role": "user", "content": (
            f"[The request to {failed} failed: {error}. The task is back on {self._start_model}; "
            f"do not switch to {failed} again. Continue with the tools you have.]")})
        return True

    def _window(self) -> int:
        """The context this agent may use: its model's window, or the smaller cap it was given."""
        full = self.resolved.model.context_length
        return min(self.context_limit, full) if self.context_limit else full

    def _delegate_budget(self) -> int:
        """What a sub-agent may use: the rest of the window, minus room for this conversation to go on.

        Both conversations live in the server's shared KV pool at the same time, so this is what
        keeps a sub-task from evicting the caller's cached prefix. Sub-agents running at once split it.
        """
        reserve = int(MIN_SESSION_ROOM * self._window())
        return (self._window() - self._context - reserve) // self._parallel_share

    def _run_subtasks_in_parallel(self, calls: list[ToolCall]) -> dict[str, tuple[str, list[str]]]:
        """Run this turn's read-only Task calls at the same time, where the model's server can take it.

        Only for a model with ``max_parallel_subtasks`` above 1 (on the A100 four requests at once
        generate 1.8 times as fast as one, but reading is prefill, which does not parallelize: sub-agents
        that mostly read finished no sooner in a live test, docs/design.md 1.6.2), only read-only sub-agents (two
        writers in one worktree would trip over each other), and only as many as the caller's room
        holds: the budget is split between those running at once, so everything still fits the pool.
        The results are returned by call id and go into the conversation in the calls' own order.
        """
        limit = self.resolved.model.max_parallel_subtasks
        batch = [c for c in calls if c.name == "Task" and _reads_only(c)]
        if limit <= 1 or len(batch) < 2 or "Task" not in self.tools:
            return {}
        workers = min(len(batch), limit, self._delegate_budget() // MIN_SUBTASK_CONTEXT)
        if workers < 2:
            return {}  # not enough room for two: they run one after another as usual
        self._parallel_share = workers
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="subtask") as pool:
                running = {call.id: pool.submit(self._execute, call) for call in batch}
                return {call_id: future.result() for call_id, future in running.items()}
        finally:
            self._parallel_share = 1

    def _update_threshold(self) -> None:
        model = self.resolved.model
        self._threshold = handoff_threshold(self._window(), model.continue_at, self._session_start)

    # ----- layer 2: handoff -------------------------------------------------

    def _handoff_notes(self, conversation: Conversation, notice: str = HANDOFF_NOTICE) -> tuple[str, str]:
        """Ask for handoff notes inside the same conversation, so its cached prefix is reused.

        Returns ``(notes, source)``, where source is ``"model"``, or ``"digest"``
        when the model's reply was unusable.

        The tool list stays identical (chat templates render tools into the
        prompt prefix, so dropping them would force a full re-prefill); the
        request simply tells the model not to call any.
        """
        self._emit("notice", text=notice)
        self._emit("handoff_start")  # the content deltas that follow are the notes, not a step's reply
        conversation.append({"role": "user", "content": HANDOFF_REQUEST})
        notes, source = "", "model"
        try:
            response = self.client.chat(
                conversation.messages(), self.schemas, thinking=False, max_tokens=HANDOFF_MAX_TOKENS,
                on_delta=self._on_delta, stop_event=self.stop_event,
            )
            self._total += response.usage.total
            notes = response.content.strip()
        except (ModelError, Cancelled) as e:
            logger.info("handoff notes request failed: %s", e)
        if len(notes) < MIN_HANDOFF_CHARS:
            notes, source = session_digest(conversation.messages()), "digest"
        self._emit("handoff", source=source, notes=notes)
        return notes, source

    # ----- model and tool plumbing ------------------------------------------

    def _emit(self, kind: str, **data: Any) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event({"type": kind, **data})
        except Exception:
            logger.exception("event sink failed")

    def _record_plan(self, plan: Plan) -> None:
        self._plan = plan
        self._emit("plan", id=self._current_call, **plan.to_event())

    def _on_tool_output(self, text: str) -> None:
        self._emit("tool_output", id=self._current_call, text=text)

    def _on_delta(self, kind: str, text: str) -> None:
        self._emit(f"{kind}_delta", text=text)

    def _on_wait(self) -> None:
        self._emit("notice", text="waiting for a free model slot")

    def _account(self, response: ChatResponse) -> None:
        model = self.resolved.model
        self._context = response.usage.total or self._context
        self._total += response.usage.total
        # Priced on the model that actually ran this step, so a task that switched models adds up.
        # Cached input is counted at the full input price, so this is an upper bound.
        step_cost = (response.usage.prompt_tokens * model.price_input
                     + response.usage.completion_tokens * model.price_output) / 1_000_000
        self._cost += step_cost
        self._emit(
            "usage", step=self._steps, prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            reasoning_tokens=response.usage.reasoning_tokens, cached_tokens=response.usage.cached_tokens,
            context_tokens=self._context, context_limit=self._window(), ttft=response.ttft,
            elapsed=response.elapsed, tokens_per_second=response.tokens_per_second,
            cost=step_cost, total_cost=self._cost, model=model.name,
            handoff_at=self._threshold, total_tokens=self._total,
        )

    def _step_budget(self, step_in_session: int, cut_off: int, reason: str = "",
                     thought: int = 0) -> tuple[int, int, bool]:
        """``(output cap, thinking bound, thinking off)`` for the next attempt at a step.

        The first step of a session reads the task and plans the whole job, which is where a thinking model
        legitimately thinks longest, so a model may give it a larger cap (``max_output_tokens_first``).

        After a step that produced no tool call, the same step is tried again with half the thinking it was
        allowed, and -- when it was the output budget that ran out, not the thinking bound -- half of that
        too. A model with no bound of its own gets one for the retry: half of the *thought* tokens the
        stopped reply managed, so every rung is tighter than the last. Only a second failure takes thinking
        away, because a step without it is a worse step.
        """
        model = self.resolved.model
        cap = model.max_output_tokens
        if step_in_session == 1 and model.max_output_tokens_first:
            cap = model.max_output_tokens_first
        think = model.max_thinking_tokens
        if cut_off == 0:
            return cap, think, False
        if reason == "length":  # the whole output went somewhere; the thinking bound was not the one hit
            cap = max(MIN_RETRY_TOKENS, cap // 2)
        elif not think:
            think = thought  # bounded below by MIN_RETRY_THINKING, like a configured bound
        if think:
            think = max(MIN_RETRY_THINKING, think // 2)
        return cap, think, cut_off > 1 and model.thinking == "toggle"

    def _report_cut_off(self, step_in_session: int, cut_off: int, reason: str, thought: int = 0,
                        guard: ThinkingGuard | None = None) -> None:
        """Say why this step produced nothing: the page would otherwise just show a wall of thinking."""
        model = self.resolved.model
        cut_cap, _, _ = self._step_budget(step_in_session, cut_off - 1, reason)
        budget, think, quiet = self._step_budget(step_in_session, cut_off, reason, thought)
        if quiet:
            again = " Trying this step again with thinking off."
        elif reason == "length":
            again = f" Trying this step again with half that budget ({budget:,} tokens)."
        else:
            again = f" Trying this step again on half the thinking ({think:,} tokens)."
        if guard is not None:
            circles = "went in circles while thinking: " if guard.reason == "circles" else ""
            what = f"{model.name} {circles}{guard.detail}, so the reply was stopped there"
        else:
            what = (f"the reply hit {model.name}'s output limit of {cut_cap:,} tokens while still thinking, "
                    "so it called no tool")
        self._emit("notice", text=what + "." + again)

    def _nudge(self, finish_reason: str | None, guard: ThinkingGuard) -> str:
        """What to tell a model whose reply called no tool, by why it stopped."""
        if finish_reason not in ("length", "thinking"):
            return NUDGE
        if finish_reason == "length":
            nudge = CUT_OFF_NUDGE
        else:
            nudge = LOOP_NUDGE if guard.reason == "circles" else LONG_NUDGE
        if "Plan" in self.tools:
            return nudge + PLAN_HINT
        return nudge + FILE_HINT if nudge is LONG_NUDGE else nudge

    def _stuck_message(self, cut_off: int) -> str:
        if cut_off == 0:
            return "The model stopped calling tools without a result."
        model = self.resolved.model
        return (f"{model.name} kept thinking instead of calling a tool, on a shorter budget and with "
                "thinking off as well. Raise max_thinking_tokens (or max_output_tokens) in the model "
                "settings, or use a model that keeps its reasoning shorter.")

    def _call_model(self, conversation: Conversation, thinking: bool | None = None,
                    max_tokens: int | None = None, guard: ThinkingGuard | None = None) -> ChatResponse:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if guard is not None:
                    guard.reset()  # a retry is a new reply; what the failed one said is not a repeat
                return self.client.chat(
                    conversation.messages(), self.schemas,
                    thinking=self.resolved.thinking if thinking is None else thinking, max_tokens=max_tokens,
                    on_delta=self._on_delta, on_wait=self._on_wait, stop_event=self.stop_event, guard=guard,
                )
            except ContextOverflowError:
                raise
            except ModelError as e:
                if not e.retryable or attempt == MAX_RETRIES:
                    raise
                delay = 2 ** attempt
                self._emit("retry", attempt=attempt, error=str(e), delay=delay)
                if self.stop_event.wait(delay):
                    raise Cancelled() from e
        raise AssertionError("unreachable")

    def _execute(self, call: ToolCall) -> tuple[str, list[str]]:
        """Run one tool call; return its text and any images (``data:`` URLs) it produced."""
        tool = self.tools.get(call.name)
        if tool is None:
            names = ", ".join([*self.tools, "finish"])
            return f"Error: unknown tool {call.name!r}. Available tools: {names}.", []
        try:
            args = call.parsed_arguments()
        except ValueError as e:
            return f"Error: the arguments are not a valid JSON object ({e}). Expected {tool.signature()}.", []
        self._current_call = call.id
        self._emit("tool_call", id=call.id, name=call.name, args=args)
        start = time.monotonic()
        images: list[str] = []
        try:
            output, is_error = tool.call(args), False
            if isinstance(output, ToolOutput):
                output, images = output.text, output.images
        except ToolError as e:
            output, is_error = f"Error: {e}", True
        except Exception as e:  # a tool bug must not kill the task; the model sees it and adapts
            logger.exception("tool %s crashed", call.name)
            output, is_error = f"Error: {type(e).__name__}: {e}", True
        self._emit("tool_result", id=call.id, name=call.name, output=output, is_error=is_error,
                   elapsed=time.monotonic() - start, images=len(images))
        return output, images
