"""Layer 4: chats. Tasks in a chat see the earlier tasks and results, and everything is persisted.

Like KISS Sorcar's ``ChatSorcarAgent.build_chat_prompt``, the history shown to
the model is bounded to 10 entries: the first 2 (they usually set the chat's
intent) plus the 8 most recent. Keeping the head fixed also keeps the prompt
prefix stable, which the local prompt cache rewards. Each result is capped as
well, because a local context window is far smaller than a frontier model's.
"""

from pathlib import Path
from typing import Any

from localagent.agent.delegation import make_delegate
from localagent.agent.loop import Agent, AgentResult, EventSink
from localagent.agent.prompts import SCRATCH_NOTE, worktree_note
from localagent.agent.review import FIX_TASK, review_changes
from localagent.attachments import Attachment, AttachmentStore, Placed, place
from localagent.config import is_scratch
from localagent.models.registry import Registry
from localagent.store import Store, TaskRecord
from localagent.tools.workspace import Workspace
from localagent.worktree import ChatWorktree, commit_changes, prepare, working_diff
from localagent.worktree.commit_message import with_trailer, write_message

MAX_HISTORY = 10
HISTORY_HEAD = 2
MAX_RESULT_CHARS = 2000
MAX_TASK_CHARS = 2000
MAX_PLAN_CHARS = 4000
# Models record a plan and then rarely mark steps done (measured), so a stored plan's statuses lag the work.
STALE_STATUSES = "step statuses may be out of date: check the files for what is done"


def select_history(tasks: list[TaskRecord]) -> list[tuple[int, TaskRecord]]:
    """Keep the first ``HISTORY_HEAD`` tasks and the most recent rest, numbered from 1."""
    numbered = list(enumerate(tasks, 1))
    if len(numbered) <= MAX_HISTORY:
        return numbered
    return numbered[:HISTORY_HEAD] + numbered[-(MAX_HISTORY - HISTORY_HEAD):]


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f"\n[... {len(text) - limit} more characters]"


def format_history(tasks: list[TaskRecord], paths: dict[str, str] | None = None) -> str:
    """The chat-history block that precedes a new task, or ``""`` for a new chat.

    Args:
        paths: Where each earlier attachment (by id) is in the workspace; they
            are listed, not shown again, and the model can Read them.
    """
    if not tasks:
        return ""
    selected = select_history(tasks)
    parts = ["## Earlier tasks in this chat (for reference)"]
    if len(selected) < len(tasks):
        parts.append(f"({len(tasks) - len(selected)} tasks in the middle of the chat are omitted.)")
    for number, record in selected:
        status = {True: "succeeded", False: "not completed", None: "interrupted"}[record.success]
        task = _clip(record.task, MAX_TASK_CHARS)
        files = [paths[a["id"]] for a in record.attachments if paths and a.get("id") in paths]
        if files:
            task += "\nAttachments: " + ", ".join(files)
        parts.append(f"### Task {number}\n{task}")
        parts.append(f"### Result {number} ({status})\n{_clip(record.result, MAX_RESULT_CHARS) or '(no result)'}")
        if record.plan and not record.success:  # where to pick it up again ("try again" after an error)
            parts.append(f"### Plan {number} when it stopped ({STALE_STATUSES})\n{_clip(record.plan, MAX_PLAN_CHARS)}")
    return "\n\n".join(parts) + "\n\n---"


class ChatSession:
    """Runs tasks in one chat for one working directory, recording them in the store.

    With ``isolate=True`` (the default) and a git repository, every task of
    the chat runs in the chat's own worktree on branch ``la/<chat id>`` and
    ends with a commit there; the user's checkout is untouched until they
    merge. Outside a repository the tasks run directly in ``work_dir``.
    """

    def __init__(
        self,
        registry: Registry,
        work_dir: str | Path,
        *,
        store: Store | None = None,
        chat_id: str | None = None,
        on_event: EventSink | None = None,
        isolate: bool = True,
        base: str | None = None,
        model_commit_messages: bool = True,
        attachment_store: AttachmentStore | None = None,
        review: bool = False,
        subtasks: bool = True,
        **agent_options: Any,
    ):
        self.registry = registry
        self.work_dir = Path(work_dir).resolve()
        self.store = store or Store()
        self.attachment_store = attachment_store or AttachmentStore()
        self.chat_id = chat_id
        self.on_event = on_event
        self.isolate = isolate
        self.base = base
        self.model_commit_messages = model_commit_messages
        self.review = review  # after a successful task, have the reviewer role read the diff
        self.subtasks = subtasks  # offer the Task tool: work that would fill the context goes to a sub-agent
        self.agent_options = agent_options
        self.agent: Agent | None = None
        self.task_id: str | None = None
        self.worktree: ChatWorktree | None = None

    def steer(self, message: str, attachments: list[Attachment] | None = None) -> None:
        """Send a message (and attachments) to the running task, if any."""
        agent = self.agent
        if agent is None:
            return
        placed = place(self.attachment_store, attachments, agent.workspace.root) if attachments else []
        if placed and self.task_id is not None:
            self.store.add_task_attachments(self.task_id, [a.to_dict() for a in attachments or []])
        agent.steer(message, placed)

    def request_model(self, name: str) -> None:
        """Move the running task to model *name* from its next step."""
        if self.agent is not None:
            self.agent.request_model(name)

    def _task_model(self) -> str | None:
        """The model the task is on right now; helpers left at ``auto`` follow it (``Registry.helper``)."""
        return self.agent.resolved.model.name if self.agent is not None else None

    def _record(self, event: dict[str, Any]) -> None:
        if self.task_id is not None:
            self.store.record_event(self.task_id, event)
        if self.on_event is not None:
            self.on_event(event)

    def _workspace(self) -> tuple[Workspace, str | None, dict[str, Any] | None]:
        """Where the agent works: the chat's worktree, or the user's directory.

        Returns the workspace, the system prompt's git note, and the event describing the choice.
        """
        if is_scratch(self.work_dir):
            # Our own folder: no project, no branches (and never a worktree of the user's home repo).
            return Workspace(self.work_dir), SCRATCH_NOTE, None
        if not self.isolate:
            return Workspace(self.work_dir), None, None
        prepared = prepare(self.work_dir, self.chat_id, base=self.base)
        self.worktree = prepared.worktree
        if self.worktree is None:
            return Workspace(self.work_dir), None, {"type": "notice", "text": f"working directly in {self.work_dir}: {prepared.reason}"}
        wt = self.worktree
        self.store.set_chat_branch(self.chat_id, wt.branch, wt.base)
        workspace = Workspace(wt.work_dir, venv_roots=[self.work_dir, wt.repo], protected_roots=[wt.repo])
        event = {
            "type": "worktree", "branch": wt.branch, "base": wt.base, "path": str(wt.path),
            "created": prepared.created, "restored": prepared.restored,
            "carries_user_changes": wt.carries_user_changes,
        }
        return workspace, worktree_note(wt.branch, wt.base), event

    def _reviewed(self, task: str, result: AgentResult) -> AgentResult:
        """Let the reviewer read what this task changed, and fix what it finds.

        Runs before the commit, so the fix lands in the same commit as the work.
        """
        if self.worktree is None:
            self._record({"type": "notice", "text": "review needs a git repository; skipped"})
            return result
        diff = working_diff(self.worktree)
        if not diff.strip():
            return result
        clean, findings, model = review_changes(
            self.registry, self.agent.workspace, task, diff, on_event=self._record,
            stop_event=self.agent.stop_event, approver=self.agent_options.get("approver"),
            task_model=self._task_model(),
        )
        self._record({"type": "review", "model": model, "clean": clean, "findings": findings})
        if clean or self.agent.stop_event.is_set():
            return result
        fixed = self.agent.run(FIX_TASK.format(task=task, findings=findings))
        fixed.summary = "\n\n".join([
            result.summary,
            f"---\n**{model} reviewed this and found:**\n{findings}",
            f"**Then fixed:**\n{fixed.summary}",
        ])
        fixed.steps += result.steps
        fixed.total_tokens += result.total_tokens
        fixed.cost += result.cost
        fixed.elapsed += result.elapsed
        return fixed

    def _commit(self, task: str, result: AgentResult) -> None:
        """Commit what the task changed on the chat's branch."""
        if self.worktree is None:
            return
        registry = self.registry if self.model_commit_messages else None

        def message_for(diff: str) -> str:
            return with_trailer(write_message(registry, task, diff, task_model=self._task_model()), task,
                                result.summary)

        committed = commit_changes(self.worktree, message_for)
        if committed is not None:
            self._record({"type": "task_committed", "branch": self.worktree.branch, "commit": committed[0],
                          "subject": committed[1]})

    def _place_attachments(self, root: Path, earlier: list[TaskRecord],
                           attachments: list[Attachment]) -> tuple[dict[str, str], list[Placed]]:
        """Put the chat's attachments into the workspace: earlier tasks' (referenced) and this task's (shown).

        Returns where each earlier attachment is, by id, and this task's placed attachments.
        """
        previous: list[Attachment] = []
        for record in earlier:
            for meta in record.attachments:
                try:
                    previous.append(self.attachment_store.get(meta.get("id", "")))
                except KeyError:
                    continue  # removed from the store; the history still names the task
        if not previous and not attachments:
            return {}, []
        placed = place(self.attachment_store, previous + attachments, root, inline={a.id for a in attachments})
        paths = {p.attachment.id: p.path for p in placed[:len(previous)]}
        return paths, placed[len(previous):]

    def run(self, task: str, attachments: list[Attachment] | None = None) -> AgentResult:
        """Run *task* (with files the user attached) using this chat's history, and store the outcome."""
        attachments = attachments or []
        if self.chat_id is None:
            self.chat_id = self.store.new_chat(self.work_dir, title=task.strip().splitlines()[0][:80] if task.strip() else "")
        earlier = self.store.tasks(self.chat_id)
        workspace, git_note, workspace_event = self._workspace()
        paths, placed = self._place_attachments(workspace.root, earlier, attachments)
        history = format_history(earlier, paths)
        delegate = None
        if self.subtasks:
            delegate = make_delegate(
                self.registry, workspace, self.store, self.chat_id, lambda: self.task_id, self._record,
                approver=self.agent_options.get("approver"), stop_event=self.agent_options.get("stop_event"),
                git_note=git_note, task_model=self._task_model,
            )
        self.agent = Agent(self.registry, workspace, git_note=git_note, on_event=self._record,
                           delegate=delegate, **self.agent_options)
        self.task_id = self.store.add_task(self.chat_id, task, self.agent.resolved.model.name, self.work_dir,
                                           attachments=[a.to_dict() for a in attachments])
        self._record({"type": "task_record", "chat_id": self.chat_id, "task_id": self.task_id})
        if workspace_event is not None:
            self._record(workspace_event)
        try:
            result = self.agent.run(task, chat_context=history, attachments=placed)
            if self.review and result.success:
                result = self._reviewed(task, result)
        except BaseException:
            self.store.finish_task(self.task_id, success=False, result="Task interrupted.", reason="interrupted",
                                   plan=self.agent.plan)
            raise
        finally:
            self.agent.close()
            if workspace.background.stop():
                # The model is told to stop what it started; when it forgets, its dev server would
                # otherwise hold the port until the user found it.
                self._record({"type": "notice", "text": "stopped the processes this task's commands "
                                                        "left running in the background"})
        self._commit(task, result)
        self.store.finish_task(
            self.task_id, success=result.success, result=result.summary, reason=result.reason,
            steps=result.steps, sessions=result.sessions, total_tokens=result.total_tokens,
            elapsed=result.elapsed, cost=result.cost, plan=result.plan,
        )
        return result
