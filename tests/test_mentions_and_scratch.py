"""Chats without a project (the scratch folder) and the file list behind @-mentions."""

import subprocess

from fastapi.testclient import TestClient

from localagent.agent import ChatSession
from localagent.config import is_scratch, scratch_dir
from localagent.server import create_app
from localagent.store import Store

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_the_scratch_folder_is_never_a_project(isolated_home, tmp_path):
    scratch = scratch_dir()
    assert scratch.is_dir() and is_scratch(scratch) and is_scratch(scratch / "deep" / "path")
    assert not is_scratch(tmp_path)


def test_a_scratch_chat_works_in_place_even_inside_a_repository(llm, registry, isolated_home, tmp_path):
    """The home folder may itself be a git repo; a scratch chat must never make a worktree of it."""
    scratch = scratch_dir()
    subprocess.run(["git", "init", "-q"], cwd=isolated_home, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=isolated_home, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=isolated_home, check=True)
    (isolated_home / "seed.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=isolated_home, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=isolated_home, check=True)

    store = Store(tmp_path / "scratch.db")
    session = ChatSession(registry, scratch, store=store)
    llm.add_tool_calls([("Write", {"file_path": "answer.md", "content": "42\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "wrote answer.md"})])
    result = session.run("work out the answer")
    store.close()

    assert result.success and session.worktree is None  # no branch, no worktree
    assert (scratch / "answer.md").read_text(encoding="utf-8") == "42\n"
    system = llm.requests[0]["messages"][0]["content"]
    assert "scratch folder for quick tasks" in system


def test_the_file_list_for_mentions(llm, registry, tmp_path, project):
    (project / "alpha.py").write_text("a = 1\n", encoding="utf-8")
    (project / "nested").mkdir()
    (project / "nested" / "beta.txt").write_text("b\n", encoding="utf-8")
    registry.save()
    store = Store(tmp_path / "files.db")
    with TestClient(create_app(TOKEN, store=store, static_dir=None)) as client:
        listed = client.get("/api/files", params={"work_dir": str(project)}, headers=AUTH).json()
        assert set(listed["files"]) == {"alpha.py", "nested/beta.txt"} and not listed["truncated"]
        assert client.get("/api/files", params={"work_dir": str(project / "nope")}, headers=AUTH).status_code == 404
        assert client.get("/api/scratch", headers=AUTH).json()["path"].endswith("scratch")
    store.close()
