import pytest

from localagent.agent import ChatSession
from localagent.agent.chat import MAX_RESULT_CHARS, format_history, select_history
from localagent.store import Store, TaskRecord


def record(n: int, result: str = "done", success: bool | None = True) -> TaskRecord:
    return TaskRecord(f"t{n}", "c", float(n), float(n), f"task {n}", result, success, "finished",
                      "m", ".", 1, 1, 10, 1.0)


def test_history_keeps_first_two_and_last_eight():
    numbers = [n for n, _ in select_history([record(i) for i in range(1, 13)])]
    assert numbers == [1, 2, 5, 6, 7, 8, 9, 10, 11, 12]


def test_history_formatting():
    text = format_history([record(1, "x" * (MAX_RESULT_CHARS + 50)), record(2, "", False), record(3, "", None)])
    assert "### Task 1\ntask 1" in text and "[... 50 more characters]" in text
    assert "### Result 2 (not completed)\n(no result)" in text
    assert "### Result 3 (interrupted)" in text
    assert format_history([]) == ""
    assert "2 tasks in the middle of the chat are omitted" in format_history([record(i) for i in range(1, 13)])


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "chat.db")
    yield s
    s.close()


def test_a_task_stops_the_server_it_started_for_testing(llm, registry, project, store):
    """The model is told to stop what it started; when it forgets, the task must not leave it running."""
    import shlex
    import sys
    import time
    from pathlib import Path

    beat = 'import sys, time\nlog = open(sys.argv[1], "a", buffering=1)\nwhile True:\n    log.write(".")\n    log.flush()\n    time.sleep(0.05)\n'
    (project / "beat.py").write_text(beat, encoding="utf-8")
    log = project / "beat.log"
    python = shlex.quote(Path(sys.executable).as_posix())
    llm.add_tool_calls([("Bash", {"command": f"nohup {python} beat.py beat.log > /dev/null 2>&1 &"})])
    llm.add_tool_calls([("Bash", {"command": "sleep 1"})])  # the task carries on while the server runs
    llm.add_tool_calls([("finish", {"success": True, "summary": "started it and forgot to stop it"})])

    session = ChatSession(registry, project, store=store)
    session.run("start the server")

    time.sleep(0.4)
    after = log.read_text(encoding="utf-8")
    time.sleep(0.5)
    assert log.read_text(encoding="utf-8") == after and after, "the task left its server running"
    notices = [e for e in store.events(session.task_id) if e["type"] == "notice"]
    assert any("left running in the background" in e["text"] for e in notices)


def test_second_task_sees_the_first(llm, registry, project, store):
    session = ChatSession(registry, project, store=store)
    llm.add_tool_calls([("finish", {"success": True, "summary": "The answer is 42."})])
    session.run("what is the answer?")
    llm.add_tool_calls([("finish", {"success": True, "summary": "Doubled: 84."})])
    session.run("double it")

    opening = llm.requests[1]["messages"][1]["content"]
    assert "### Task 1\nwhat is the answer?" in opening and "The answer is 42." in opening
    assert opening.rstrip().endswith("# Task (work on it now)\n\ndouble it")
    tasks = store.tasks(session.chat_id)
    assert [t.task for t in tasks] == ["what is the answer?", "double it"]
    assert [t.result for t in tasks] == ["The answer is 42.", "Doubled: 84."]
    types = [e["type"] for e in store.events(tasks[0].id)]
    assert types[0] == "task_record" and "finish" in types and "content_delta" not in types


def test_resumed_chat_uses_stored_history(llm, registry, project, store):
    first = ChatSession(registry, project, store=store)
    llm.add_tool_calls([("finish", {"success": True, "summary": "created notes.md"})])
    first.run("create notes.md")
    resumed = ChatSession(registry, project, store=store, chat_id=store.latest_chat(project))
    llm.add_tool_calls([("finish", {"success": True, "summary": "ok"})])
    resumed.run("add a title")
    assert "created notes.md" in llm.requests[1]["messages"][1]["content"]
    assert resumed.chat_id == first.chat_id
