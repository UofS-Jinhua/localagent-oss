"""Your skills on the machines you log in to: copied there while the app is connected.

A skill written on this computer should work the same when the task runs on the GPU
server. The app already holds what that takes: the ssh tunnel to the machine and the
address (with its token) of the LocalAgent UI running there. So while a machine is
connected, this computer packs its ``<data>/skills`` into a zip and hands it to that
server, which keeps it under its own ``<data>/synced-skills/<this computer>/`` (``<data>`` is
each side's ``config.home_dir``: ``data/`` in a checkout, or ``$LOCALAGENT_HOME``).

* **One way, this computer to the machine.** The page of another machine never gets this
  computer's token (invariant 28), and for the same reason nothing comes back: skills are
  instructions the model follows and scripts it runs, so a machine that was broken into
  must not be able to plant them on the laptop.
* **A copy, not a merge.** Each sync replaces that computer's folder there, so a skill
  deleted here disappears there too. Skills kept on the machine itself (its own
  ``<data>/skills``) are never touched and win over a copy with the same name.
* **Cheap to repeat.** A fingerprint of the folder (names, sizes, mtimes) is checked every
  few seconds; only a change sends anything.
"""

import hashlib
import io
import os
import re
import socket
import stat
import uuid
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from localagent.fsutil import remove_tree
from localagent.skills import SKILL_FILE, synced_skills_dir

MAX_BYTES = 20 * 1024 * 1024  # all skills together, unpacked
MAX_FILES = 2000
SKIPPED = frozenset({"__pycache__", ".git", ".venv", "node_modules", ".DS_Store"})
_ORIGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SyncError(Exception):
    """A sync that did not happen, with a reason a person can act on."""


def origin_name() -> str:
    """How this computer is named on the machines it syncs to."""
    name = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname()).strip("-.") or "computer"
    return name[:64]


def _files(directory: Path) -> list[Path]:
    """The files that belong to skills: those inside a top-level folder holding a SKILL.md."""
    if not directory.is_dir():
        return []
    found = []
    for skill in sorted(directory.iterdir()):
        if not (skill.is_dir() and (skill / SKILL_FILE).is_file()) or skill.name in SKIPPED:
            continue
        for root, dirs, names in os.walk(skill):
            dirs[:] = sorted(d for d in dirs if d not in SKIPPED)
            found += [Path(root) / n for n in sorted(names) if n not in SKIPPED]
    return found


def fingerprint(directory: Path) -> str:
    """Changes whenever a skill file is added, removed or edited."""
    digest = hashlib.sha1()
    for path in _files(directory):
        try:
            info = path.stat()
        except OSError:
            continue
        digest.update(f"{path.relative_to(directory).as_posix()}\0{info.st_size}\0{info.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def pack(directory: Path) -> tuple[bytes, int]:
    """Zip the skills in *directory*. Returns the archive and how many skills it holds."""
    files = _files(directory)
    if len(files) > MAX_FILES:
        raise SyncError(f"{directory} holds {len(files)} files; at most {MAX_FILES} are copied")
    total = sum(p.stat().st_size for p in files)
    if total > MAX_BYTES:
        raise SyncError(f"the skills in {directory} take {total // (1024 * 1024)} MB; "
                        f"at most {MAX_BYTES // (1024 * 1024)} MB are copied")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(directory).as_posix())
    skills = {p.relative_to(directory).parts[0] for p in files}
    return buffer.getvalue(), len(skills)


def _safe_member(name: str) -> tuple[str, ...]:
    parts = tuple(p for p in name.replace("\\", "/").split("/") if p not in ("", "."))
    if not parts or name.startswith("/") or ".." in parts or ":" in parts[0]:
        raise SyncError(f"refusing a file outside the skills folder: {name!r}")
    return parts


def unpack(data: bytes, origin: str) -> int:
    """Replace the skills copied from *origin* with those in *data*. Returns how many there are now."""
    if not _ORIGIN.match(origin):
        raise SyncError(f"not a computer name: {origin!r}")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SyncError("the skills did not arrive as a zip archive") from None
    members = [m for m in archive.infolist() if not m.is_dir()]
    if len(members) > MAX_FILES or sum(m.file_size for m in members) > MAX_BYTES:
        raise SyncError("too many skill files, or too large")
    base = synced_skills_dir()
    incoming = base / f".incoming-{uuid.uuid4().hex[:8]}"  # load_skills ignores dot folders
    incoming.mkdir(parents=True)
    try:
        for member in members:
            target = incoming.joinpath(*_safe_member(member.filename))
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source:
                content = source.read(MAX_BYTES + 1)
            target.write_bytes(content)
            if os.name != "nt" and content.startswith(b"#!"):  # zips from Windows carry no exec bit
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        count = sum(1 for d in incoming.iterdir() if (d / SKILL_FILE).is_file())
        target = base / origin
        if target.exists() and not remove_tree(target):
            raise SyncError(f"could not replace {target}")
        incoming.rename(target)
        return count
    finally:
        if incoming.exists():
            remove_tree(incoming)


def push(ui_url: str, directory: Path, origin: str, timeout: float = 30.0) -> int:
    """Send the skills in *directory* to the LocalAgent UI at *ui_url* (as printed, with ``#token=``)."""
    parts = urlsplit(ui_url)
    token = (parse_qs(parts.fragment).get("token") or [""])[0]
    if not token:
        raise SyncError("the machine's UI address carries no token")
    data, count = pack(directory)
    try:
        # The tunnel listens on 127.0.0.1 here: never through an HTTP proxy.
        response = httpx.put(f"{parts.scheme}://{parts.netloc}/api/skills/synced", params={"origin": origin},
                             content=data, headers={"Authorization": f"Bearer {token}",
                                                    "Content-Type": "application/zip"},
                             timeout=timeout, trust_env=False)
    except httpx.HTTPError as e:
        raise SyncError(f"could not reach the machine's LocalAgent: {e}") from None
    if response.status_code in (404, 405):
        if not count:
            return 0  # nothing to copy, so an older LocalAgent there is nothing to complain about
        raise SyncError("LocalAgent on that machine is too old to receive skills; update it there")
    if response.status_code != 200:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise SyncError(f"the machine refused the skills ({response.status_code}): {str(detail)[:200]}")
    return int(response.json().get("skills", 0))
