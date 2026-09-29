"""The agent's working directory and its read-before-edit bookkeeping."""

import hashlib
import locale
import os
import re
from pathlib import Path

from localagent.procutil import Background

_MSYS_DRIVE = re.compile(r"^/([a-zA-Z])(/.*)?$")


def is_binary(data: bytes) -> bool:
    """Heuristic: a NUL byte in the first 8 KiB means binary."""
    return b"\x00" in data[:8192]


def decode(data: bytes) -> tuple[str, str]:
    """Decode file bytes, returning ``(text, encoding)``.

    Tries UTF-8 (with or without BOM), then the system code page (e.g. GBK on
    Chinese Windows), then Latin-1, which never fails.
    """
    for encoding in ("utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8",
                     locale.getpreferredencoding(False)):
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1"), "latin-1"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_venv(directories: list[Path]) -> Path | None:
    """The first ``.venv`` or ``venv`` directory found in *directories*, in order."""
    for directory in directories:
        for name in (".venv", "venv"):
            if (directory / name).is_dir():
                return directory / name
    return None


class Workspace:
    """Resolves paths against the working directory and tracks what the agent has read.

    Args:
        root: The directory the agent works in (a git worktree in isolated mode).
        venv_roots: Further directories searched for the project's virtualenv
            after *root*; a worktree has no ``.venv`` of its own, so the main
            checkout's is used.
        protected_roots: Directories that commands must not name, other than
            inside *root* (the main checkout, when working in a worktree under it).
        hidden_env: Environment variables to remove before running a command.
            The agent runs arbitrary commands, so the model API keys it is being
            served with must not be readable from inside them (``Agent`` fills
            this in from the registry).
        background: Where commands' background processes are collected, so the
            task can stop them when it ends; a fresh one by default.
    """

    def __init__(self, root: str | Path, venv_roots: list[Path] | None = None,
                 protected_roots: list[Path] | None = None, hidden_env: set[str] | None = None,
                 background: Background | None = None):
        self.root = Path(root).resolve()
        # Shared with the sub-agents, which get their own Workspace: one task, one set of leftovers.
        self.background = background if background is not None else Background()
        self.venv_roots = [Path(p).resolve() for p in venv_roots or []]
        self.protected_roots = [Path(p).resolve() for p in protected_roots or []]
        self.hidden_env: set[str] = set(hidden_env or ())
        self._seen: dict[Path, tuple[str, str]] = {}

    def venv(self) -> Path | None:
        """The virtualenv commands should use, if any."""
        return find_venv([self.root, *self.venv_roots])

    def resolve(self, path: str) -> Path:
        """Turn a model-supplied path (relative, absolute, ``~`` or ``/c/...``) into an absolute path."""
        text = path.strip().strip('"').strip("'")
        match = _MSYS_DRIVE.match(text) if os.name == "nt" else None
        if match:
            text = f"{match.group(1)}:{match.group(2) or '/'}"
        candidate = Path(os.path.expanduser(text))
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return candidate.resolve()

    def contains(self, path: Path) -> bool:
        return path == self.root or self.root in path.parents

    def display(self, path: Path) -> str:
        """Path relative to the root with forward slashes, or absolute if outside."""
        if self.contains(path):
            return path.relative_to(self.root).as_posix() or "."
        return str(path)

    def forget_reads(self) -> None:
        """Start a new session: the model no longer remembers what it read."""
        self._seen.clear()

    def record(self, path: Path, data: bytes, encoding: str) -> None:
        """Remember the content the agent has seen (after a Read, Write or Edit)."""
        self._seen[path] = (_digest(data), encoding)

    def encoding_of(self, path: Path) -> str:
        return self._seen.get(path, ("", "utf-8"))[1]

    def staleness(self, path: Path) -> str | None:
        """Return why *path* may not be modified yet, or ``None`` if it is safe."""
        seen = self._seen.get(path)
        if seen is None:
            return "You must Read it before modifying it."
        if _digest(path.read_bytes()) != seen[0]:
            return "It changed on disk since you last read it; Read it again first."
        return None
