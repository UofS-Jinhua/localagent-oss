import subprocess

import pytest

from localagent.agent import ChatSession
from localagent.store import Store
from localagent.worktree import git
from localagent.worktree import manager as wm


def sh(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    sh("init", "-q", "-b", "main", cwd=root)
    sh("config", "user.email", "test@example.com", cwd=root)
    sh("config", "user.name", "Test", cwd=root)
    (root / "app.py").write_text("print('v1')\n", encoding="utf-8")
    sh("add", ".", cwd=root)
    sh("commit", "-qm", "init", cwd=root)
    return root


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "chat.db")
    yield s
    s.close()


def test_chat_tasks_accumulate_on_the_chat_branch(llm, registry, repo, store):
    events = []
    session = ChatSession(registry, repo, store=store, on_event=events.append)
    llm.add_tool_calls([("Write", {"file_path": "hello.txt", "content": "hi\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "created hello.txt"})])
    llm.add_text("feat: add hello.txt")  # the commit_message role
    session.run("create hello.txt")

    wt = session.worktree
    assert wt is not None and wt.branch == f"la/{session.chat_id}" and wt.base == "main"
    assert (wt.path / "hello.txt").exists() and not (repo / "hello.txt").exists()
    system = llm.requests[0]["messages"][0]["content"]
    assert f"branch {wt.branch}, created from main" in system and str(wt.path) in system
    assert [e["subject"] for e in events if e["type"] == "task_committed"] == ["feat: add hello.txt"]
    body = git.out("log", "-1", "--format=%B", wt.branch, cwd=repo)
    assert "Task: create hello.txt" in body and "Result: created hello.txt" in body

    # A follow-up task works in the same worktree and sees the first task's file.
    llm.add_tool_calls([("Read", {"file_path": "hello.txt"})])
    llm.add_tool_calls([("Edit", {"file_path": "hello.txt", "old_string": "hi", "new_string": "hello"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "said hello"})])
    llm.add_text("fix: friendlier greeting")
    session.run("make it say hello")
    assert [s for _, s in wm.commits(wt)] == ["feat: add hello.txt", "fix: friendlier greeting"]
    assert store.list_chats(repo)[0].branch == wt.branch

    result = wm.merge(wt, lambda diff: "feat: greeting")
    assert result.status == "merged" and (repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"


def test_plain_commit_messages_skip_the_model(llm, registry, repo, store):
    session = ChatSession(registry, repo, store=store, model_commit_messages=False)
    llm.add_tool_calls([("Write", {"file_path": "a.txt", "content": "a\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "ok"})])
    session.run("add a.txt")
    assert len(llm.requests) == 2
    assert git.out("log", "-1", "--format=%s", session.worktree.branch, cwd=repo) == "la: add a.txt"


def test_no_worktree_and_non_repository_work_directly(llm, registry, repo, store, project):
    session = ChatSession(registry, repo, store=store, isolate=False)
    llm.add_tool_calls([("Write", {"file_path": "direct.txt", "content": "x\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "ok"})])
    session.run("write direct.txt")
    assert session.worktree is None and (repo / "direct.txt").exists() and wm.pending(repo) == []

    events = []
    plain = ChatSession(registry, project, store=store, on_event=events.append)
    llm.add_tool_calls([("finish", {"success": True, "summary": "ok"})])
    plain.run("nothing")
    assert any(e["type"] == "notice" and "not a git repository" in e["text"] for e in events)


def test_commands_in_the_worktree_cannot_touch_the_main_checkout(llm, registry, repo, store):
    session = ChatSession(registry, repo, store=store, model_commit_messages=False)
    target = str(repo).replace("\\", "/") + "/app.py"
    llm.add_tool_calls([("Bash", {"command": f"echo hacked > {target}"})])
    llm.add_tool_calls([("Write", {"file_path": str(repo / "app.py"), "content": "hacked\n"})])
    llm.add_tool_calls([("finish", {"success": False, "summary": "blocked"})])
    events = []
    session.on_event = events.append
    session.run("try to escape")
    results = [e for e in events if e["type"] == "tool_result"]
    assert results[0]["is_error"] and "user's own checkout" in results[0]["output"]
    assert results[1]["is_error"] and "outside the working directory" in results[1]["output"]
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('v1')\n"


def test_attachments_travel_with_the_chat_but_are_never_committed(llm, registry, repo, store, tmp_path):
    from conftest import make_pdf

    from localagent.attachments import AttachmentStore

    attachments = AttachmentStore(tmp_path / "attachments")
    paper = attachments.add(make_pdf(["Method: add a cache"]), "paper.pdf")
    session = ChatSession(registry, repo, store=store, attachment_store=attachments, model_commit_messages=False)
    llm.add_tool_calls([("Write", {"file_path": "cache.py", "content": "CACHE = {}\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "implemented the paper's cache"})])
    session.run("implement the method in the paper", [paper])

    wt = session.worktree
    assert (wt.path / ".la-attachments" / "paper.pdf").is_file()
    opening = llm.requests[0]["messages"][1]["content"]
    assert ".la-attachments/paper.pdf: PDF, 1 page(s); its text is included below" in opening
    assert "Method: add a cache" in opening
    files = git.out("show", "--name-only", "--format=", wt.branch, cwd=repo).split()
    assert files == ["cache.py"]
    assert store.tasks(session.chat_id)[0].attachments[0]["name"] == "paper.pdf"

    wm.keep(wt, lambda diff: "leftovers")  # the worktree folder goes away; the next task restores it
    llm.add_tool_calls([("finish", {"success": True, "summary": "answered"})])
    session.run("which paper did I send?")
    opening = llm.requests[-1]["messages"][1]["content"]
    assert "Attachments: .la-attachments/paper.pdf" in opening and "Method: add a cache" not in opening
    assert (session.worktree.path / ".la-attachments" / "paper.pdf.txt").is_file()
