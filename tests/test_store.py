import pytest

from localagent.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_chats_tasks_and_events(store, project):
    chat = store.new_chat(project, title="first")
    assert store.latest_chat(project) == chat and store.chat_exists(chat)
    task = store.add_task(chat, "fix bug", "qwen", project)
    store.record_event(task, {"type": "content_delta", "text": "ignored"})
    store.record_event(task, {"type": "tool_call", "name": "Read", "args": {"file_path": "a.py"}})
    store.record_event(task, {"type": "finish", "success": True})
    store.finish_task(task, success=True, result="fixed", reason="finished", steps=3, sessions=1,
                      total_tokens=1234, elapsed=5.0)

    [record] = store.tasks(chat)
    assert (record.task, record.result, record.success, record.steps, record.model) == ("fix bug", "fixed", True, 3, "qwen")
    assert [e["type"] for e in store.events(task)] == ["tool_call", "finish"]


def test_running_task_has_unknown_success(store, project):
    chat = store.new_chat(project)
    store.add_task(chat, "long task", "m", project)
    [record] = store.tasks(chat)
    assert record.success is None and record.reason == "running"


def test_list_chats_filters_by_work_dir(store, project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    old = store.new_chat(project)
    store.add_task(old, "one", "m", project)
    store.add_task(old, "two", "m", project)
    store.new_chat(other)
    [chat] = store.list_chats(project)
    assert (chat.id, chat.task_count, chat.last_task) == (old, 2, "two")
    assert len(store.list_chats()) == 2


def test_find_chat_by_prefix(store, project):
    chat = store.new_chat(project)
    assert store.find_chat(chat[:6]) == chat
    assert store.find_chat(chat[:3]) is None
    assert store.find_chat("zzzzzz") is None


def test_event_sequence_survives_reopen(tmp_path, project):
    path = tmp_path / "seq.db"
    first = Store(path)
    task = first.add_task(first.new_chat(project), "t", "m", project)
    first.record_event(task, {"type": "a"})
    first.close()
    second = Store(path)
    second.record_event(task, {"type": "b"})
    assert [e["type"] for e in second.events(task)] == ["a", "b"]
    second.close()


def test_task_attachments_and_migration_of_old_databases(tmp_path, project):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE chats (id TEXT PRIMARY KEY, created REAL NOT NULL, work_dir TEXT NOT NULL, title TEXT NOT NULL DEFAULT '');"
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, created REAL NOT NULL, finished REAL, "
        "task TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', success INTEGER, reason TEXT NOT NULL DEFAULT 'running', "
        "model TEXT NOT NULL DEFAULT '', work_dir TEXT NOT NULL, steps INTEGER NOT NULL DEFAULT 0, "
        "sessions INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0, elapsed REAL NOT NULL DEFAULT 0);"
        "INSERT INTO chats VALUES ('c1', 1, 'x', 't');"
        "INSERT INTO tasks (id, chat_id, created, task, work_dir) VALUES ('t1', 'c1', 1, 'old task', 'x');"
    )
    old.commit()
    old.close()
    store = Store(path)
    assert store.tasks("c1")[0].attachments == []
    task = store.add_task("c1", "new", "m", project, attachments=[{"id": "a1", "name": "shot.png"}])
    store.add_task_attachments(task, [{"id": "a1", "name": "shot.png"}, {"id": "a2", "name": "note.md"}])
    assert [a["id"] for a in store.tasks("c1")[1].attachments] == ["a1", "a2"]
    store.close()


def test_recent_folders_follow_opens_and_chats_and_stay_removed(store, project, tmp_path):
    import time

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store.new_chat(project)  # folders worked on from the command line count too
    time.sleep(0.05)
    store.open_folder(first)
    store.open_folder(second)
    assert store.recent_folders() == [str(second.resolve()), str(first.resolve()), str(project.resolve())]

    store.open_folder(first)
    assert store.recent_folders()[0] == str(first.resolve())

    store.forget_folder(project)
    store.forget_folder(second)
    assert store.recent_folders() == [str(first.resolve())]
    store.open_folder(second)  # opening it again brings it back, even in the same clock tick
    assert store.recent_folders() == [str(second.resolve()), str(first.resolve())]
