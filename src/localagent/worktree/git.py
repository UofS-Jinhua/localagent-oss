"""A small wrapper around the git command line."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from localagent.procutil import quiet

FALLBACK_IDENTITY = ("-c", "user.name=LocalAgent", "-c", "user.email=localagent@localhost")


class GitError(Exception):
    """A git command failed."""

    def __init__(self, args: tuple[str, ...], result: subprocess.CompletedProcess[str]):
        detail = (result.stderr or result.stdout).strip()
        super().__init__(f"git {' '.join(args)} failed ({result.returncode}): {detail}")
        self.result = result


def git(*args: str, cwd: Path, check: bool = True, timeout: float = 300,
        input: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run git in *cwd* without prompts, with paths printed unquoted (so non-ASCII names survive)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat")
    result = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=timeout, input=input, **quiet(),
    )
    if check and result.returncode != 0:
        raise GitError(args, result)
    return result


def blob(rev: str, path: str, cwd: Path, limit: int) -> tuple[bytes, bool] | None:
    """The first *limit* bytes of *path* (relative to *cwd*) as of *rev*, and whether there was more.

    ``None`` when the file is not in that commit. Bytes, not text: the file may be in any encoding.
    """
    spec = f"{rev}:./{path}"
    size = git("cat-file", "-s", spec, cwd=cwd, check=False)
    if size.returncode != 0:
        return None
    if int(size.stdout.strip() or 0) > 8 * limit:
        return b"", True  # far too large to show; not worth reading
    result = subprocess.run(["git", "cat-file", "blob", spec], cwd=cwd, capture_output=True, timeout=60,
                            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), **quiet())
    if result.returncode != 0:
        return None
    data = result.stdout
    if len(data) <= limit:
        return data, False
    return data[:data.rfind(b"\n", 0, limit) + 1 or limit], True


def out(*args: str, cwd: Path) -> str:
    """Stdout of a successful git command, stripped."""
    return git(*args, cwd=cwd).stdout.strip()


def succeeds(*args: str, cwd: Path) -> bool:
    return git(*args, cwd=cwd, check=False).returncode == 0


def repo_root(path: Path) -> Path | None:
    """The top-level directory of the repository containing *path*, or ``None``."""
    directory = path if path.is_dir() else path.parent
    result = git("rev-parse", "--show-toplevel", cwd=directory, check=False)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def common_dir(repo: Path) -> Path:
    """The shared ``.git`` directory (the same for every worktree of the repository)."""
    return Path(out("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo))


def has_commits(repo: Path) -> bool:
    return succeeds("rev-parse", "--verify", "--quiet", "HEAD", cwd=repo)


def current_branch(directory: Path) -> str | None:
    """The checked-out branch, or ``None`` when HEAD is detached."""
    result = git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=directory, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def head(directory: Path) -> str:
    return out("rev-parse", "HEAD", cwd=directory)


def branch_exists(repo: Path, branch: str) -> bool:
    return succeeds("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", cwd=repo)


def local_branches(repo: Path) -> list[str]:
    text = out("for-each-ref", "--format=%(refname:short)", "refs/heads/", cwd=repo)
    return [line for line in text.splitlines() if line]


def is_dirty(directory: Path) -> bool:
    """Whether the working tree has staged, unstaged or untracked (non-ignored) changes."""
    return bool(out("status", "--porcelain", "--untracked-files=normal", cwd=directory))


def has_tracked_changes(directory: Path) -> bool:
    """Whether tracked files have staged or unstaged edits (untracked files do not count)."""
    return not succeeds("diff", "--quiet", "HEAD", cwd=directory)


def identity_args(repo: Path) -> tuple[str, ...]:
    """Extra ``-c`` options giving commits an author when the user has none configured."""
    email = git("config", "user.email", cwd=repo, check=False).stdout.strip()
    return () if email else FALLBACK_IDENTITY


@dataclass
class StatusEntry:
    """One line of ``git status --porcelain=v1``: index code, worktree code, path, and the source of a rename."""

    index: str
    worktree: str
    path: str
    original: str | None = None


def status_entries(directory: Path, untracked: str = "all") -> list[StatusEntry]:
    """Parse ``git status --porcelain=v1 -z`` (robust to spaces and non-ASCII names).

    Args:
        untracked: ``"all"`` lists every untracked file; ``"normal"`` lists an
            untracked folder once (path ending in ``/``), which is much faster
            when, say, an unignored virtualenv sits in the tree.
    """
    raw = git("status", "--porcelain=v1", "-z", f"--untracked-files={untracked}", cwd=directory).stdout
    fields = raw.split("\0")
    entries: list[StatusEntry] = []
    i = 0
    while i < len(fields) and fields[i]:
        record = fields[i]
        entry = StatusEntry(record[0], record[1], record[3:])
        if entry.index in "RC":
            entry.original = fields[i + 1]
            i += 1
        entries.append(entry)
        i += 1
    return entries


def worktree_checkouts(repo: Path) -> dict[str, Path]:
    """Map each checked-out branch to the worktree directory that has it."""
    text = out("worktree", "list", "--porcelain", cwd=repo)
    checkouts: dict[str, Path] = {}
    path: Path | None = None
    for line in text.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):]).resolve()
        elif line.startswith("branch refs/heads/") and path is not None:
            checkouts[line[len("branch refs/heads/"):]] = path
    return checkouts
