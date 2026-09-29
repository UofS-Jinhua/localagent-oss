"""Run agent tasks for the UI: one at a time, in a worker thread, publishing every event."""

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from localagent.agent import ChatSession
from localagent.attachments import Attachment, AttachmentStore
from localagent.models.registry import Registry
from localagent.store import Store
from localagent.worktree import repo_status

logger = logging.getLogger(__name__)

Publish = Callable[..., None]  # (event, remember=True) -> None, like Hub.publish
APPROVAL_TIMEOUT = 600
QUESTION_TIMEOUT = 1800  # the user may be away; the agent says so and carries on


class Busy(Exception):
    """A task is already running."""


@dataclass
class RunRequest:
    """What the UI asks to run."""

    work_dir: str
    task: str
    chat_id: str | None = None
    base: str | None = None
    isolate: bool = True
    model: str | None = None
    thinking: bool | None = None
    max_steps: int = 100
    max_sessions: int = 5
    plain_commits: bool = False
    attachments: list[str] = field(default_factory=list)  # ids in the AttachmentStore
    allow_cloud: bool = False  # set_model may offer cloud models
    review: bool = False  # a second pass reads the diff before the commit
    subtasks: bool = True  # offer the Task tool (a sub-agent with its own context)


class Approvals:
    """Command approvals asked from the agent thread and answered by a UI client."""

    def __init__(self, publish: Publish):
        self.publish = publish
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, list[bool]]] = {}

    def ask(self, command: str, reason: str) -> bool:
        request_id = uuid.uuid4().hex[:8]
        answered, slot = threading.Event(), [False]
        with self._lock:
            self._pending[request_id] = (answered, slot)
        self.publish({"type": "approval_request", "id": request_id, "command": command, "reason": reason})
        answered.wait(APPROVAL_TIMEOUT)
        with self._lock:
            self._pending.pop(request_id, None)
        self.publish({"type": "approval_resolved", "id": request_id, "allowed": slot[0]})
        return slot[0]

    def answer(self, request_id: str, allow: bool) -> bool:
        with self._lock:
            entry = self._pending.get(request_id)
        if entry is None:
            return False
        entry[1][0] = allow
        entry[0].set()
        return True

    def deny_all(self) -> None:
        with self._lock:
            entries = list(self._pending.values())
        for answered, _ in entries:
            answered.set()


class Questions:
    """AskUser questions asked from the agent thread and answered by a UI client."""

    def __init__(self, publish: Publish):
        self.publish = publish
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, list[str]]] = {}

    def ask(self, question: str, options: list[str]) -> str:
        request_id = uuid.uuid4().hex[:8]
        answered, slot = threading.Event(), [""]
        with self._lock:
            self._pending[request_id] = (answered, slot)
        self.publish({"type": "question_request", "id": request_id, "question": question, "options": options})
        answered.wait(QUESTION_TIMEOUT)
        with self._lock:
            self._pending.pop(request_id, None)
        self.publish({"type": "question_resolved", "id": request_id, "answer": slot[0]})
        return slot[0]

    def answer(self, request_id: str, text: str) -> bool:
        with self._lock:
            entry = self._pending.get(request_id)
        if entry is None:
            return False
        entry[1][0] = text
        entry[0].set()
        return True

    def close_all(self) -> None:
        """Unblock every waiting question (the task is stopping)."""
        with self._lock:
            entries = list(self._pending.values())
        for answered, _ in entries:
            answered.set()


@dataclass
class _Running:
    request: RunRequest
    session: ChatSession
    stop: threading.Event
    thread: threading.Thread
    started: float = field(default_factory=time.time)


class TaskRunner:
    """Owns the single running task and its controls."""

    def __init__(self, store: Store, publish: Publish, registry_loader: Callable[[], Registry] = Registry.load,
                 attachments: AttachmentStore | None = None):
        self.store = store
        self.publish = publish
        self.registry_loader = registry_loader
        self.attachments = attachments or AttachmentStore()
        self.approvals = Approvals(publish)
        self.questions = Questions(publish)
        self._lock = threading.Lock()
        self._current: _Running | None = None

    def _attachments(self, ids: list[str]) -> list[Attachment]:
        try:
            return [self.attachments.get(i) for i in ids]
        except KeyError as e:
            raise ValueError(f"unknown attachment {e.args[0]!r}; upload it again") from None

    def state(self) -> dict[str, Any]:
        with self._lock:
            current = self._current
        if current is None:
            return {"busy": False}
        return {"busy": True, "chat_id": current.session.chat_id, "task_id": current.session.task_id,
                "work_dir": current.request.work_dir, "task": current.request.task, "started": current.started}

    def running_chat(self) -> str | None:
        with self._lock:
            return self._current.session.chat_id if self._current else None

    def start(self, request: RunRequest, on_start: Callable[[], None] | None = None) -> str:
        """Start *request* in a worker thread and return its chat id; raise ``Busy`` if a task runs."""
        work_dir = Path(request.work_dir).expanduser().resolve()
        if not work_dir.is_dir():
            raise ValueError(f"{work_dir} is not a directory")
        if not request.task.strip():
            raise ValueError("the task is empty")
        attachments = self._attachments(request.attachments)
        registry = self.registry_loader()
        with self._lock:
            if self._current is not None:
                raise Busy("a task is already running; stop it or wait for it to finish")
            chat_id = request.chat_id or self.store.new_chat(work_dir, title=request.task.strip().splitlines()[0][:80])
            stop = threading.Event()
            session = ChatSession(
                registry, work_dir, store=self.store, chat_id=chat_id, on_event=None,
                isolate=request.isolate, base=request.base or None,
                model_commit_messages=not request.plain_commits, attachment_store=self.attachments,
                review=request.review, subtasks=request.subtasks,
                model=request.model or None, thinking=request.thinking, max_steps=request.max_steps,
                max_sessions=request.max_sessions, approver=self.approvals.ask, stop_event=stop,
                allow_cloud=request.allow_cloud, asker=self.questions.ask,
            )
            session.on_event = lambda event, s=session: self.publish({**event, "chat_id": s.chat_id, "task_id": s.task_id})
            thread = threading.Thread(target=self._run, args=(session, request, attachments), name="agent-task",
                                      daemon=True)
            self._current = _Running(request, session, stop, thread)
        if on_start is not None:
            on_start()
        self.publish({"type": "task_started", "chat_id": chat_id, "work_dir": str(work_dir), "task": request.task,
                      "started": time.time(), "request": asdict(request),
                      "attachments": [a.to_dict() for a in attachments]})
        thread.start()
        return chat_id

    def _run(self, session: ChatSession, request: RunRequest, attachments: list[Attachment]) -> None:
        result = None
        try:
            result = session.run(request.task, attachments)
        except Exception as e:  # keep the server alive; the UI shows the error
            logger.exception("task crashed")
            self.publish({"type": "error", "message": f"{type(e).__name__}: {e}", "chat_id": session.chat_id})
        finally:
            with self._lock:
                self._current = None
            self.publish({
                "type": "task_finished", "chat_id": session.chat_id, "task_id": session.task_id,
                "success": bool(result and result.success), "reason": result.reason if result else "error",
            })
            self.publish({"type": "repo", "work_dir": request.work_dir, **status_payload(request.work_dir)},
                         remember=False)

    def steer(self, text: str, attachment_ids: list[str] | None = None) -> bool:
        """Send a message (and uploaded attachments) to the running task; raise ``ValueError`` for unknown ids."""
        attachments = self._attachments(attachment_ids or [])
        with self._lock:
            current = self._current
        if current is None or not (text.strip() or attachments):
            return False
        current.session.steer(text, attachments)
        return True

    def set_model(self, name: str) -> bool:
        """Move the running task to model *name* from its next step."""
        with self._lock:
            current = self._current
        if current is None or not name:
            return False
        current.session.request_model(name)
        return True

    def stop(self) -> bool:
        with self._lock:
            current = self._current
        if current is None:
            return False
        current.stop.set()
        self.approvals.deny_all()
        self.questions.close_all()
        return True


def status_payload(work_dir: str | Path) -> dict[str, Any]:
    """``repo_status`` as JSON-friendly data."""
    status = repo_status(work_dir)
    return {
        "repo": str(status.repo) if status.repo else None,
        "branch": status.branch,
        "dirty_files": status.dirty_files,
        "pending": [asdict(p) for p in status.pending],
    }
