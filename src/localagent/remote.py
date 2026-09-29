"""Other machines, reached over ssh from the app: log in, keep the tunnel up, open their UI.

The system's own ``ssh`` does the work, so everything in ``~/.ssh/config`` applies
(ProxyJump, keys, host aliases) exactly as in a terminal. What changes is where its
questions go: with ``SSH_ASKPASS_REQUIRE=force`` ssh hands every prompt -- a password,
a new host key -- to ``la-askpass``, which asks this app over loopback and passes the
answer back. VS Code's Remote-SSH works the same way.

One ssh process per machine carries all of its port forwards and, when the machine
runs LocalAgent too, the command that starts that UI and prints its address. One
connection means one login: with ProxyJump that is one prompt for the jump host and
one for the target, never more.

A machine can also carry **named commands** (``commands:`` in remotes.yaml), the things
you would otherwise ssh in to type: starting or stopping its services, say. ``run()``
executes one of them in its own short ssh and brings back what it printed. Only names
already in remotes.yaml can be run -- what arrives over the API is the name, never a
command line -- so the page can trigger what the user wrote there and nothing else.

What keeps a password safe (docs/architecture.md, invariant 28):

* host keys are always checked: no ``StrictHostKeyChecking=no``, no throwaway
  known_hosts. A changed key stops ssh before any password is asked for.
* a password lives only in memory, between the user typing it and ``la-askpass``
  collecting it; it is never written to the database, a log or the event backlog.
* ``la-askpass`` must present a one-time secret that only the ssh process we started
  carries, so another program on this computer cannot collect a password.
* forwarded ports listen on 127.0.0.1 only.

What keeps the traffic on the user's own services (invariant 41): on a shared machine anyone
logged in may listen on its 127.0.0.1, and a port the user's service is not holding at that
moment (after a reboot, while it is stopped) is free for them to take. A forward into it would
hand them every request -- the code, the conversation, the API key -- and let their answers
steer tools running on this computer. So the session checks, before the UI command and every
``WATCH_EVERY`` seconds after, that whoever listens on a forwarded port there is the login user;
anyone else and the connection is closed and not retried.
"""

import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from localagent.config import home_dir
from localagent.procutil import kill_tree, new_group
from localagent.skills import user_skills_dir
from localagent.skillsync import SyncError, fingerprint, origin_name, push

Publish = Callable[[dict[str, Any]], None]

READY = "LA_REMOTE_READY"
UI_ADDRESS = re.compile(r"http://127\.0\.0\.1:(\d+)/#token=[^\s]+")
PROMPT_TIMEOUT = 600.0
RUN_TIMEOUT = 300.0  # a named command that has not finished by then is killed
RUN_OUTPUT_CHARS = 8000
MAX_COMMANDS = 20
COMMAND_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
RETRY_DELAYS = (2, 5, 15, 30, 60)  # after a connection that had worked drops
STDERR_LINES = 20
SLEEP_FOREVER = "exec sleep 2147483647"  # keeps the session (and its forwards) open after the UI command
WATCH_EVERY = 2  # seconds between two looks at who listens on the forwarded ports there
PORTS_OK = "LA_PORTS_OK"
PORTS_FOREIGN = "LA_PORTS_FOREIGN"  # followed by "<port in hex>:<uid>" for each listener that is not the user's
PORTS_UNCHECKED = "LA_PORTS_UNCHECKED"  # no /proc/net/tcp there (not Linux): nothing to check with
SKILL_CHECK = 5.0  # how often a connected machine's copy of the skills is compared with this computer's
SKILL_RETRY = 60.0  # after a sync failed


@dataclass(frozen=True)
class Forward:
    """``127.0.0.1:<local>`` here reaches ``127.0.0.1:<remote>`` on the machine."""

    local: int
    remote: int

    @classmethod
    def parse(cls, value: int | str) -> "Forward":
        text = str(value).strip()
        local, _, remote = text.partition(":")
        try:
            forward = cls(int(local), int(remote or local))
        except ValueError:
            raise ValueError(f"a forward is a port or local:remote, not {text!r}") from None
        if not (0 < forward.local < 65536 and 0 < forward.remote < 65536):
            raise ValueError(f"port out of range in {text!r}")
        return forward

    def spec(self) -> str:
        return f"127.0.0.1:{self.local}:127.0.0.1:{self.remote}"

    def label(self) -> str:
        return str(self.local) if self.local == self.remote else f"{self.local}:{self.remote}"


@dataclass
class RemoteSpec:
    """A machine: the ssh host alias, what to forward, and how to start its LocalAgent UI."""

    name: str
    host: str
    forwards: list[Forward] = field(default_factory=list)
    ui: str = ""  # a command on the machine that starts its UI (if needed) and prints the address
    commands: dict[str, str] = field(default_factory=dict)  # name -> shell command, run by name only

    def to_yaml(self) -> dict[str, Any]:
        return {"host": self.host, "forwards": [f.label() for f in self.forwards], "ui": self.ui,
                "commands": dict(self.commands)}


def remotes_path() -> Path:
    return home_dir() / "remotes.yaml"


def load_remotes() -> dict[str, RemoteSpec]:
    path = remotes_path()
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    found = {}
    for name, entry in (data.get("remotes") or {}).items():
        entry = entry or {}
        commands = entry.get("commands") or {}
        found[name] = RemoteSpec(name=name, host=str(entry.get("host") or name),
                                 forwards=[Forward.parse(f) for f in entry.get("forwards") or []],
                                 ui=str(entry.get("ui") or ""),
                                 commands={str(k): str(v) for k, v in dict(commands).items()})
    return found


def save_remotes(remotes: dict[str, RemoteSpec]) -> None:
    path = remotes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"remotes": {name: spec.to_yaml() for name, spec in remotes.items()}}
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def ssh_hosts(config: Path | None = None) -> list[str]:
    """The host aliases in ``~/.ssh/config`` that name one machine (no wildcards)."""
    path = config or Path.home() / ".ssh" / "config"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    hosts: list[str] = []
    for line in lines:
        words = line.strip().split()
        if len(words) >= 2 and words[0].lower() == "host":
            hosts += [w for w in words[1:] if not any(c in w for c in "*?!") and w not in hosts]
    return hosts


def askpass_program() -> Path | None:
    """The ``la-askpass`` launcher installed next to this interpreter (``pip install`` creates it)."""
    folder = Path(sys.executable).parent
    for name in ("la-askpass.exe", "la-askpass") if sys.platform == "win32" else ("la-askpass",):
        if (folder / name).is_file():
            return folder / name
    return None


def port_check(ports: list[int], watch: bool) -> str:
    """A command for the machine that reports who listens on *ports* there, once or (``watch``) until ssh ends.

    It prints ``PORTS_OK``, ``PORTS_FOREIGN`` with each listener whose uid is not the login
    user's (and then exits 1), or ``PORTS_UNCHECKED``. Any address counts, not only 127.0.0.1:
    a listener on 0.0.0.0 receives the forwarded connections just the same. Only ``/proc`` and
    ``sh`` builtins, one line and no single quote, so any login shell can run it; a watch that
    outlives its connection ends at its next line, which cannot be written (``sleep`` stayed behind).
    """
    wanted = " ".join(f"{port:04X}" for port in ports)
    one_look = ('bad=; for f in /proc/net/tcp /proc/net/tcp6; do [ -r $f ] || continue; '
                'while read -r sl addr rem st q tr rt uid rest; do [ "$st" = 0A ] || continue; '
                f'case " {wanted} " in *" ${{addr##*:}} "*) [ "$uid" = "$me" ] || bad="$bad ${{addr##*:}}:$uid";; esac; '
                'done < $f; done; '
                f'if [ -n "$bad" ]; then echo {PORTS_FOREIGN}$bad; exit 1; fi; echo {PORTS_OK} || exit 1')
    then = f"sleep {WATCH_EVERY}" if watch else "break"
    return (f"sh -c 'me=$(id -u); while :; do if [ -r /proc/net/tcp ]; then {one_look}; "
            f"else echo {PORTS_UNCHECKED} || exit 1; fi; {then}; done'")


def foreign_listeners(line: str) -> list[tuple[int, str]]:
    """``(port, uid)`` for each listener a ``PORTS_FOREIGN`` line names."""
    found = []
    for word in line.removeprefix(PORTS_FOREIGN).split():
        port, _, uid = word.partition(":")
        try:
            item = (int(port, 16), uid)
        except ValueError:
            continue
        if item not in found:
            found.append(item)
    return found


def ssh_command(spec: RemoteSpec, ssh: list[str]) -> list[str]:
    """The ssh invocation for *spec*. Host keys are checked as the user's config says; never switched off.

    With forwards, the session checks who listens on them before anything else and keeps
    checking while it lasts (``port_check``).
    """
    args = [*ssh, "-T", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3", "-o", "BatchMode=no"]
    for forward in spec.forwards:
        args += ["-L", forward.spec()]
    ports = sorted({f.remote for f in spec.forwards})
    steps = [port_check(ports, watch=False)] if ports else []
    if spec.ui:
        steps.append(spec.ui)
    if not steps:
        return [*args, "-N", spec.host]
    steps += [f"echo {READY}", f"exec {port_check(ports, watch=True)}" if ports else SLEEP_FOREVER]
    return [*args, spec.host, " && ".join(steps)]


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


@dataclass
class Prompt:
    """Something ssh asked; ``answer`` is ``None`` until answered, and after a cancel."""

    id: str
    remote: str
    text: str
    kind: str  # "secret" (password, passphrase, code) or "confirm" (a yes/no question such as a new host key)
    done: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "remote": self.remote, "prompt": self.text, "kind": self.kind}


def prompt_kind(text: str) -> str:
    return "confirm" if "(yes/no" in text else "secret"


class Tunnel:
    """One ssh connection to a machine, kept up while wanted."""

    def __init__(self, spec: RemoteSpec, manager: "Remotes"):
        self.spec = spec
        self.manager = manager
        self.state = "disconnected"  # connecting | connected | failed | disconnected
        self.detail = ""
        self.ui_url = ""
        self.skills = ""  # how the copy of this computer's skills on the machine stands
        self.refused = False  # closed because someone else listens on a forwarded port there
        self.secret = ""
        self._want = False
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def view(self) -> dict[str, Any]:
        return {"state": self.state, "detail": self.detail, "ui_url": self.ui_url, "skills": self.skills,
                "refused": self.refused}

    def _set(self, state: str, detail: str = "") -> None:
        self.state, self.detail = state, detail
        self.manager.publish({"type": "remote_status", "name": self.spec.name, **self.view()})

    def start(self) -> None:
        with self._lock:
            self._want = True
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._supervise, name=f"ssh-{self.spec.name}", daemon=True)
            self._thread.start()

    def stop(self, detail: str = "") -> None:
        with self._lock:
            self._want = False
            process = self._process
        if process is not None:
            kill_tree(process)
        self.manager.cancel_prompts(self.spec.name)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        self.ui_url = self.skills = ""
        self.refused = False
        self._set("disconnected", detail)

    def _supervise(self) -> None:
        failures = 0
        while self._want:
            connected = self._run_once()
            if not self._want:
                return
            if not connected:
                return  # never got in (wrong password, cancelled, unreachable): wait for the user to retry
            delay = RETRY_DELAYS[min(failures, len(RETRY_DELAYS) - 1)]
            failures += 1
            self._set("connecting", f"connection lost; reconnecting in {delay}s")
            deadline = time.monotonic() + delay
            while self._want and time.monotonic() < deadline:
                time.sleep(0.2)

    def _run_once(self) -> bool:
        """Run ssh until it exits. Returns whether to reconnect: it had connected and was not refused."""
        askpass = askpass_program()
        if askpass is None:
            self._set("failed", "la-askpass is not installed; run setup again (setup.cmd or scripts/setup.sh)")
            return False
        taken = [f.local for f in self.spec.forwards if _port_open(f.local)]
        if taken:
            # Something here already listens there (often a tunnel opened by hand); connecting would look
            # successful while every request went to that other listener.
            self._set("failed", f"port {', '.join(map(str, taken))} is already in use on this computer "
                                "(close the other tunnel first)")
            return False
        self.refused = False
        self.secret = secrets.token_urlsafe(32)
        env = {**os.environ, "SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force",
               "LA_ASKPASS_URL": self.manager.askpass_url, "LA_ASKPASS_SECRET": self.secret}
        self._set("connecting", f"ssh {self.spec.host}")
        stderr: list[str] = []
        ready = threading.Event()
        checked = threading.Event()  # the machine said who listens on the forwarded ports (or that it cannot tell)
        refused: list[str] = []  # why the connection was closed: someone else listens on a forwarded port
        unchecked = False
        waits = bool(self.spec.forwards or self.spec.ui)  # a remote command runs and prints READY
        try:
            process = subprocess.Popen(
                ssh_command(self.spec, self.manager.ssh), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8", errors="replace", **new_group(),
            )
        except OSError as e:
            self._set("failed", f"cannot run ssh: {e}")
            return False
        with self._lock:
            self._process = process

        def read_stdout() -> None:
            nonlocal unchecked
            for line in process.stdout:
                if line.startswith(PORTS_FOREIGN):
                    held = ", ".join(f"port {port} (uid {uid})" for port, uid in foreign_listeners(line))
                    refused.append(f"another user is listening on {held or 'a forwarded port'} there, where your "
                                   "service should be; disconnected so that nothing is sent to it")
                    kill_tree(process)  # here, not in the loop below: every moment the forward stays up counts
                    return
                if line.startswith((PORTS_OK, PORTS_UNCHECKED)):
                    unchecked = line.startswith(PORTS_UNCHECKED)
                    checked.set()
                match = UI_ADDRESS.search(line)
                if match:
                    self.ui_url = self._local_address(match.group(0), int(match.group(1)))
                if READY in line:
                    ready.set()

        def read_stderr() -> None:
            for line in process.stderr:
                if line.strip():
                    stderr.append(line.strip())
                    del stderr[:-STDERR_LINES]

        readers = [threading.Thread(target=f, daemon=True) for f in (read_stdout, read_stderr)]
        for reader in readers:
            reader.start()
        connected = False
        while process.poll() is None and self._want:
            if not connected and (ready.is_set() or not waits) and (checked.is_set() or not self.spec.forwards) and all(
                    _port_open(f.local) for f in self.spec.forwards):
                if self.spec.ui and not self.ui_url:
                    self._set("failed", "the machine's UI command did not print an address")
                    kill_tree(process)
                    break
                connected = True
                self._set("connected", "could not check who listens on its ports there (no /proc/net/tcp)"
                          if unchecked else "")
                if self.spec.ui:
                    threading.Thread(target=self._sync_skills, args=(process,), name=f"skills-{self.spec.name}",
                                     daemon=True).start()
            time.sleep(0.3)
        kill_tree(process)
        for reader in readers:
            reader.join(timeout=2)
        with self._lock:
            self._process = None
        self.secret = ""  # la-askpass calls from the finished process are refused from here on
        self.manager.cancel_prompts(self.spec.name)
        if refused:
            # Not retried: reconnecting would open the forwards to the same listener before the next check.
            self.ui_url, self.skills, self.refused = "", "", True
            self._set("failed", refused[0])
            return False
        if self._want and not connected and self.state != "failed":
            self._set("failed", _explain(stderr, process.returncode))
        return connected

    def _sync_skills(self, process: subprocess.Popen) -> None:
        """Keep the machine's copy of this computer's skills current while this connection lasts."""
        last, retry_at = None, 0.0
        while process.poll() is None and self._want:
            try:
                current = fingerprint(user_skills_dir())
            except OSError:
                current = last
            if current != last and time.monotonic() >= retry_at:
                try:
                    count = push(self.ui_url, user_skills_dir(), origin_name())
                    last = current
                    self.skills = f"{count} skill{'' if count == 1 else 's'} copied" if count else ""
                except (SyncError, OSError) as e:
                    retry_at = time.monotonic() + SKILL_RETRY
                    self.skills = f"skills not copied: {e}"
                if process.poll() is None and self._want:
                    self.manager.publish({"type": "remote_status", "name": self.spec.name, **self.view()})
            deadline = time.monotonic() + SKILL_CHECK
            while process.poll() is None and self._want and time.monotonic() < deadline:
                time.sleep(0.2)

    def _local_address(self, url: str, remote_port: int) -> str:
        forward = next((f for f in self.spec.forwards if f.remote == remote_port), None)
        if forward is None:
            self.detail = f"the UI listens on port {remote_port} there, which is not forwarded"
            return ""
        return url.replace(f"127.0.0.1:{remote_port}/", f"127.0.0.1:{forward.local}/", 1)


def _explain(stderr: list[str], code: int | None) -> str:
    """The line of ssh's output that says what went wrong, not its banner."""
    for line in reversed(stderr):
        if any(key in line for key in ("Permission denied", "Host key verification failed", "HOST IDENTIFICATION",
                                       "Could not resolve", "timed out", "refused", "port forwarding failed",
                                       "Address already in use", "cannot listen")):
            return line
    return stderr[-1] if stderr else f"ssh exited with code {code}"


class Remotes:
    """The machines of ``remotes.yaml``, their tunnels, and the questions their ssh is asking."""

    def __init__(self, publish: Publish, ssh: list[str] | None = None):
        self.publish = publish
        self.ssh = ssh or [shutil.which("ssh") or "ssh"]
        self.askpass_url = ""
        self._tunnels: dict[str, Tunnel] = {}
        self._run_secrets: dict[str, str] = {}  # secret -> machine, for the ssh of a named command
        self._runs: dict[str, subprocess.Popen] = {}  # machine -> the ssh of its running command
        self._prompts: dict[str, Prompt] = {}
        self._lock = threading.Lock()

    def view(self) -> dict[str, Any]:
        specs = load_remotes()
        with self._lock:
            tunnels = dict(self._tunnels)
            prompts = [p.view() for p in self._prompts.values()]
        machines = []
        for name, spec in specs.items():
            tunnel = tunnels.get(name)
            machines.append({"name": name, "host": spec.host, "forwards": [f.label() for f in spec.forwards],
                             "commands": dict(spec.commands),
                             "ui": spec.ui, **(tunnel.view() if tunnel else {"state": "disconnected", "detail": "",
                                                                               "ui_url": "", "skills": "",
                                                                               "refused": False})})
        return {"remotes": machines, "prompts": prompts, "ssh_hosts": ssh_hosts(),
                "askpass_installed": askpass_program() is not None}

    def save(self, name: str, host: str, forwards: list[int | str], ui: str,
             commands: dict[str, str] | None = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise ValueError("a machine name may use letters, digits, '.', '_' and '-'")
        if not host.strip() or host.strip().startswith("-"):
            raise ValueError("the ssh host must be an alias or user@host")
        named = {str(k).strip(): str(v).strip() for k, v in (commands or {}).items() if str(v).strip()}
        if len(named) > MAX_COMMANDS:
            raise ValueError(f"at most {MAX_COMMANDS} commands per machine")
        for command_name in named:
            if not COMMAND_NAME.fullmatch(command_name):
                raise ValueError(f"{command_name!r} is not a command name (letters, digits, '.', '_', '-')")
        spec = RemoteSpec(name=name, host=host.strip(), forwards=[Forward.parse(f) for f in forwards], ui=ui.strip(),
                          commands=named)
        if not spec.forwards and not spec.ui and not spec.commands:
            raise ValueError("give the machine a port to forward, a UI command, or a named command, "
                             "or there is nothing it can do")
        if spec.ui and not spec.forwards:
            raise ValueError("the machine's UI is only reachable through a forwarded port; add the port it listens on")
        specs = load_remotes()
        specs[name] = spec
        save_remotes(specs)
        with self._lock:
            tunnel = self._tunnels.get(name)
        if tunnel is not None:
            tunnel.spec = spec  # applies from its next connection

    def remove(self, name: str) -> None:
        self.disconnect(name)
        specs = load_remotes()
        specs.pop(name, None)
        save_remotes(specs)

    def connect(self, name: str) -> None:
        specs = load_remotes()
        if name not in specs:
            raise KeyError(name)
        with self._lock:
            tunnel = self._tunnels.get(name)
            if tunnel is None:
                tunnel = self._tunnels[name] = Tunnel(specs[name], self)
            else:
                tunnel.spec = specs[name]
        tunnel.start()

    def disconnect(self, name: str) -> None:
        with self._lock:
            tunnel = self._tunnels.get(name)
        if tunnel is not None:
            tunnel.stop()

    def close_all(self) -> None:
        with self._lock:
            tunnels = list(self._tunnels.values())
        for tunnel in tunnels:
            tunnel.stop()

    def run(self, name: str, command: str, timeout: float = RUN_TIMEOUT) -> dict[str, Any]:
        """Run the machine's named command over its own ssh and return what it printed.

        The name must already be in remotes.yaml: a caller says "stop", never a command
        line, so nothing that reaches this app can invent work for the machine.

        Raises:
            KeyError: no such machine, or no such command on it.
        """
        specs = load_remotes()
        if name not in specs:
            raise KeyError(name)
        spec = specs[name]
        if command not in spec.commands:
            raise KeyError(command)
        line = spec.commands[command]
        askpass = askpass_program()
        env = {**os.environ}
        secret = ""
        if askpass is not None and self.askpass_url:
            # With a UI behind us, ssh asks there; without one (the CLI) it asks on the terminal.
            secret = secrets.token_urlsafe(32)
            env |= {"SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force",
                    "LA_ASKPASS_URL": self.askpass_url, "LA_ASKPASS_SECRET": secret}
            with self._lock:
                self._run_secrets[secret] = name
        args = [*self.ssh, "-T", "-o", "BatchMode=no", spec.host, line]
        started = time.monotonic()
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL if secret else None, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, env=env, text=True, encoding="utf-8",
                                       errors="replace", **new_group())
        except OSError as e:
            return {"command": command, "line": line, "exit_code": None, "output": f"cannot run ssh: {e}"}
        with self._lock:
            self._runs[name] = process  # so cancelling its password dialog can end it
        try:
            output, _ = process.communicate(timeout=timeout)
            code = process.returncode
        except subprocess.TimeoutExpired:
            kill_tree(process)
            output, code = f"no answer within {timeout:.0f}s; ssh was stopped", None
        finally:
            with self._lock:
                self._run_secrets.pop(secret, None)
                self._runs.pop(name, None)
            self.cancel_prompts(name)
        if len(output) > RUN_OUTPUT_CHARS:
            half = RUN_OUTPUT_CHARS // 2
            elided = f"[... {len(output) - RUN_OUTPUT_CHARS} characters ...]"
            output = "\n".join([output[:half], elided, output[-half:]])
        return {"command": command, "line": line, "exit_code": code, "output": output.strip(),
                "elapsed": round(time.monotonic() - started, 1)}

    # ----- prompts ------------------------------------------------------------

    def ask(self, secret: str, text: str) -> str | None:
        """Called for ``la-askpass``: wait for the user's answer. ``None`` means refused or cancelled.

        Raises:
            PermissionError: *secret* belongs to no ssh process of ours.
        """
        with self._lock:
            tunnel = next((t for t in self._tunnels.values()
                           if t.secret and secrets.compare_digest(t.secret, secret)), None)
            owner = tunnel.spec.name if tunnel is not None else next(
                (name for known, name in self._run_secrets.items() if secrets.compare_digest(known, secret)), None)
        if owner is None:
            raise PermissionError("unknown askpass secret")
        prompt = Prompt(id=uuid.uuid4().hex[:12], remote=owner, text=text.strip()[:500],
                        kind=prompt_kind(text))
        with self._lock:
            self._prompts[prompt.id] = prompt
        # Not remembered in the backlog; the page asks /api/remotes after a reload instead.
        self.publish({"type": "ssh_prompt", **prompt.view()})
        prompt.done.wait(PROMPT_TIMEOUT)
        with self._lock:
            self._prompts.pop(prompt.id, None)
        answer, prompt.answer = prompt.answer, None  # the only copy leaves with the return value
        self.publish({"type": "ssh_prompt_resolved", "id": prompt.id, "remote": prompt.remote})
        return answer

    def respond(self, prompt_id: str, answer: str | None) -> bool:
        """Answer a waiting prompt; ``None`` cancels the login.

        A cancel ends ssh *before* la-askpass is released. OpenSSH reads a failed askpass as an
        empty password, sends it, and asks again (three times by default), so merely declining
        would cost failed logins on the machine and bring the dialog straight back.
        """
        with self._lock:
            prompt = self._prompts.get(prompt_id)
            tunnel = self._tunnels.get(prompt.remote) if prompt is not None else None
        if prompt is None:
            return False
        if answer is None:
            # End ssh before la-askpass returns: OpenSSH reads a failed askpass as an empty password,
            # sends it and asks again, which would cost failed logins on the machine.
            with self._lock:
                running = self._runs.get(prompt.remote)
            if tunnel is not None:
                tunnel.stop("login cancelled")
            if running is not None:
                kill_tree(running)
        prompt.answer = answer
        prompt.done.set()
        return True

    def cancel_prompts(self, remote: str) -> None:
        with self._lock:
            waiting = [p for p in self._prompts.values() if p.remote == remote]
        for prompt in waiting:
            prompt.answer = None
            prompt.done.set()
