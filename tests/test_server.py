import subprocess

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from localagent.server import create_app
from localagent.store import Store

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(llm, registry, tmp_path):
    registry.save()  # into the isolated LOCALAGENT_HOME, where the server loads it from
    store = Store(tmp_path / "ui.db")
    with TestClient(create_app(TOKEN, store=store, static_dir=None)) as c:
        yield c
    store.close()


def connect(client):
    ws = client.websocket_connect(f"/ws?token={TOKEN}")
    return ws


def wait_for(ws, kind, limit=300):
    seen = []
    for _ in range(limit):
        event = ws.receive_json()
        seen.append(event)
        if event["type"] == kind:
            return event, seen
    raise AssertionError(f"no {kind} event in {[e['type'] for e in seen]}")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"), ("config", "user.name", "T")):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


def test_serves_the_built_ui(llm, registry, tmp_path):
    store = Store(tmp_path / "static.db")
    with TestClient(create_app(TOKEN, store=store)) as c:
        page = c.get("/")
        assert page.status_code == 200 and '<div id="root">' in page.text
    with TestClient(create_app(TOKEN, store=store, static_dir=tmp_path / "missing")) as c:
        assert "has not been built" in c.get("/").text
    store.close()


def test_token_is_required(client):
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/state", headers=AUTH).json()["busy"] is False
    assert client.get(f"/api/state?token={TOKEN}").status_code == 200
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=wrong") as ws:
            ws.receive_json()


def test_models_and_folders(client, project):
    assert [m["name"] for m in client.get("/api/models", headers=AUTH).json()["models"]] == ["test"]
    listing = client.get("/api/fs", params={"path": str(project.parent)}, headers=AUTH).json()
    assert "project" in [e["name"] for e in listing["entries"]]
    assert client.get("/api/fs", headers=AUTH).json()["entries"]  # home and drives
    assert client.get("/api/fs", params={"path": str(project / "missing")}, headers=AUTH).status_code == 404


def test_run_streams_events_and_persists_the_chat(client, llm, project):
    llm.add_tool_calls([("Write", {"file_path": "hello.txt", "content": "hi\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "created hello.txt"})])
    with connect(client) as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "run", "work_dir": str(project), "task": "create hello.txt"})
        finished, seen = wait_for(ws, "task_finished")
    types = [e["type"] for e in seen]
    assert types[0] == "accepted" or "accepted" in types
    assert {"task_started", "tool_call", "tool_result", "finish"} <= set(types)
    assert finished["success"] and (project / "hello.txt").exists()
    [chat] = client.get("/api/chats", params={"work_dir": str(project)}, headers=AUTH).json()
    detail = client.get(f"/api/chats/{chat['id']}", headers=AUTH).json()
    assert detail["tasks"][0]["result"] == "created hello.txt"
    assert "tool_result" in [e["type"] for e in detail["tasks"][0]["events"]]


def test_bad_requests_are_reported(client, project):
    with connect(client) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "work_dir": str(project / "nope"), "task": "x"})
        assert "not a directory" in ws.receive_json()["message"]
        ws.send_json({"type": "run", "task": "missing work_dir"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "dance"})
        assert "unknown command" in ws.receive_json()["message"]


def test_approval_busy_steer_and_late_clients(client, llm, project):
    llm.add_tool_calls([("Bash", {"command": "git push origin main"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "respected the refusal"})])
    with connect(client) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "work_dir": str(project), "task": "publish"})
        request, _ = wait_for(ws, "approval_request")
        assert request["command"] == "git push origin main"
        # While the task waits, a second run is refused and a new client catches up from the backlog.
        ws.send_json({"type": "run", "work_dir": str(project), "task": "another"})
        assert "already running" in wait_for(ws, "error")[0]["message"]
        with connect(client) as late:
            hello = late.receive_json()
            assert hello["state"]["busy"] and "approval_request" in [e["type"] for e in hello["backlog"]]
        ws.send_json({"type": "steer", "text": "do not push, just report"})
        ws.send_json({"type": "approval", "id": request["id"], "allow": False})
        wait_for(ws, "task_finished")
    denied = llm.requests[1]["messages"]
    assert any("did not approve" in m.get("content", "") for m in denied if m["role"] == "tool")
    assert "do not push, just report" in denied[-1]["content"]


def test_stop_ends_the_task(client, llm, project):
    llm.add_tool_calls([("Bash", {"command": "git push"})])
    with connect(client) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "work_dir": str(project), "task": "push"})
        wait_for(ws, "approval_request")
        ws.send_json({"type": "stop"})
        finished, _ = wait_for(ws, "task_finished")
    assert finished["reason"] == "stopped" and not finished["success"]


def test_branch_review_and_merge(client, llm, repo):
    llm.add_tool_calls([("Write", {"file_path": "b.py", "content": "y = 2\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "added b.py"})])
    with connect(client) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "work_dir": str(repo), "task": "add b.py", "plain_commits": True})
        chat_id = wait_for(ws, "accepted")[0]["chat_id"]
        _, seen = wait_for(ws, "task_finished")
        repo_event, _ = wait_for(ws, "repo")
    worktree_event = next(e for e in seen if e["type"] == "worktree")
    assert worktree_event["branch"] == f"la/{chat_id}" and worktree_event["base"] == "main"
    assert repo_event["branch"] == "main" and repo_event["pending"][0]["branch"] == f"la/{chat_id}"

    changes = client.get(f"/api/chats/{chat_id}/changes", headers=AUTH).json()
    assert changes["branch"] == f"la/{chat_id}" and "+y = 2" in changes["diff"]
    assert [c[1] for c in changes["commits"]] == ["la: add b.py"]
    status = client.get("/api/repo", params={"work_dir": str(repo)}, headers=AUTH).json()
    assert status["branch"] == "main" and "main" in status["branches"]

    result = client.post(f"/api/chats/{chat_id}/merge?plain=true", headers=AUTH).json()
    assert result["status"] == "merged" and (repo / "b.py").exists()
    assert client.get(f"/api/chats/{chat_id}/changes", headers=AUTH).json()["branch"] is None
    assert client.post(f"/api/chats/{chat_id}/discard", headers=AUTH).status_code == 404


def test_attachments_upload_run_and_steer(client, llm, project):
    from localagent.models.openai_compat import IMAGE_OMITTED
    from localagent.models.probe import solid_png

    png = solid_png(4, 4, (0, 255, 0))
    shot = client.post("/api/attachments", params={"name": "shot.png"}, content=png,
                       headers={**AUTH, "Content-Type": "image/png"}).json()
    assert shot["kind"] == "image" and shot["size"] == len(png)
    fetched = client.get(f"/api/attachments/{shot['id']}", headers=AUTH)
    assert fetched.content == png and fetched.headers["content-type"] == "image/png"
    assert client.get("/api/attachments/" + "0" * 24, headers=AUTH).status_code == 404
    assert client.post("/api/attachments", params={"name": "x.txt"}, content=b"x").status_code == 401
    note = client.post("/api/attachments", params={"name": "note.md"}, content=b"Also check the footer.\n",
                       headers=AUTH).json()

    llm.add_tool_calls([("Bash", {"command": "git push"})])  # waits for approval, so there is time to steer
    llm.add_tool_calls([("finish", {"success": True, "summary": "reviewed"})])
    with connect(client) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "work_dir": str(project), "task": "review the screenshot",
                      "attachments": [shot["id"]]})
        request, seen = wait_for(ws, "approval_request")
        ws.send_json({"type": "steer", "text": "x", "attachments": ["bogus"]})
        assert "unknown attachment" in wait_for(ws, "error")[0]["message"]
        ws.send_json({"type": "steer", "text": "and this note", "attachments": [note["id"]]})
        ws.send_json({"type": "approval", "id": request["id"], "allow": False})
        wait_for(ws, "task_finished")
    opening = llm.requests[0]["messages"][1]["content"]
    assert "[image .la-attachments/shot.png]" in opening and IMAGE_OMITTED in opening  # "test" has no vision
    assert "Also check the footer." in llm.requests[1]["messages"][-1]["content"]
    assert next(e for e in seen if e["type"] == "task_start")["attachments"][0]["name"] == "shot.png"
    [chat] = client.get("/api/chats", params={"work_dir": str(project)}, headers=AUTH).json()
    task = client.get(f"/api/chats/{chat['id']}", headers=AUTH).json()["tasks"][0]
    assert [a["name"] for a in task["attachments"]] == ["shot.png", "note.md"]


def test_registry_management(client, llm, isolated_home):
    import json

    view = client.get("/api/registry", headers=AUTH).json()
    assert [p["name"] for p in view["providers"]] == ["scripted"] and "gemini" in [p["id"] for p in view["presets"]]
    assert client.put("/api/providers/bad name", json={"base_url": llm.base_url}, headers=AUTH).status_code == 400

    view = client.put("/api/providers/cloudy", json={"base_url": llm.base_url, "api_key": "sk-secret-123",
                                                     "max_concurrency": 1, "local": False}, headers=AUTH).json()
    cloudy = next(p for p in view["providers"] if p["name"] == "cloudy")
    assert cloudy["has_key"] and cloudy["api_key_env"] == "LA_CLOUDY_API_KEY" and not cloudy["local"]
    assert "sk-secret-123" not in json.dumps(view)
    assert "sk-secret-123" not in (isolated_home / "models.yaml").read_text(encoding="utf-8")
    assert "LA_CLOUDY_API_KEY=sk-secret-123" in (isolated_home / "secrets.env").read_text(encoding="utf-8")

    found = client.get("/api/providers/cloudy/discover", headers=AUTH).json()["models"]
    assert found[0]["model_id"] == "test-model" and found[0]["registered"] is False
    view = client.put("/api/models/cloud-test", json={"provider": "cloudy", "model_id": "test-model",
                                                       "vision": True, "switchable": True}, headers=AUTH).json()
    assert next(m for m in view["models"] if m["name"] == "cloud-test")["vision"]
    assert client.put("/api/models/x", json={"provider": "nope"}, headers=AUTH).status_code == 400
    assert client.put("/api/models/x", json={"bogus": 1}, headers=AUTH).status_code == 400
    assert client.get("/api/models", headers=AUTH).json()["models"][1]["switchable"] is True

    view = client.put("/api/roles", json={"commit_message": {"model": "cloud-test", "thinking": False}},
                      headers=AUTH).json()
    assert view["roles"]["commit_message"] == {"model": "cloud-test", "thinking": False}
    assert client.delete("/api/providers/cloudy", headers=AUTH).status_code == 409  # a role uses its model
    assert client.put("/api/roles", json={"main": None}, headers=AUTH).status_code == 400
    client.put("/api/roles", json={"commit_message": None}, headers=AUTH)

    llm.add_tool_calls([("Read", {"file_path": "src/app.py"})])
    llm.add_text("Red.")
    results = client.post("/api/models/cloud-test/test", headers=AUTH).json()["results"]
    assert [(r["name"], r["ok"]) for r in results] == [("tool call", True), ("vision", True)]
    assert llm.headers[-1]["Authorization"] == "Bearer sk-secret-123"
    assert llm.requests[-1]["messages"][0]["content"][1]["type"] == "image_url"

    assert client.delete("/api/providers/cloudy?forget_key=true", headers=AUTH).status_code == 200
    assert "LA_CLOUDY_API_KEY" not in (isolated_home / "secrets.env").read_text(encoding="utf-8")
    assert [m["name"] for m in client.get("/api/registry", headers=AUTH).json()["models"]] == ["test"]


def test_models_payload_marks_what_the_server_has_loaded(client, llm, monkeypatch):
    """A one-GPU server swaps models when you pick another one, so the UI shows which is loaded."""
    import localagent.server.app as app_module
    from localagent.models.discovery import DiscoveredModel, forget_loaded

    forget_loaded()
    monkeypatch.setattr(app_module, "loaded_models",
                        lambda provider, timeout=2.0: [DiscoveredModel(id="test-model", loaded=True)])
    assert client.get("/api/models", headers=AUTH).json()["models"][0]["loaded"] is True

    def offline(provider, timeout=2.0):
        raise ConnectionError("server is off")

    monkeypatch.setattr(app_module, "loaded_models", offline)
    assert client.get("/api/models", headers=AUTH).json()["models"][0]["loaded"] is False


def test_the_backlog_for_late_clients_stays_bounded():
    """A long task streams megabytes; the backlog must not hold all of it."""
    from localagent.server.hub import MAX_BACKLOG_EVENTS, MAX_TEXT_CHARS, Hub

    hub = Hub()
    hub.publish({"type": "task_start", "task": "long one"})
    hub.publish({"type": "step_start", "step": 1})
    for index in range(MAX_BACKLOG_EVENTS * 2):
        hub.publish({"type": "tool_call", "id": f"c{index}", "name": "Bash", "args": {}})
        hub.publish({"type": "tool_output", "id": f"c{index}", "text": "x" * 5000})
    _, backlog = hub.subscribe()
    assert len(backlog) <= MAX_BACKLOG_EVENTS
    assert backlog[0]["type"] == "task_start"  # the opening events survive, so a late client knows what runs
    assert max(len(e.get("text", "")) for e in backlog) <= MAX_TEXT_CHARS


def test_a_git_failure_is_reported_not_a_500(client, llm, repo):
    """Branch actions are git; if git refuses, the UI must get the reason."""
    llm.add_tool_calls([("Write", {"file_path": "c.py", "content": "z = 3\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "added c.py"})])
    subprocess.run(["git", "branch", "side"], cwd=repo, check=True)
    with connect(client) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "work_dir": str(repo), "task": "add c.py", "base": "side",
                      "plain_commits": True})
        chat_id = wait_for(ws, "accepted")[0]["chat_id"]
        wait_for(ws, "task_finished")
    subprocess.run(["git", "branch", "-D", "side"], cwd=repo, check=True)

    response = client.post(f"/api/chats/{chat_id}/merge?plain=true", headers=AUTH)
    assert response.status_code == 200  # merge has its own result type for this
    assert response.json()["status"] == "failed" and "side" in response.json()["message"]
    assert client.post(f"/api/chats/{chat_id}/keep", headers=AUTH).status_code in (200, 409)


def test_a_client_that_connects_late_sees_the_pending_question(client, llm, project):
    """Reloading the page while the agent waits must bring the dialog back, not strand the task."""
    llm.add_tool_calls([("AskUser", {"question": "Which file?", "options": ["a.py", "b.py"]})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "used a.py"})])
    with connect(client) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "work_dir": str(project), "task": "change the constant"})
        asked, _ = wait_for(ws, "question_request")
        with connect(client) as reloaded:  # a second client, like a reloaded page
            hello = reloaded.receive_json()
            waiting = [e for e in hello["backlog"] if e["type"] == "question_request"]
            assert waiting and waiting[-1]["id"] == asked["id"]
            assert not [e for e in hello["backlog"] if e["type"] == "question_resolved"]
            reloaded.send_json({"type": "answer", "id": asked["id"], "text": "a.py"})
        wait_for(ws, "task_finished")
    assert "The user answered: a.py" in str(llm.requests[-1]["messages"])


def test_only_the_desktop_apps_own_server_offers_native_dialogs(tmp_path):
    """Another machine's UI can be shown in the desktop window; its folders are not on this computer."""
    from starlette.testclient import TestClient

    from localagent.server import create_app
    from localagent.store import Store

    for desktop in (False, True):
        store = Store(tmp_path / f"state-{desktop}.db")
        with TestClient(create_app("t0ken", store=store, desktop=desktop)) as client:
            assert client.get("/api/state", params={"token": "t0ken"}).json()["native_dialogs"] is desktop
            with client.websocket_connect("/ws?token=t0ken") as socket:
                assert socket.receive_json()["state"]["native_dialogs"] is desktop
        store.close()


def test_settings_survive_a_restart(client, llm, project):
    """The page cannot keep them: the desktop window opens on a new port, and storage follows the port."""
    assert client.get("/api/settings", headers=AUTH).json() == {}
    client.put("/api/settings", json={"thinking": False, f"model:{project}": "qwen"}, headers=AUTH)
    stored = client.put("/api/settings", json={"review": True}, headers=AUTH).json()
    assert stored == {"thinking": False, f"model:{project}": "qwen", "review": True}
    assert client.put("/api/settings", json={"review": None}, headers=AUTH).json().get("review") is None
    assert client.get("/api/settings", headers=AUTH).json()["thinking"] is False


def test_a_running_task_says_when_it_started(client, llm, project):
    """The timer in the page must agree after a reload, so the start time comes from the server."""
    import time

    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])
    before = time.time()
    with connect(client) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "work_dir": str(project), "task": "quick"})
        started, _ = wait_for(ws, "task_started")
        assert before <= started["started"] <= time.time()
        wait_for(ws, "task_finished")


def test_deleting_a_chat_takes_its_transcript_but_guards_its_branch(client, llm, repo):
    """The transcript is cheap to lose; commits on the chat's branch are not, so they need a second yes."""
    llm.add_tool_calls([("Write", {"file_path": "c.py", "content": "z = 3\n"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "added c.py"})])
    with connect(client) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "work_dir": str(repo), "task": "add c.py", "plain_commits": True})
        chat_id = wait_for(ws, "accepted")[0]["chat_id"]
        wait_for(ws, "task_finished")

    refused = client.delete(f"/api/chats/{chat_id}", headers=AUTH)
    assert refused.status_code == 409 and f"la/{chat_id}" in refused.json()["detail"]
    assert client.get(f"/api/chats/{chat_id}", headers=AUTH).status_code == 200  # still there

    deleted = client.delete(f"/api/chats/{chat_id}", params={"discard_branch": "true"}, headers=AUTH).json()
    assert deleted["tasks"] == 1 and deleted["branch"] == f"la/{chat_id}"
    assert client.get(f"/api/chats/{chat_id}", headers=AUTH).status_code == 404
    assert client.get("/api/chats", params={"work_dir": str(repo)}, headers=AUTH).json() == []
    assert client.get("/api/repo", params={"work_dir": str(repo)}, headers=AUTH).json()["pending"] == []
    assert client.delete(f"/api/chats/{chat_id}", headers=AUTH).status_code == 404


def test_a_chat_without_a_branch_is_deleted_at_once(client, llm, project):
    llm.add_tool_calls([("finish", {"success": True, "summary": "nothing to do"})])
    with connect(client) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "work_dir": str(project), "task": "look around"})
        chat_id = wait_for(ws, "accepted")[0]["chat_id"]
        wait_for(ws, "task_finished")

    assert client.delete(f"/api/chats/{chat_id}", headers=AUTH).json()["tasks"] == 1
    assert client.get("/api/chats", params={"work_dir": str(project)}, headers=AUTH).json() == []


def test_chats_of_a_deleted_folder_are_offered_for_deletion_and_only_they_go(client, llm, project, tmp_path):
    """Once a project folder is gone its chats cannot be opened; the page offers them for deletion, never more."""
    from localagent.fsutil import remove_tree

    gone = tmp_path / "old-project"
    gone.mkdir()
    only = client.post("/api/attachments", params={"name": "only.md"}, content=b"used by the old project\n",
                       headers=AUTH).json()
    shared = client.post("/api/attachments", params={"name": "shared.md"}, content=b"used by both\n",
                         headers=AUTH).json()
    chats = {}
    for folder, attached in ((gone, [only["id"], shared["id"]]), (project, [shared["id"]])):
        llm.add_tool_calls([("finish", {"success": True, "summary": "read it"})])
        client.post("/api/recent", json={"path": str(folder)}, headers=AUTH)
        client.put("/api/settings", json={f"model:{folder.resolve()}": "test"}, headers=AUTH)
        with connect(client) as ws:
            ws.receive_json()
            ws.send_json({"type": "run", "work_dir": str(folder), "task": "read the note", "attachments": attached})
            chats[folder] = wait_for(ws, "accepted")[0]["chat_id"]
            wait_for(ws, "task_finished")
    assert client.get("/api/orphaned-chats", headers=AUTH).json() == []  # both folders exist

    assert remove_tree(gone)
    listed = client.get("/api/orphaned-chats", headers=AUTH).json()
    assert [(c["id"], c["work_dir"], c["task_count"]) for c in listed] == [(chats[gone], str(gone.resolve()), 1)]

    # Asking for a chat whose folder exists does nothing to it: the folder is checked again.
    result = client.post("/api/orphaned-chats/delete", json={"ids": [chats[gone], chats[project]]}, headers=AUTH).json()
    assert result == {"chats": 1, "tasks": 1, "attachments": 1}
    assert client.get(f"/api/chats/{chats[gone]}", headers=AUTH).status_code == 404
    assert client.get(f"/api/chats/{chats[project]}", headers=AUTH).status_code == 200
    assert client.get(f"/api/attachments/{only['id']}", headers=AUTH).status_code == 404
    assert client.get(f"/api/attachments/{shared['id']}", headers=AUTH).status_code == 200
    settings = client.get("/api/settings", headers=AUTH).json()
    assert f"model:{gone.resolve()}" not in settings and f"model:{project.resolve()}" in settings
    assert client.get("/api/orphaned-chats", headers=AUTH).json() == []
    # The page filters missing folders out of the recent list anyway; the row itself is gone too.
    stored = Store(tmp_path / "ui.db")
    assert str(gone.resolve()) not in stored.recent_folders() and str(project.resolve()) in stored.recent_folders()
    stored.close()


def test_a_deleted_chat_takes_its_attachment_copies_out_of_the_folder(client, llm, project):
    """Outside a repository the copies sit in the project itself; nothing else would ever remove them."""
    from conftest import make_pdf

    def upload(name, data):
        return client.post("/api/attachments", params={"name": name}, content=data, headers=AUTH).json()["id"]

    def names():
        return {p.name for p in folder.iterdir()}

    folder = project / ".la-attachments"
    only, paper, shared = (upload("only.md", b"just for the first chat\n"),
                           upload("paper.pdf", make_pdf(["A page of the paper"])), upload("shared.md", b"both\n"))
    chats = []
    for attached in ([only, paper, shared], [shared]):
        llm.add_tool_calls([("finish", {"success": True, "summary": "read them"})])
        with connect(client) as ws:
            ws.receive_json()
            ws.send_json({"type": "run", "work_dir": str(project), "task": "read the attachments",
                          "attachments": attached})
            chats.append(wait_for(ws, "accepted")[0]["chat_id"])
            wait_for(ws, "task_finished")
    assert names() == {".gitignore", "only.md", "paper.pdf", "paper.pdf.txt", "shared.md"}

    client.delete(f"/api/chats/{chats[0]}", headers=AUTH)
    assert names() == {".gitignore", "shared.md"}  # the other chat still uses it
    client.delete(f"/api/chats/{chats[1]}", headers=AUTH)
    assert not folder.exists()  # the project is back to how it was


def test_deleting_a_chat_whose_branch_holds_nothing_needs_no_second_yes(client, llm, repo):
    """An empty branch is bookkeeping, not work: asking about it would be noise."""
    llm.add_tool_calls([("finish", {"success": True, "summary": "nothing needed changing"})])
    with connect(client) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "work_dir": str(repo), "task": "have a look", "plain_commits": True})
        chat_id = wait_for(ws, "accepted")[0]["chat_id"]
        wait_for(ws, "task_finished")

    deleted = client.delete(f"/api/chats/{chat_id}", headers=AUTH).json()
    assert deleted["branch"] == f"la/{chat_id}" and deleted["tasks"] == 1
    assert client.get("/api/repo", params={"work_dir": str(repo)}, headers=AUTH).json()["pending"] == []
