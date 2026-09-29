"""SQLite persistence for chats, tasks and their event transcripts.

Modelled on KISS Sorcar's ``sorcar.db`` (``task_history`` + ``events``), with
one difference: streaming token deltas are never stored. Only transcript-level
events (tool calls, results, usage, continuations, the final result) are
written, so a single locked connection is plenty and no background writer
thread is needed.
"""

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from localagent.config import home_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    work_dir TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(id),
    created REAL NOT NULL,
    finished REAL,
    task TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT '',
    success INTEGER,
    reason TEXT NOT NULL DEFAULT 'running',
    model TEXT NOT NULL DEFAULT '',
    work_dir TEXT NOT NULL,
    steps INTEGER NOT NULL DEFAULT 0,
    sessions INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    elapsed REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    UNIQUE (task_id, seq)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS folders (
    path TEXT PRIMARY KEY,
    opened REAL NOT NULL DEFAULT 0,
    forgotten REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_chat ON tasks (chat_id, created);
CREATE INDEX IF NOT EXISTS idx_chats_work_dir ON chats (work_dir, created);
"""

# Columns added after the first release, created on older databases by ``_migrate``.
ADDED_COLUMNS = {
    "chats": (("branch", "TEXT NOT NULL DEFAULT ''"), ("base", "TEXT NOT NULL DEFAULT ''")),
    "tasks": (("attachments", "TEXT NOT NULL DEFAULT '[]'"), ("cost", "REAL NOT NULL DEFAULT 0"),
              ("parent_id", "TEXT NOT NULL DEFAULT ''"), ("plan", "TEXT NOT NULL DEFAULT ''")),
}

STREAMING_EVENTS = frozenset({"reasoning_delta", "content_delta", "tool_output"})


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _normalize_dir(work_dir: str | Path) -> str:
    return str(Path(work_dir).resolve())


@dataclass
class TaskRecord:
    """One row of ``tasks``."""

    id: str
    chat_id: str
    created: float
    finished: float | None
    task: str
    result: str
    success: bool | None
    reason: str
    model: str
    work_dir: str
    steps: int
    sessions: int
    total_tokens: int
    elapsed: float
    attachments: list[dict[str, Any]] = field(default_factory=list)  # ``Attachment.to_dict()`` entries
    cost: float = 0.0  # USD, 0 for local models
    parent_id: str = ""  # set on a sub-agent's task: its caller
    plan: str = ""  # the last plan the model recorded, as a checklist


@dataclass
class ChatRecord:
    """One row of ``chats`` plus a summary of its tasks."""

    id: str
    created: float
    work_dir: str
    title: str
    task_count: int
    last_task: str
    last_reason: str
    branch: str = ""
    base: str = ""


class Store:
    """Thread-safe access to the LocalAgent database."""

    def __init__(self, path: Path | None = None):
        self.path = path or home_dir() / "localagent.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.executescript(SCHEMA)
        self._migrate()
        self._seq: dict[str, int] = {}
        self._folder_clock = 0.0

    def _migrate(self) -> None:
        for table, columns in ADDED_COLUMNS.items():
            existing = {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns:
                if name not in existing:
                    try:
                        self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                    except sqlite3.OperationalError:  # another process added it first
                        pass

    def set_chat_branch(self, chat_id: str, branch: str, base: str) -> None:
        """Record the git branch a chat works on and the branch it came from."""
        with self._lock:
            self._db.execute("UPDATE chats SET branch = ?, base = ? WHERE id = ?", (branch, base, chat_id))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def new_chat(self, work_dir: str | Path, title: str = "") -> str:
        """Create a chat for *work_dir* and return its id."""
        chat_id = _new_id()
        with self._lock:
            self._db.execute(
                "INSERT INTO chats (id, created, work_dir, title) VALUES (?, ?, ?, ?)",
                (chat_id, time.time(), _normalize_dir(work_dir), title[:200]),
            )
        return chat_id

    def chat_exists(self, chat_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone() is not None

    def chat(self, chat_id: str) -> ChatRecord | None:
        """One chat with its task summary, or ``None``."""
        for record in self.list_chats(limit=1, chat_id=chat_id):
            return record
        return None

    def recent_folders(self, limit: int = 20) -> list[str]:
        """Folders opened in the UI or worked on by a chat, most recent first.

        A folder the user removed from the list stays out of it until it is opened or used again.
        """
        with self._lock:
            rows = self._db.execute(
                """
                SELECT used.path, MAX(used.at) AS last FROM (
                    SELECT path, opened AS at FROM folders
                    UNION ALL SELECT work_dir, created FROM chats
                ) used LEFT JOIN folders f ON f.path = used.path
                GROUP BY used.path HAVING last > COALESCE(MAX(f.forgotten), 0)
                ORDER BY last DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [r["path"] for r in rows]

    def delete_chat(self, chat_id: str) -> int:
        """Delete a chat with its tasks (sub-agent runs included) and their events; return the task count.

        Attachments are left alone: they are content-addressed and shared between chats.
        """
        with self._lock:
            tasks = [r["id"] for r in self._db.execute("SELECT id FROM tasks WHERE chat_id = ?", (chat_id,))]
            self._db.execute("BEGIN")
            try:
                for task_id in tasks:
                    self._db.execute("DELETE FROM events WHERE task_id = ?", (task_id,))
                self._db.execute("DELETE FROM tasks WHERE chat_id = ?", (chat_id,))
                self._db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            for task_id in tasks:
                self._seq.pop(task_id, None)
        return len(tasks)

    def orphaned_chats(self) -> list[ChatRecord]:
        """Chats whose folder no longer exists, newest first: the project was deleted or moved.

        A folder that is only unreachable for now (an unplugged disk, a network home whose login
        expired) looks the same, which is why these are offered for deletion and never deleted on sight.
        """
        return [c for c in self.list_chats(limit=-1) if not Path(c.work_dir).is_dir()]

    def attachments_in_use(self) -> set[str]:
        """Ids of the attachments that some task still refers to."""
        with self._lock:
            rows = self._db.execute("SELECT attachments FROM tasks").fetchall()
        return {a["id"] for r in rows for a in json.loads(r["attachments"] or "[]") if a.get("id")}

    def placed_attachments(self, chat_id: str) -> set[str]:
        """Where this chat's tasks copied attachments, relative to their workspace (``.la-attachments/...``).

        Read back from the ``task_start`` and ``steer`` events, which record each copy's path; a PDF's
        extracted text sits beside it as ``<path>.txt``.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT e.data FROM events e JOIN tasks t ON t.id = e.task_id "
                "WHERE t.chat_id = ? AND e.type IN ('task_start', 'steer')", (chat_id,)).fetchall()
        paths: set[str] = set()
        for row in rows:
            for attachment in json.loads(row["data"]).get("attachments") or []:
                if attachment.get("path"):
                    paths.add(attachment["path"])
                    if attachment.get("kind") == "pdf":
                        paths.add(attachment["path"] + ".txt")
        return paths

    def delete_folder_record(self, path: str | Path) -> None:
        """Drop a folder from the table of opened and removed folders (``forget_folder`` only hides it)."""
        with self._lock:
            self._db.execute("DELETE FROM folders WHERE path = ?", (_normalize_dir(path),))

    def settings(self) -> dict[str, Any]:
        """The page's own settings (model per folder, thinking, cloud, review…), as they were stored.

        They live here and not in the page's storage because the desktop window opens on a new port every
        start, and browser storage is per origin: whatever the page saved last time would be unreachable.
        """
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM settings").fetchall()
        found = {}
        for row in rows:
            try:
                found[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                continue  # hand-edited into something that is not JSON: ignore that one
        return found

    def save_settings(self, values: dict[str, Any]) -> None:
        """Store settings by key; a value of ``None`` removes the key."""
        with self._lock:
            for key, value in values.items():
                if value is None:
                    self._db.execute("DELETE FROM settings WHERE key = ?", (key,))
                else:
                    self._db.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, json.dumps(value)),
                    )

    def open_folder(self, path: str | Path) -> None:
        """Record that the UI opened *path*, which puts it first in ``recent_folders``."""
        self._mark_folder(path, "opened")

    def forget_folder(self, path: str | Path) -> None:
        """Take *path* off the recent list (its chats are kept)."""
        self._mark_folder(path, "forgotten")

    def _mark_folder(self, path: str | Path, column: str) -> None:
        with self._lock:
            # Strictly increasing, so opening a folder right after removing it (same clock tick) still shows it.
            self._folder_clock = max(time.time(), self._folder_clock + 1e-6)
            self._db.execute(
                f"INSERT INTO folders (path, {column}) VALUES (?, ?) "
                f"ON CONFLICT(path) DO UPDATE SET {column} = excluded.{column}",
                (_normalize_dir(path), self._folder_clock),
            )

    def latest_chat(self, work_dir: str | Path) -> str | None:
        """The most recently created chat for *work_dir*, if any."""
        with self._lock:
            row = self._db.execute(
                "SELECT id FROM chats WHERE work_dir = ? ORDER BY created DESC, rowid DESC LIMIT 1",
                (_normalize_dir(work_dir),),
            ).fetchone()
        return row["id"] if row else None

    def list_chats(self, work_dir: str | Path | None = None, limit: int = 20,
                   chat_id: str | None = None) -> list[ChatRecord]:
        """Recent chats, newest first, optionally only those for *work_dir* (or the one with *chat_id*)."""
        if chat_id is not None:
            where, params = "WHERE c.id = ?", [chat_id]
        elif work_dir:
            where, params = "WHERE c.work_dir = ?", [_normalize_dir(work_dir)]
        else:
            where, params = "", []
        with self._lock:
            rows = self._db.execute(
                f"""
                SELECT c.id, c.created, c.work_dir, c.title, c.branch, c.base,
                       (SELECT COUNT(*) FROM tasks t WHERE t.chat_id = c.id AND t.parent_id = '') AS task_count,
                       (SELECT t.task FROM tasks t WHERE t.chat_id = c.id AND t.parent_id = ''
                         ORDER BY t.created DESC, t.rowid DESC LIMIT 1) AS last_task,
                       (SELECT t.reason FROM tasks t WHERE t.chat_id = c.id AND t.parent_id = ''
                         ORDER BY t.created DESC, t.rowid DESC LIMIT 1) AS last_reason
                FROM chats c {where}
                ORDER BY c.created DESC, c.rowid DESC LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [ChatRecord(r["id"], r["created"], r["work_dir"], r["title"], r["task_count"],
                           r["last_task"] or "", r["last_reason"] or "", r["branch"], r["base"]) for r in rows]

    def add_task(self, chat_id: str, task: str, model: str, work_dir: str | Path,
                 attachments: list[dict[str, Any]] | None = None, parent_id: str = "") -> str:
        """Record a task that is starting; return its id. A sub-agent's task names its caller."""
        task_id = _new_id()
        with self._lock:
            self._db.execute(
                "INSERT INTO tasks (id, chat_id, created, task, model, work_dir, attachments, parent_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, chat_id, time.time(), task, model, _normalize_dir(work_dir),
                 json.dumps(attachments or [], ensure_ascii=False), parent_id),
            )
        return task_id

    def add_task_attachments(self, task_id: str, attachments: list[dict[str, Any]]) -> None:
        """Add attachments sent to a running task (with a steering message)."""
        with self._lock:
            row = self._db.execute("SELECT attachments FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return
            known = json.loads(row["attachments"] or "[]")
            ids = {a.get("id") for a in known}
            known += [a for a in attachments if a.get("id") not in ids]
            self._db.execute("UPDATE tasks SET attachments = ? WHERE id = ?",
                             (json.dumps(known, ensure_ascii=False), task_id))

    def finish_task(self, task_id: str, *, success: bool, result: str, reason: str, steps: int = 0,
                    sessions: int = 0, total_tokens: int = 0, elapsed: float = 0.0, cost: float = 0.0,
                    plan: str = "") -> None:
        """Store how a task ended."""
        with self._lock:
            self._db.execute(
                """UPDATE tasks SET finished = ?, success = ?, result = ?, reason = ?, steps = ?,
                   sessions = ?, total_tokens = ?, elapsed = ?, cost = ?, plan = ? WHERE id = ?""",
                (time.time(), int(success), result, reason, steps, sessions, total_tokens, elapsed, cost, plan,
                 task_id),
            )

    def record_event(self, task_id: str, event: dict[str, Any]) -> None:
        """Append one transcript event; streaming deltas are ignored."""
        if event.get("type") in STREAMING_EVENTS:
            return
        with self._lock:
            seq = self._seq.get(task_id)
            if seq is None:
                row = self._db.execute("SELECT MAX(seq) AS s FROM events WHERE task_id = ?", (task_id,)).fetchone()
                seq = row["s"] or 0
            seq += 1
            self._seq[task_id] = seq
            self._db.execute(
                "INSERT INTO events (task_id, seq, ts, type, data) VALUES (?, ?, ?, ?, ?)",
                (task_id, seq, time.time(), event["type"], json.dumps(event, ensure_ascii=False, default=str)),
            )

    def tasks(self, chat_id: str, parent_id: str | None = "") -> list[TaskRecord]:
        """Tasks of a chat, oldest first.

        By default only the chat's own tasks; pass a task id for that task's
        sub-agent runs, or ``None`` for everything.
        """
        where = "chat_id = ?"
        params: list[Any] = [chat_id]
        if parent_id is not None:
            where += " AND parent_id = ?"
            params.append(parent_id)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM tasks WHERE {where} ORDER BY created ASC, rowid ASC", params
            ).fetchall()
        return [
            TaskRecord(
                r["id"], r["chat_id"], r["created"], r["finished"], r["task"], r["result"],
                None if r["success"] is None else bool(r["success"]), r["reason"], r["model"],
                r["work_dir"], r["steps"], r["sessions"], r["total_tokens"], r["elapsed"],
                json.loads(r["attachments"] or "[]"), r["cost"], r["parent_id"], r["plan"],
            )
            for r in rows
        ]

    def events(self, task_id: str) -> list[dict[str, Any]]:
        """A task's stored events in order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM events WHERE task_id = ? ORDER BY seq ASC", (task_id,)
            ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def find_chat(self, prefix: str) -> str | None:
        """Resolve a unique chat id from a prefix of at least 4 characters."""
        if len(prefix) < 4:
            return None
        with self._lock:
            rows = self._db.execute("SELECT id FROM chats WHERE id LIKE ? LIMIT 2", (prefix + "%",)).fetchall()
        return rows[0]["id"] if len(rows) == 1 else None
