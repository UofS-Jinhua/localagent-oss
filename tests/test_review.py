"""The reviewer pass: one model writes, a second reads the diff before the commit."""

import subprocess

import pytest

from localagent.agent import ChatSession
from localagent.store import Store
from localagent.tools import Workspace, make_tools
from localagent.worktree import git, working_diff


def sh(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    sh("init", "-q", "-b", "main", cwd=root)
    sh("config", "user.email", "t@e.com", cwd=root)
    sh("config", "user.name", "T", cwd=root)
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    sh("add", ".", cwd=root)
    sh("commit", "-qm", "init", cwd=root)
    return root


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "review.db")
    yield s
    s.close()


def session(registry, repo, store, **kwargs):
    return ChatSession(registry, repo, store=store, model_commit_messages=False, review=True, **kwargs)


def test_the_reviewer_cannot_change_anything(project):
    """It reads and runs tests; giving it Write or Bash would make it a second author."""
    tools = make_tools(Workspace(project), read_only=True)
    assert [t.name for t in tools] == ["Read", "RunTests", "Grep", "Glob"]
    # ...and RunTests must not be a way back to a shell (a live reviewer used it to run git and mkdir).
    run_tests = next(t for t in tools if t.name == "RunTests")
    assert "command" not in run_tests.parameters["properties"]


def test_a_clean_review_leaves_the_work_alone(llm, registry, repo, store):
    llm.add_tool_calls([("Write", {"file_path": "util.py", "content": "def double(x):\n    return x * 2\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "added double()"})])
    llm.add_tool_calls([("Read", {"file_path": "util.py"})])  # the reviewer looks
    llm.add_tool_calls([("finish", {"success": True, "summary": "Checked double(): correct and tested."})])

    events = []
    chat = session(registry, repo, store, on_event=events.append)
    result = chat.run("add a double() helper")

    review = next(e for e in events if e["type"] == "review")
    assert review["clean"] and result.summary == "added double()"
    assert "util.py" in git.out("show", "--name-only", "--format=", chat.worktree.branch, cwd=repo)
    # The reviewer saw the uncommitted work, not an empty diff.
    reviewed_task = llm.requests[2]["messages"][1]["content"]
    assert "def double" in reviewed_task and "# The task it was given" in reviewed_task


def test_findings_come_back_as_a_fix_pass_in_the_same_commit(llm, registry, repo, store):
    llm.add_tool_calls([("Write", {"file_path": "util.py", "content": "def half(x):\n    return x / 0\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "added half()"})])
    llm.add_tool_calls([("finish", {"success": False, "summary": "1. util.py divides by zero; it should divide by 2."})])
    llm.add_tool_calls([("Read", {"file_path": "util.py"})])  # the fix pass reads before editing, as the rules require
    llm.add_tool_calls([("Edit", {"file_path": "util.py", "old_string": "x / 0", "new_string": "x / 2"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "fixed the divisor"})])

    events = []
    chat = session(registry, repo, store, on_event=events.append)
    result = chat.run("add a half() helper")

    assert not next(e for e in events if e["type"] == "review")["clean"]
    fix_task = llm.requests[3]["messages"][1]["content"]  # the fix pass's opening message
    assert "divides by zero" in fix_task and "# The original task" in fix_task
    assert "added half()" in result.summary and "reviewed this and found" in result.summary
    assert "fixed the divisor" in result.summary
    assert (chat.worktree.path / "util.py").read_text(encoding="utf-8").endswith("x / 2\n")
    # One commit for the task, already containing the fix.
    assert len(git.out("log", "--format=%h", f"main..{chat.worktree.branch}", cwd=repo).split()) == 1


def test_the_working_diff_shows_new_files(llm, registry, repo, store):
    chat = ChatSession(registry, repo, store=store, model_commit_messages=False)
    llm.add_tool_calls([("Write", {"file_path": "fresh.py", "content": "x = 1\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "added fresh.py"})])
    chat.run("add fresh.py")
    (chat.worktree.path / "extra.py").write_text("y = 2\n", encoding="utf-8")  # untracked, like a new file mid-task
    diff = working_diff(chat.worktree)
    assert "extra.py" in diff and "y = 2" in diff


def test_the_reviewer_runs_the_tests_itself(llm, registry, repo, store):
    """A summary claiming "all tests pass" is not evidence; the reviewer has to see the output."""
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test_add():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    sh("add", ".", cwd=repo)
    sh("commit", "-qm", "tests", cwd=repo)
    llm.add_tool_calls([("Write", {"file_path": "util.py", "content": "def double(x):\n    return x * 2\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "added double(); all tests pass"})])
    llm.add_tool_calls([("RunTests", {})])  # the reviewer checks for itself
    llm.add_tool_calls([("finish", {"success": True, "summary": "ran the tests: 1 passed"})])

    result = session(registry, repo, store).run("add double()")

    assert result.success
    review_prompt = llm.requests[-2]["messages"][-1]["content"]
    assert "Run the tests with RunTests" in review_prompt
    assert "asserts on the stub" in review_prompt  # it must judge whether the tests prove anything
    # It saw a real run of the real command with its real exit code (this project has no venv with
    # pytest in it, so the verdict is FAILED -- what matters is that the reviewer read the output).
    ran = [r for r in llm.requests[-1]["messages"]
           if r["role"] == "tool" and "python -m pytest" in str(r["content"])]
    assert ran and "exit code" in str(ran[0]["content"])
