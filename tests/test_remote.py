"""Logging in to another machine from the app: ssh's questions answered in the page, the tunnel kept up.

The whole path is real: a uvicorn server, the installed ``la-askpass`` launcher that
ssh runs, and a stand-in ssh (``fake_ssh.py``) that asks through ``SSH_ASKPASS`` the way
OpenSSH does and then really forwards ports to a server this test runs.
"""

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from localagent import remote
from localagent.fsutil import remove_tree
from localagent.remote import Forward, RemoteSpec, askpass_program, ssh_command, ssh_hosts
from localagent.server.desktop import start_server
from localagent.skills import synced_skills_dir, user_skills_dir
from localagent.skillsync import origin_name

FAKE_SSH = [sys.executable, str(Path(__file__).with_name("fake_ssh.py"))]
TOKEN = "t0ken"
PASSWORDS = [["alice@jump.example.edu's password: ", "jump-pass-1"],
             ["alice@gpu2.example.edu's password: ", "gpu-pass-2"]]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(check, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("timed out waiting")


@pytest.fixture
def remote_service():
    """What sits behind the tunnel on the other machine: a small HTTP server."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"hello from the other machine"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture
def app(isolated_home, tmp_path, monkeypatch):
    if askpass_program() is None:
        pytest.fail("la-askpass is not installed in this environment: run `pip install -e .` again")
    port = free_port()
    server, thread = start_server("127.0.0.1", port, TOKEN)
    server.config.app.state.remotes.ssh = FAKE_SSH
    monkeypatch.setenv("FAKE_SSH_RUNS", str(tmp_path / "runs"))
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {TOKEN}"},
                          timeout=30, trust_env=False)
    yield client
    client.close()
    server.should_exit = True
    thread.join(15)


def add_gpu2(app, remote_service, monkeypatch, forwards=None, ui="bin/la-ui"):
    local = free_port()
    monkeypatch.setenv("FAKE_SSH_TARGETS", json.dumps({"18080": remote_service, "8765": remote_service}))
    monkeypatch.setenv("FAKE_SSH_OUTPUT", "http://127.0.0.1:8765/#token=remote-ui-token&dir=/srv/alice")
    response = app.put("/api/remotes/gpu2", json={"host": "gpu2", "forwards": forwards or [f"{local}:8765"], "ui": ui})
    assert response.status_code == 200, response.text
    return local


def state(app, name="gpu2"):
    return next(r for r in app.get("/api/remotes").json()["remotes"] if r["name"] == name)


def answer_prompts(app, answers):
    """Answer each prompt as it appears, the way the page's dialog does."""
    seen = []
    for answer in answers:
        prompt = wait_for(lambda: next((p for p in app.get("/api/remotes").json()["prompts"]
                                        if p["id"] not in seen), None))
        seen.append(prompt["id"])
        body = {"cancel": True} if answer is None else {"answer": answer}
        assert app.post(f"/api/ssh-prompts/{prompt['id']}", json=body).status_code == 200
    return seen


def test_one_connection_asks_for_the_jump_host_and_the_machine_then_forwards(
        app, remote_service, monkeypatch, isolated_home):
    monkeypatch.setenv("FAKE_SSH_PROMPTS", json.dumps(PASSWORDS))
    local = add_gpu2(app, remote_service, monkeypatch)

    assert app.post("/api/remotes/gpu2/connect").status_code == 200
    prompts = wait_for(lambda: app.get("/api/remotes").json()["prompts"])
    assert prompts[0]["prompt"] == PASSWORDS[0][0].strip() and prompts[0]["kind"] == "secret"
    answer_prompts(app, ["jump-pass-1", "gpu-pass-2"])

    connected = wait_for(lambda: (s := state(app))["state"] == "connected" and s)
    # The machine's UI address, rewritten to the port it is forwarded to here.
    assert connected["ui_url"] == f"http://127.0.0.1:{local}/#token=remote-ui-token&dir=/srv/alice"
    assert httpx.get(f"http://127.0.0.1:{local}/", trust_env=False).text == "hello from the other machine"

    # The passwords went from the page to ssh and nowhere else.
    stored = b"".join(p.read_bytes() for p in Path(isolated_home).rglob("*") if p.is_file())
    for _, password in PASSWORDS:
        assert password.encode() not in stored
        assert password not in app.get("/api/remotes").text

    assert app.post("/api/remotes/gpu2/disconnect").json()["remotes"][0]["state"] == "disconnected"
    wait_for(lambda: not _port_listens(local))


def _port_listens(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def test_while_connected_the_machine_keeps_a_copy_of_the_skills_written_here(app, monkeypatch, isolated_home):
    """The machine is a second LocalAgent server with its own token; the copy follows adds and deletes here."""
    monkeypatch.setattr(remote, "SKILL_CHECK", 0.3)
    user = user_skills_dir()
    (user / "paper-review").mkdir(parents=True)
    (user / "paper-review" / "SKILL.md").write_text("---\nname: paper-review\ndescription: Review a paper.\n---\n"
                                                    "Read the method section first.\n", encoding="utf-8")
    (user / "paper-review" / "scripts").mkdir()
    (user / "paper-review" / "scripts" / "cite.sh").write_text("#!/bin/sh\necho cite\n", encoding="utf-8")

    machine_port = free_port()
    machine, machine_thread = start_server("127.0.0.1", machine_port, "machine-token")
    try:
        monkeypatch.setenv("FAKE_SSH_TARGETS", json.dumps({"8765": machine_port}))
        monkeypatch.setenv("FAKE_SSH_OUTPUT", "http://127.0.0.1:8765/#token=machine-token&dir=/srv/alice")
        local = free_port()
        assert app.put("/api/remotes/gpu2", json={"host": "gpu2", "forwards": [f"{local}:8765"],
                                                   "ui": "bin/la-ui"}).status_code == 200
        app.post("/api/remotes/gpu2/connect")

        wait_for(lambda: state(app)["skills"] == "1 skill copied")
        copy = synced_skills_dir() / origin_name() / "paper-review"
        assert "Read the method section first." in (copy / "SKILL.md").read_text(encoding="utf-8")
        assert (copy / "scripts" / "cite.sh").is_file()

        (user / "latex").mkdir()
        (user / "latex" / "SKILL.md").write_text("How we write LaTeX.\n", encoding="utf-8")
        wait_for(lambda: state(app)["skills"] == "2 skills copied")
        remove_tree(user / "paper-review")
        wait_for(lambda: not copy.exists())
        assert (synced_skills_dir() / origin_name() / "latex" / "SKILL.md").is_file()

        # The machine refuses anyone without its own token, including this computer's.
        refused = httpx.put(f"http://127.0.0.1:{local}/api/skills/synced", params={"origin": "x"}, content=b"",
                            headers={"Authorization": f"Bearer {TOKEN}"}, trust_env=False)
        assert refused.status_code == 401
        app.post("/api/remotes/gpu2/disconnect")
    finally:
        machine.should_exit = True
        machine_thread.join(15)


def add_commands(app, commands, host="gpu2"):
    assert app.put("/api/remotes/gpu2", json={"host": host, "forwards": [], "ui": "",
                                               "commands": commands}).status_code == 200


def test_a_machine_runs_the_commands_it_was_given_a_name_for_and_no_others(app, monkeypatch):
    """The page asks for "start"; what start means is written in remotes.yaml, on this computer."""
    add_commands(app, {"start": "bin/la-services start", "stop": "bin/la-services stop"})

    ran = app.post("/api/remotes/gpu2/run", json={"command": "start"}, timeout=60).json()

    assert ran["exit_code"] == 0 and ran["output"] == "ran: bin/la-services start"
    assert ran["command"] == "start" and ran["line"] == "bin/la-services start"
    # A command line, rather than one of the names, is not something a machine will run.
    assert app.post("/api/remotes/gpu2/run", json={"command": "rm -rf /u2"}).status_code == 404
    assert app.post("/api/remotes/nowhere/run", json={"command": "start"}).status_code == 404
    assert state(app)["commands"] == {"start": "bin/la-services start", "stop": "bin/la-services stop"}


def test_a_command_that_fails_says_so(app, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_EXIT", "3")
    add_commands(app, {"status": "bin/la-services status"})

    ran = app.post("/api/remotes/gpu2/run", json={"command": "status"}, timeout=60).json()

    assert ran["exit_code"] == 3 and "ran: bin/la-services status" in ran["output"]


def test_a_command_asks_for_the_password_in_the_page_like_a_connection_does(app, monkeypatch, isolated_home):
    monkeypatch.setenv("FAKE_SSH_PROMPTS", json.dumps(PASSWORDS[:1]))
    add_commands(app, {"start": "bin/la-services start"})

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(lambda: app.post("/api/remotes/gpu2/run", json={"command": "start"}, timeout=60))
        answer_prompts(app, [PASSWORDS[0][1]])
        ran = running.result().json()

    assert ran["exit_code"] == 0 and ran["output"] == "ran: bin/la-services start"
    stored = b"".join(p.read_bytes() for p in Path(isolated_home).rglob("*") if p.is_file())
    assert PASSWORDS[0][1].encode() not in stored  # the password went to ssh and nowhere else


def test_a_wrong_password_fails_with_what_ssh_said_and_does_not_ask_again(app, remote_service, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_PROMPTS", json.dumps(PASSWORDS))
    add_gpu2(app, remote_service, monkeypatch)

    app.post("/api/remotes/gpu2/connect")
    answer_prompts(app, ["not-the-password"])

    failed = wait_for(lambda: (s := state(app))["state"] == "failed" and s)
    assert "Permission denied" in failed["detail"]
    time.sleep(3)  # no retry loop that keeps asking for a password
    assert state(app)["state"] == "failed" and not app.get("/api/remotes").json()["prompts"]


def test_cancelling_the_dialog_ends_ssh_before_it_can_send_anything(app, remote_service, monkeypatch, tmp_path):
    """OpenSSH sends an empty password when askpass fails, then asks again: a cancel must not get that far."""
    monkeypatch.setenv("FAKE_SSH_PROMPTS", json.dumps(PASSWORDS))
    add_gpu2(app, remote_service, monkeypatch)

    app.post("/api/remotes/gpu2/connect")
    answer_prompts(app, [None])

    stopped = wait_for(lambda: (s := state(app))["state"] == "disconnected" and s)
    assert stopped["detail"] == "login cancelled"
    time.sleep(2)
    assert not (tmp_path / "runs.sent").exists()  # ssh was gone before la-askpass came back
    assert not app.get("/api/remotes").json()["prompts"]


def test_a_dropped_connection_comes_back_without_asking_again(app, remote_service, monkeypatch):
    """After one password login the key works again, so a reconnect needs nobody at the keyboard."""
    monkeypatch.setenv("FAKE_SSH_PROMPTS", json.dumps(PASSWORDS))
    monkeypatch.setenv("FAKE_SSH_DROP_AFTER", "1.5")
    local = add_gpu2(app, remote_service, monkeypatch)

    app.post("/api/remotes/gpu2/connect")
    answer_prompts(app, ["jump-pass-1", "gpu-pass-2"])
    wait_for(lambda: state(app)["state"] == "connected")
    wait_for(lambda: state(app)["state"] == "connecting")  # the drop
    wait_for(lambda: state(app)["state"] == "connected", timeout=30)
    assert httpx.get(f"http://127.0.0.1:{local}/", trust_env=False).status_code == 200
    app.post("/api/remotes/gpu2/disconnect")


def test_a_port_already_taken_here_is_reported_before_ssh_runs(app, remote_service, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_SSH_PROMPTS", json.dumps(PASSWORDS))
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        add_gpu2(app, remote_service, monkeypatch, forwards=[f"{port}:8765"])

        app.post("/api/remotes/gpu2/connect")
        failed = wait_for(lambda: (s := state(app))["state"] == "failed" and s)

    assert "already in use" in failed["detail"]
    assert not (tmp_path / "runs").exists()  # ssh never started, so it never asked for a password


FOREIGN = "LA_PORTS_FOREIGN 46A0:1234"  # the machine's check: uid 1234, not the user, listens on 18080


def test_a_port_someone_else_listens_on_there_is_never_connected_to(app, remote_service, monkeypatch, tmp_path):
    """On a shared machine anyone may listen on 127.0.0.1, say while the user's model server is down."""
    monkeypatch.setenv("FAKE_SSH_PORTS", json.dumps([FOREIGN]))
    ui, model = free_port(), free_port()
    add_gpu2(app, remote_service, monkeypatch, forwards=[f"{ui}:8765", f"{model}:18080"])

    app.post("/api/remotes/gpu2/connect")
    failed = wait_for(lambda: (s := state(app))["state"] == "failed" and s)

    assert failed["refused"] and "port 18080 (uid 1234)" in failed["detail"]
    assert not failed["ui_url"]  # the UI command never ran: the check comes first
    time.sleep(3)  # past the first retry delay
    assert (tmp_path / "runs").read_text() == "1" and state(app)["state"] == "failed"
    assert not _port_listens(ui) and not _port_listens(model)
    assert not app.post("/api/remotes/gpu2/disconnect").json()["remotes"][0]["refused"]


def test_a_connection_closes_when_someone_else_takes_a_forwarded_port_later(
        app, remote_service, monkeypatch, tmp_path):
    """The model server there stops, another user starts listening on its port: the forward must not stay."""
    monkeypatch.setenv("FAKE_SSH_PORTS", json.dumps(["LA_PORTS_OK"] * 4 + [FOREIGN]))
    ui, model = free_port(), free_port()
    add_gpu2(app, remote_service, monkeypatch, forwards=[f"{ui}:8765", f"{model}:18080"])

    app.post("/api/remotes/gpu2/connect")
    wait_for(lambda: state(app)["state"] == "connected")
    assert httpx.get(f"http://127.0.0.1:{model}/", trust_env=False).status_code == 200
    failed = wait_for(lambda: (s := state(app))["state"] == "failed" and s)

    assert failed["refused"] and "port 18080 (uid 1234)" in failed["detail"]
    assert not _port_listens(model) and not _port_listens(ui)
    time.sleep(3)
    assert (tmp_path / "runs").read_text() == "1"  # not reconnected: that would reach the same listener


needs_proc = pytest.mark.skipif(not Path("/proc/net/tcp").is_file() or shutil.which("sh") is None,
                                reason="the check reads Linux's /proc/net/tcp")


@needs_proc
def test_the_port_check_tells_the_users_own_listener_from_anyone_elses(tmp_path):
    """The real check, run by sh as the machine's login shell would, against a real listener."""
    fake_id = tmp_path / "id"  # to the check, an `id` answering another uid makes this listener someone else's
    fake_id.write_text(f"#!/bin/sh\necho {os.getuid() + 1}\n", encoding="utf-8")
    fake_id.chmod(0o755)
    as_other = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    with socket.socket() as mine:
        mine.bind(("127.0.0.1", 0))
        mine.listen()
        port = mine.getsockname()[1]
        check = ["sh", "-c", remote.port_check([port], watch=False)]

        own = subprocess.run(check, capture_output=True, text=True, timeout=30)
        other = subprocess.run(check, capture_output=True, text=True, timeout=30, env=as_other)

    assert (own.returncode, own.stdout.strip()) == (0, "LA_PORTS_OK")
    assert other.returncode == 1 and remote.foreign_listeners(other.stdout.strip()) == [(port, str(os.getuid()))]
    nobody = subprocess.run(check, capture_output=True, text=True, timeout=30, env=as_other)
    assert nobody.stdout.strip() == "LA_PORTS_OK"  # nothing listening is nothing to refuse


@needs_proc
def test_the_watch_ends_with_its_connection():
    """``sleep`` outlived every session; the watch stops at the first line nobody reads."""
    watch = subprocess.Popen(["sh", "-c", remote.port_check([free_port()], watch=True)],
                             stdout=subprocess.PIPE, text=True)
    assert watch.stdout.readline().strip() == "LA_PORTS_OK"
    watch.stdout.close()
    assert watch.wait(timeout=remote.WATCH_EVERY + 10) != 0


def test_askpass_answers_only_the_ssh_the_app_started(app):
    """Another program on this computer cannot collect a password by calling the endpoint."""
    response = app.post("/api/askpass", json={"prompt": "password: "}, headers={"X-LA-Askpass": "guessed"})
    assert response.status_code == 403


def test_la_askpass_refuses_to_answer_anywhere_but_this_app():
    program = str(askpass_program())
    bare = subprocess.run([program, "password: "], capture_output=True, env={"PATH": ""})
    assert bare.returncode == 1 and bare.stdout == b""
    elsewhere = subprocess.run([program, "password: "], capture_output=True,
                               env={"LA_ASKPASS_URL": "http://example.com/api/askpass", "LA_ASKPASS_SECRET": "x"})
    assert elsewhere.returncode == 1 and elsewhere.stdout == b""


def test_the_ssh_command_keeps_host_key_checks_and_listens_only_here():
    spec = RemoteSpec("gpu2", "gpu2", [Forward.parse(18080), Forward.parse("18765:8765")], "/srv/alice/bin/la-ui")
    command = ssh_command(spec, ["ssh"])
    text = " ".join(command)
    assert "StrictHostKeyChecking" not in text and "UserKnownHostsFile" not in text
    assert "-L 127.0.0.1:18080:127.0.0.1:18080" in text and "-L 127.0.0.1:18765:127.0.0.1:8765" in text
    assert command[-2] == "gpu2"
    # Who listens on the forwarded ports there is checked before the UI command, then watched.
    check, ui, ready, watch = command[-1].split(" && ")
    assert check.startswith("sh -c ") and '" 223D 46A0 "' in check and "break" in check
    assert ui == "/srv/alice/bin/la-ui" and ready == "echo LA_REMOTE_READY"
    assert watch.startswith("exec sh -c ") and f"sleep {remote.WATCH_EVERY}" in watch
    assert ssh_command(RemoteSpec("m", "m", [Forward.parse(22)]), ["ssh"])[-1].startswith('sh -c ')
    assert ssh_command(RemoteSpec("m", "m"), ["ssh"])[-2:] == ["-N", "m"]


def test_machine_settings_are_checked(app):
    assert app.put("/api/remotes/x", json={"host": "gpu2", "forwards": []}).status_code == 400
    assert app.put("/api/remotes/x", json={"host": "gpu2", "forwards": [], "ui": "la-ui"}).status_code == 400
    assert app.put("/api/remotes/x", json={"host": "-oProxyCommand=evil", "forwards": [22]}).status_code == 400
    assert app.put("/api/remotes/bad name", json={"host": "gpu2", "forwards": [22]}).status_code == 400
    assert app.put("/api/remotes/x", json={"host": "gpu2", "forwards": ["70000"]}).status_code == 400


def test_ssh_hosts_lists_the_aliases_in_the_config(tmp_path):
    config = tmp_path / "config"
    config.write_text("Host jump\n  HostName t\nHost gpu1 gpu2\n  ProxyJump jump\nHost *\n  User me\n"
                      "Host !bad *.edu\n", encoding="utf-8")
    assert ssh_hosts(config) == ["jump", "gpu1", "gpu2"]
