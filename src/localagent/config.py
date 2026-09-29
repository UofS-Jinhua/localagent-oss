"""Filesystem locations used by LocalAgent, and the API-key file."""

import os
import re
from pathlib import Path

_SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
WORKTREE_DIR = ".la-worktrees"  # worktree/manager.py owns the name; config cannot import it (circular)
LEGACY_HOME = Path.home() / ".localagent"
_tried_migration = False


def _checkout_data_dir() -> Path | None:
    """``<checkout>/data`` when LocalAgent runs from its own source tree, else ``None``.

    Everything the program owns -- registry, keys, database, attachments, scratch -- then sits beside
    the code it belongs to, so an installation is one folder instead of two.
    """
    checkout = Path(__file__).resolve().parents[2]
    if not (checkout / "pyproject.toml").is_file():
        return None
    # A chat's worktree is a copy of the checkout: its data belongs to the original, not to the copy.
    parts = checkout.parts
    if WORKTREE_DIR in parts:
        checkout = Path(*parts[: parts.index(WORKTREE_DIR)])
    return checkout / "data"


def home_dir() -> Path:
    """Where LocalAgent keeps its own files: ``$LOCALAGENT_HOME``, else ``<checkout>/data``.

    Installed as a wheel (no source tree) it stays at ``~/.localagent``, and so does a checkout whose
    older ``~/.localagent`` could not be moved in (see ``_migrate_legacy_home``).
    """
    override = os.environ.get("LOCALAGENT_HOME")
    if override:
        return Path(override)
    data = _checkout_data_dir()
    if data is None:
        return LEGACY_HOME
    _migrate_legacy_home(data)
    return data if data.exists() or not LEGACY_HOME.exists() else LEGACY_HOME


def _migrate_legacy_home(data: Path) -> None:
    """Move an older ``~/.localagent`` into the checkout once, and only if it can be moved whole.

    A rename is all or nothing: while another ``la`` process holds the database it fails, the old
    folder keeps being used, and nothing ends up half here and half there.
    """
    global _tried_migration
    if _tried_migration or data.exists() or not LEGACY_HOME.is_dir():
        return
    _tried_migration = True
    try:
        data.parent.mkdir(parents=True, exist_ok=True)
        LEGACY_HOME.rename(data)
    except OSError:
        pass  # in use, or on another drive: keep using ~/.localagent


def registry_path() -> Path:
    """Return the path of the model registry file."""
    return home_dir() / "models.yaml"


def scratch_dir() -> Path:
    """A folder for chats that have no project: quick questions, one-off scripts.

    It is a real directory, so every tool still works there, but it is never a
    git repository and never gets branch isolation.
    """
    path = home_dir() / "scratch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_scratch(path: Path) -> bool:
    """Whether *path* is LocalAgent's own folder (the scratch area), not a user project."""
    path = Path(path).resolve()
    home = home_dir().resolve()
    return path == home or home in path.parents


def secrets_path() -> Path:
    """Return the path of the API-key file (``NAME=value`` lines)."""
    return home_dir() / "secrets.env"


def read_secrets() -> dict[str, str]:
    """The keys stored in ``secrets.env``; missing or unreadable file means none."""
    try:
        text = secrets_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        found[name.strip()] = value.replace("﻿", "").strip().strip('"').strip("'")
    return found


def get_secret(name: str) -> str:
    """An API key by variable name: the environment first, then ``secrets.env``.

    Keys from the file are deliberately not put into ``os.environ``, so commands
    the agent runs never inherit them.
    """
    if not name:
        return ""
    return os.environ.get(name) or read_secrets().get(name, "")


def save_secret(name: str, value: str) -> Path:
    """Store (or with an empty *value*, remove) a key in ``secrets.env``."""
    if not _SECRET_NAME.match(name):
        raise ValueError(f"invalid key name {name!r}: use letters, digits and underscores")
    value = value.replace("﻿", "").strip()  # PowerShell pipes add a byte-order mark
    if not value.isascii() or not value.isprintable():
        raise ValueError("the key must be a single line of printable ASCII characters")
    secrets = read_secrets()
    if value:
        secrets[name] = value
    else:
        secrets.pop(name, None)
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "# API keys for LocalAgent providers (never commit this file)\n"
    body += "".join(f"{k}={v}\n" for k, v in secrets.items())
    path.write_text(body, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path
