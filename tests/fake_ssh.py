"""A stand-in for ``ssh`` that behaves like it where LocalAgent can tell.

It asks its questions through ``$SSH_ASKPASS`` exactly as OpenSSH does with
``SSH_ASKPASS_REQUIRE=force`` (the prompt as the only argument, the answer on
stdout), refuses a wrong answer with ssh's own message, then really listens on
every ``-L 127.0.0.1:<local>:127.0.0.1:<remote>`` and relays connections to a real
server the test runs. A remote command is "run" by printing what the test says it
prints. Tests set it up through the environment:

* ``FAKE_SSH_PROMPTS``: JSON ``[[prompt, expected answer], ...]`` for the first login
* ``FAKE_SSH_TARGETS``: JSON ``{"<remote port>": <real port on this computer>}``
* ``FAKE_SSH_OUTPUT``: what the remote command prints
* ``FAKE_SSH_DROP_AFTER``: seconds after which the first connection drops
* ``FAKE_SSH_RUNS``: a file counting the runs (later runs log in without prompts, like a key would)
* ``FAKE_SSH_EXIT``: what a one-off command (not the UI command) exits with; it prints ``ran: <command>``
* ``FAKE_SSH_PORTS``: JSON list of what the machine's port check reports (default all ``LA_PORTS_OK``):
  the first before the UI command, then one per watch pass (``FAKE_SSH_WATCH_EVERY`` seconds apart),
  the last one repeated. A ``LA_PORTS_FOREIGN`` report ends the session as the real check does.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


def relay(client: socket.socket, port: int) -> None:
    try:
        upstream = socket.create_connection(("127.0.0.1", port))
    except OSError:
        client.close()
        return

    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
    pump(upstream, client)


def listen(local: int, target: int) -> None:
    server = socket.socket()
    if os.name != "nt":
        # Like OpenSSH's listeners: after a drop, connections of the last run still in TIME_WAIT must not
        # keep the reconnect from listening again. (On Windows the option would let two listeners share a port.)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", local))
    server.listen()
    while True:
        client, _ = server.accept()
        threading.Thread(target=relay, args=(client, target), daemon=True).start()


def main() -> int:
    args = sys.argv[1:]
    forwards: list[tuple[int, int]] = []
    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in ("-o", "-L"):
            if args[i] == "-L":
                _, local, _, remote = args[i + 1].split(":")
                forwards.append((int(local), int(remote)))
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            positional = args[i:]
            break
    host, command = positional[0], " ".join(positional[1:])

    runs_file = Path(os.environ["FAKE_SSH_RUNS"])
    run = int(runs_file.read_text() or 0) + 1 if runs_file.exists() else 1
    runs_file.write_text(str(run))

    if run == 1:
        for prompt, expected in json.loads(os.environ.get("FAKE_SSH_PROMPTS", "[]")):
            asked = subprocess.run([os.environ["SSH_ASKPASS"], prompt], capture_output=True)
            answer = asked.stdout.decode("utf-8").removesuffix("\n")
            # Like OpenSSH: whatever came back (an empty string after a failed askpass) would be sent now.
            Path(str(runs_file) + ".sent").write_text("sent")
            if asked.returncode != 0 or answer != expected:
                print(f"alice@{host}: Permission denied (publickey,password).", file=sys.stderr, flush=True)
                return 255

    targets = json.loads(os.environ.get("FAKE_SSH_TARGETS", "{}"))
    for local, remote in forwards:
        threading.Thread(target=listen, args=(local, targets.get(str(remote), remote)), daemon=True).start()

    if command and "echo LA_REMOTE_READY" not in command:
        # A one-off command (Remotes.run): report it the way the machine's shell would, then leave.
        print(f"ran: {command}", flush=True)
        return int(os.environ.get("FAKE_SSH_EXIT", "0"))
    reports = json.loads(os.environ.get("FAKE_SSH_PORTS", '["LA_PORTS_OK"]')) if "LA_PORTS_" in command else []
    if reports:
        print(reports[0], flush=True)
        if reports[0].startswith("LA_PORTS_FOREIGN"):
            return 1  # the check fails, so the && chain stops before the UI command
    if command:
        print(os.environ.get("FAKE_SSH_OUTPUT", ""), flush=True)
        print("LA_REMOTE_READY", flush=True)
    drop_after = float(os.environ.get("FAKE_SSH_DROP_AFTER", "0"))
    watch_every = float(os.environ.get("FAKE_SSH_WATCH_EVERY", "0.5"))
    started = next_watch = time.monotonic()
    passes = 0
    while True:
        time.sleep(0.2)
        if run == 1 and drop_after and time.monotonic() - started > drop_after:
            print("Connection to host closed by remote host.", file=sys.stderr, flush=True)
            return 255
        if reports and time.monotonic() >= next_watch + watch_every:
            next_watch, passes = time.monotonic(), passes + 1
            report = reports[min(passes, len(reports) - 1)]
            print(report, flush=True)
            if report.startswith("LA_PORTS_FOREIGN"):
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
