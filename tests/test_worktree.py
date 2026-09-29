import os
import stat
import subprocess

import pytest

from localagent.fsutil import remove_tree
from localagent.tools.shell import find_bash
from localagent.worktree import git
from localagent.worktree import manager as wm
from localagent.worktree.lock import file_lock


def sh(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def message(text):
    return lambda diff: text


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    sh("init", "-q", "-b", "main", cwd=root)
    sh("config", "user.email", "test@example.com", cwd=root)
    sh("config", "user.name", "Test", cwd=root)
    sh("config", "core.autocrlf", "false", cwd=root)
    (root / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    sh("add", ".", cwd=root)
    sh("commit", "-qm", "init", cwd=root)
    return root


def fix_add(worktree):
    (worktree.path / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")


def test_prepare_creates_and_reuses_the_chat_worktree(repo):
    prepared = wm.prepare(repo, "chat1")
    wt = prepared.worktree
    assert prepared.created and (wt.branch, wt.base) == ("la/chat1", "main") and wt.start == wt.baseline
    assert git.current_branch(wt.path) == "la/chat1" and git.current_branch(repo) == "main"
    assert (wt.path / "app.py").exists() and not git.is_dirty(repo)
    again = wm.prepare(repo, "chat1")
    assert not again.created and again.worktree == wt
    assert "/.la-worktrees/" in (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")


def test_fallbacks(tmp_path, repo):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert wm.prepare(plain, "c").reason == "not a git repository"
    empty = tmp_path / "empty"
    empty.mkdir()
    sh("init", "-q", cwd=empty)
    assert "no commits" in wm.prepare(empty, "c").reason
    assert "no branch named" in wm.prepare(repo, "c", base="nope").reason
    sh("checkout", "-q", "--detach", cwd=repo)
    assert "detached" in wm.prepare(repo, "c").reason
    assert wm.prepare(repo, "c2", base="main").worktree is not None


def test_uncommitted_changes_become_the_baseline(repo):
    (repo / "app.py").write_text("def add(a, b):\n    return a - b  # wip\n", encoding="utf-8")
    (repo / "notes").mkdir()
    (repo / "notes" / "todo.txt").write_text("todo\n", encoding="utf-8")
    (repo / "README.md").unlink()
    (repo / ".venv" / "Lib").mkdir(parents=True)
    (repo / ".venv" / "Lib" / "site.py").write_text("x", encoding="utf-8")
    wt = wm.prepare(repo, "chat1").worktree
    assert wt.carries_user_changes
    assert "# wip" in (wt.path / "app.py").read_text(encoding="utf-8")
    assert (wt.path / "notes" / "todo.txt").exists() and not (wt.path / "README.md").exists()
    assert not (wt.path / ".venv").exists()
    assert (repo / "README.md").exists() is False and (repo / "notes").exists()  # the user's checkout is untouched


def test_commit_changes_leaves_junk_out(repo):
    wt = wm.prepare(repo, "chat1").worktree
    assert wm.commit_changes(wt, message("x")) is None
    fix_add(wt)
    (wt.path / "__pycache__").mkdir()
    (wt.path / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"x")
    (wt.path / "tmp").mkdir()
    (wt.path / "tmp" / "scratch.txt").write_text("x", encoding="utf-8")
    sha, subject = wm.commit_changes(wt, message("fix: add adds"))
    assert subject == "fix: add adds"
    assert git.out("show", "--name-only", "--format=", sha, cwd=repo).splitlines() == ["app.py"]
    assert [s for _, s in wm.commits(wt)] == ["fix: add adds"]
    assert "+    return a + b" in wm.diff(wt)


def make_nul(directory):
    """A file named nul, made the way tasks made it: a CMD-style redirect run by bash (Git Bash on Windows)."""
    subprocess.run([find_bash(), "-c", "ls no-such-file 2>nul"], cwd=directory, capture_output=True, check=False)
    assert "nul" in os.listdir(directory)


def test_a_file_named_nul_neither_breaks_the_commit_nor_outlives_the_worktree(repo):
    """git for Windows cannot read such a file ("short read while indexing nul"), so `add -A` failed outright."""
    wt = wm.prepare(repo, "chat1").worktree
    fix_add(wt)
    make_nul(wt.path)
    sha, _ = wm.commit_changes(wt, message("fix: add adds"))
    assert git.out("show", "--name-only", "--format=", sha, cwd=repo).splitlines() == ["app.py"]
    assert wm.discard(wt) == [] and not wt.path.exists()  # Windows cannot delete it through an ordinary path


def test_merge_squashes_onto_the_base_and_cleans_up(repo):
    wt = wm.prepare(repo, "chat1").worktree
    fix_add(wt)
    wm.commit_changes(wt, message("fix: first task"))
    (wt.path / "new.py").write_text("x = 1\n", encoding="utf-8")
    result = wm.merge(wt, message("fix: add adds, plus new.py"))  # the second task was never committed
    assert result.status == "merged", result.message
    assert "return a + b" in (repo / "app.py").read_text(encoding="utf-8") and (repo / "new.py").exists()
    assert git.out("log", "--format=%s", cwd=repo).splitlines() == ["fix: add adds, plus new.py", "init"]
    assert not git.branch_exists(repo, "la/chat1") and not wt.path.exists() and wm.pending(repo) == []


def test_merge_keeps_the_users_uncommitted_work_out_of_the_commit(repo):
    (repo / "README.md").write_text("hello\nuser wip\n", encoding="utf-8")
    wt = wm.prepare(repo, "chat1").worktree
    fix_add(wt)
    wm.commit_changes(wt, message("fix: add"))
    result = wm.merge(wt, message("fix: add"))
    assert result.status == "merged" and not result.notes
    assert git.out("show", "--name-only", "--format=", "HEAD", cwd=repo).splitlines() == ["app.py"]
    assert (repo / "README.md").read_text(encoding="utf-8") == "hello\nuser wip\n"


def test_merge_into_a_branch_that_is_not_checked_out(repo):
    sh("branch", "develop", cwd=repo)
    wt = wm.prepare(repo, "chat1", base="develop").worktree
    fix_add(wt)
    result = wm.merge(wt, message("feat: on develop"))
    assert result.status == "merged"
    assert git.current_branch(repo) == "main" and "a - b" in (repo / "app.py").read_text(encoding="utf-8")
    assert git.out("log", "-1", "--format=%s", "develop", cwd=repo) == "feat: on develop"
    assert not (repo / ".la-worktrees" / "_merge-chat1").exists()


def test_conflict_changes_nothing(repo):
    wt = wm.prepare(repo, "chat1").worktree
    (wt.path / "app.py").write_text("def add(a, b):\n    return a + b  # agent\n", encoding="utf-8")
    wm.commit_changes(wt, message("fix: agent"))
    (repo / "app.py").write_text("def add(a, b):\n    return b + a  # user\n", encoding="utf-8")
    sh("commit", "-qam", "user change", cwd=repo)
    result = wm.merge(wt, message("fix"))
    assert result.status == "conflict" and "git merge --squash la/chat1" in result.message
    assert git.branch_exists(repo, "la/chat1") and wt.path.exists()
    assert (repo / "app.py").read_text(encoding="utf-8").endswith("# user\n") and not git.is_dirty(repo)


def test_nothing_to_merge(repo):
    wt = wm.prepare(repo, "chat1").worktree
    assert wm.merge(wt, message("x")).status == "nothing"
    assert not git.branch_exists(repo, "la/chat1")


def test_discard(repo):
    wt = wm.prepare(repo, "chat1").worktree
    fix_add(wt)
    wm.commit_changes(wt, message("fix"))
    (wt.path / "readonly.txt").write_text("x", encoding="utf-8")
    (wt.path / "readonly.txt").chmod(stat.S_IREAD)
    assert wm.discard(wt) == []
    assert not git.branch_exists(repo, "la/chat1") and not wt.path.exists()
    assert "a - b" in (repo / "app.py").read_text(encoding="utf-8")


def test_keep_then_resume(repo):
    wt = wm.prepare(repo, "chat1").worktree
    fix_add(wt)
    committed, notes = wm.keep(wt, message("wip: keep"))
    assert committed is not None and notes == []
    assert not wt.path.exists() and git.branch_exists(repo, "la/chat1")
    again = wm.prepare(repo, "chat1")
    assert again.restored and "a + b" in (again.worktree.path / "app.py").read_text(encoding="utf-8")


def test_repo_status(repo):
    wt = wm.prepare(repo, "chat1").worktree
    fix_add(wt)
    wm.commit_changes(wt, message("fix"))
    (repo / "scratch.txt").write_text("1", encoding="utf-8")
    status = wm.repo_status(repo)
    assert (status.branch, status.dirty_files) == ("main", 1)
    [summary] = status.pending
    assert (summary.branch, summary.base, summary.commits, summary.files, summary.checked_out) == (
        "la/chat1", "main", 1, 1, True)
    assert wm.repo_status(repo.parent).repo is None


def test_work_dir_in_a_subdirectory(repo):
    (repo / "pkg").mkdir()
    (repo / "pkg" / "m.py").write_text("x = 1\n", encoding="utf-8")
    sh("add", ".", cwd=repo)
    sh("commit", "-qm", "pkg", cwd=repo)
    wt = wm.prepare(repo / "pkg", "chat1").worktree
    assert wt.subdir == "pkg" and (wt.work_dir / "m.py").exists()


def test_remove_tree_handles_git_objects(repo):
    # git's object files are read-only; a plain rmtree fails on them on Windows
    assert any(not p.stat().st_mode & stat.S_IWRITE for p in (repo / ".git" / "objects").rglob("*") if p.is_file())
    assert remove_tree(repo) and not repo.exists()
    assert remove_tree(repo)  # already gone


def test_lock_is_exclusive(tmp_path):
    path = tmp_path / "repo.lock"
    with file_lock(path):
        with pytest.raises(TimeoutError):
            with file_lock(path, timeout=0.3):
                pass


def test_merge_reports_git_failures_instead_of_raising(repo, llm, registry, tmp_path):
    """A base branch that disappeared must give a readable failure, not a traceback (the UI shows it)."""
    from localagent.agent import ChatSession

    sh("branch", "feature", cwd=repo)
    from localagent.store import Store

    store = Store(tmp_path / "merge.db")
    session = ChatSession(registry, repo, store=store, base="feature", model_commit_messages=False)
    llm.add_tool_calls([("Write", {"file_path": "new.py", "content": "x = 1\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "added new.py"})])
    session.run("add new.py")
    sh("branch", "-D", "feature", cwd=repo)  # the user deleted it while the chat was open

    result = wm.merge(session.worktree, lambda diff: "merge it")
    assert result.status == "failed" and "feature" in result.message
    assert session.worktree.branch in git.local_branches(repo)  # nothing was thrown away
    store.close()
