"""The Files view: recent folders, the project tree with git status, file contents and diffs."""

import subprocess

import pytest
from fastapi.testclient import TestClient

from localagent.config import scratch_dir
from localagent.server import create_app
from localagent.store import Store

TOKEN = "files-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000")  # enough to be served


@pytest.fixture
def client(tmp_path):
    store = Store(tmp_path / "files.db")
    with TestClient(create_app(TOKEN, store=store, static_dir=None)) as c:
        yield c
    store.close()


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A committed project, then: one file edited, one deleted, one added, one ignored, one untracked folder."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    (root / ".gitignore").write_bytes(b"build/\n")
    (root / "app.py").write_bytes(b"x = 1\ny = 2\n")
    (root / "old.txt").write_bytes(b"bye\n")
    (root / "pkg" / "mod.py").write_bytes(b"a = 1\n")
    (root / "gone").mkdir()
    (root / "gone" / "only.txt").write_bytes(b"only\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    (root / "app.py").write_bytes(b"x = 1\ny = 3\nz = 4\n")
    (root / "old.txt").unlink()
    (root / "gone" / "only.txt").unlink()
    (root / "gone").rmdir()
    (root / "数据.md").write_bytes("# 新文件\n".encode())
    (root / "build").mkdir()
    (root / "build" / "out.bin").write_bytes(b"\x00\x01\x02")
    (root / "venv").mkdir()
    (root / "venv" / "lib.py").write_bytes(b"")
    (root / "logo.png").write_bytes(PNG)
    return root


def test_recent_folders(client, project, tmp_path):
    assert client.get("/api/recent").status_code == 401
    assert client.post("/api/recent", json={"path": str(tmp_path / "missing")}, headers=AUTH).status_code == 404
    other = tmp_path / "other"
    other.mkdir()
    client.post("/api/recent", json={"path": str(project)}, headers=AUTH)
    recent = client.post("/api/recent", json={"path": str(other)}, headers=AUTH).json()
    assert recent["folders"] == [str(other.resolve()), str(project.resolve())]
    assert recent["scratch"] == str(scratch_dir())

    # The scratch folder has its own entry in the menu, and a folder that is gone is not offered.
    client.post("/api/recent", json={"path": str(scratch_dir())}, headers=AUTH)
    other.rmdir()
    assert client.get("/api/recent", headers=AUTH).json()["folders"] == [str(project.resolve())]
    assert client.get("/api/fs", headers=AUTH).json()["recent"] == [str(project.resolve())]
    assert client.delete("/api/recent", params={"path": str(project)}, headers=AUTH).json()["folders"] == []


def test_tree_shows_git_status_ignored_and_deleted_entries(client, repo):
    listing = client.get("/api/tree", params={"work_dir": str(repo)}, headers=AUTH).json()
    entries = {e["name"]: e for e in listing["entries"]}
    assert listing["git"] and listing["label"] == "main"
    # Folders first, then files; deleted entries (no longer on disk) come last.
    assert list(entries) == ["build", "pkg", "venv", ".gitignore", "app.py", "logo.png", "数据.md", "gone", "old.txt"]
    assert entries["app.py"]["status"] == "M" and not entries["app.py"]["dir"]
    assert entries["数据.md"]["status"] == "U"
    assert entries["venv"]["status"] == "U" and entries["venv"]["dir"]  # a whole untracked folder
    assert entries["build"]["ignored"] and entries["build"]["status"] == ""
    assert entries["pkg"]["status"] == ""
    assert entries["old.txt"]["deleted"] and entries["old.txt"]["status"] == "D"
    assert entries["gone"]["deleted"] and entries["gone"]["dir"]  # its only file was deleted
    assert entries[".gitignore"]["status"] == "" and not entries[".gitignore"]["ignored"]

    (repo / "pkg" / "mod.py").write_bytes(b"a = 2\n")
    again = client.get("/api/tree", params={"work_dir": str(repo)}, headers=AUTH).json()["entries"]
    assert next(e for e in again if e["name"] == "pkg")["status"] == "M"  # a folder with a change inside
    inner = client.get("/api/tree", params={"work_dir": str(repo), "path": "pkg"}, headers=AUTH).json()
    assert [(e["path"], e["status"]) for e in inner["entries"]] == [("pkg/mod.py", "M")]

    changes = client.get("/api/tree/changes", params={"work_dir": str(repo)}, headers=AUTH).json()
    assert {(f["path"], f["status"]) for f in changes["files"]} == {
        ("app.py", "M"), ("old.txt", "D"), ("gone/only.txt", "D"), ("pkg/mod.py", "M"),
        ("数据.md", "U"), ("venv/", "U"), ("logo.png", "U"),
    }


def test_file_contents_and_diffs(client, repo):
    def get(path, **params):
        return client.get("/api/file", params={"work_dir": str(repo), "path": path, **params}, headers=AUTH)

    edited = get("app.py").json()
    assert (edited["kind"], edited["status"], edited["text"]) == ("text", "M", "x = 1\ny = 3\nz = 4\n")
    assert "-y = 2" in edited["diff"] and "+z = 4" in edited["diff"]
    assert (edited["base_text"], edited["base_truncated"]) == ("x = 1\ny = 2\n", False)  # the side-by-side diff's left

    new = get("数据.md").json()
    assert new["status"] == "U" and new["text"] == "# 新文件\n" and "+# 新文件" in new["diff"]
    assert new["base_text"] == ""

    deleted = get("old.txt").json()
    assert (deleted["kind"], deleted["status"]) == ("missing", "D") and "-bye" in deleted["diff"]
    assert deleted["base_text"] == "bye\n"

    # A file in another encoding reads the same before and after (GBK here, as a Chinese Windows writes it).
    (repo / "pkg" / "cn.txt").write_bytes("旧的\n".encode("gbk"))
    git(repo, "add", "pkg/cn.txt")
    git(repo, "commit", "-qm", "gbk")
    (repo / "pkg" / "cn.txt").write_bytes("新的\n".encode("gbk"))
    gbk = client.get("/api/file", params={"work_dir": str(repo / "pkg"), "path": "cn.txt"}, headers=AUTH).json()
    assert (gbk["base_text"], gbk["text"]) == ("旧的\n", "新的\n")  # a subfolder as the root: paths stay relative to it

    assert get("build/out.bin").json()["kind"] == "binary"
    assert get("app.py").json()["path"] == "app.py"
    assert get("pkg/../app.py").json()["path"] == "app.py"
    assert get("../outside.txt").status_code == 400
    assert get("pkg").status_code == 400
    assert get("nothing.txt").status_code == 404

    assert get("logo.png").json()["kind"] == "image"
    raw = client.get("/api/file/raw", params={"work_dir": str(repo), "path": "logo.png"}, headers=AUTH)
    assert raw.content == PNG and raw.headers["content-type"] == "image/png"
    assert raw.headers["content-security-policy"] == "sandbox"  # an SVG opened from here runs no script


def test_a_folder_outside_git_has_files_but_no_status(client, project):
    (project / "notes.txt").write_bytes(b"hello\n")
    listing = client.get("/api/tree", params={"work_dir": str(project)}, headers=AUTH).json()
    assert not listing["git"] and [(e["name"], e["status"]) for e in listing["entries"]] == [("notes.txt", "")]
    file = client.get("/api/file", params={"work_dir": str(project), "path": "notes.txt"}, headers=AUTH).json()
    assert (file["text"], file["diff"], file["git"]) == ("hello\n", "", False)


def test_a_chats_tree_shows_what_the_chat_changed(tmp_path, llm, registry, repo):
    registry.save()
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "start clean")
    llm.add_tool_calls([("Write", {"file_path": "b.py", "content": "y = 2\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "added b.py"})])
    store = Store(tmp_path / "chat.db")
    with TestClient(create_app(TOKEN, store=store, static_dir=None)) as client:
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            ws.receive_json()
            ws.send_json({"type": "run", "work_dir": str(repo), "task": "add b.py", "plain_commits": True})
            while (event := ws.receive_json())["type"] != "accepted":
                pass
            chat_id = event["chat_id"]
            while ws.receive_json()["type"] != "task_finished":
                pass

        chat = {"chat_id": chat_id}
        changes = client.get("/api/tree/changes", params=chat, headers=AUTH).json()
        assert changes["label"] == f"la/{chat_id}"
        assert [(f["path"], f["status"]) for f in changes["files"]] == [("b.py", "A")]  # committed on the branch
        file = client.get("/api/file", params={**chat, "path": "b.py"}, headers=AUTH).json()
        assert file["status"] == "A" and "+y = 2" in file["diff"]
        # The user's checkout does not have it until the chat is merged.
        checkout = client.get("/api/tree", params={"work_dir": str(repo)}, headers=AUTH).json()
        assert "b.py" not in [e["name"] for e in checkout["entries"]]
        assert client.get("/api/tree", params={"chat_id": "nope"}, headers=AUTH).status_code == 404
    store.close()


def test_a_tree_needs_a_folder_or_a_chat(client):
    assert client.get("/api/tree", headers=AUTH).status_code == 400
