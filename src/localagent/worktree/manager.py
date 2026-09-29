"""Layer 5: each chat works on its own branch in its own git worktree.

Adapted from KISS Sorcar's ``WorktreeSorcarAgent`` / ``GitWorktreeOps`` with
one deliberate change. KISS gives every task a fresh branch and auto-merges a
finished task into the user's branch when the next task starts. Here the unit
is a chat: its tasks accumulate on ``la/<chat id>`` (one commit per task), a
follow-up task sees the earlier ones' changes, and nothing reaches the user's
branch until they choose to merge. Kept from KISS:

* the user's uncommitted changes are copied in as a baseline commit, so the
  agent sees what the user sees;
* merging squashes only the commits after that baseline, via
  ``cherry-pick --no-commit``, with ``-X theirs`` only when the base has not
  moved (it resolves conflicts that the baseline itself would create);
* all state lives in git (branch names and ``branch.<name>.la-*`` config), so
  it survives crashes and any process can list it;
* the worktree directory sits in ``<repo>/.la-worktrees/`` and is hidden via
  ``.git/info/exclude``, not ``.gitignore``.
"""

import os
import re
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from localagent.attachments import ATTACHMENT_DIR
from localagent.fsutil import remove_tree
from localagent.worktree import git
from localagent.worktree.lock import file_lock

WORKTREE_DIR = ".la-worktrees"
BRANCH_PREFIX = "la/"
BASELINE_MESSAGE = "la: baseline with the user's uncommitted changes"
MAX_DIFF_CHARS = 8000
# Never copied into or committed from a worktree, even when the project does not ignore them:
# caches and dependency folders at any depth, and top-level environments and the agent's scratch dir.
JUNK_DIRS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules")
JUNK_TOP_DIRS = (".venv", "venv", "tmp", WORKTREE_DIR, ATTACHMENT_DIR)
JUNK_SUFFIXES = (".pyc",)
# A command's "2>nul" leaves a file of this name in Git Bash. git for Windows cannot read it (the name opens
# the null device: "short read while indexing nul"), so staging it would fail the whole commit.
JUNK_NAMES = ("nul",)
JUNK_PATHSPECS = (
    *(f":(exclude,glob)**/{d}/**" for d in JUNK_DIRS),
    *(f":(exclude,glob){d}/**" for d in JUNK_TOP_DIRS),
    *(f":(exclude,glob)**/*{s}" for s in JUNK_SUFFIXES),
    *(f":(exclude,glob,icase)**/{n}" for n in JUNK_NAMES),
)


def is_junk(relative: str) -> bool:
    """Whether a repository-relative path (``/``-separated) is junk that must not travel."""
    parts = relative.rstrip("/").split("/")
    return (
        any(part in JUNK_DIRS for part in parts)
        or parts[0] in JUNK_TOP_DIRS
        or relative.endswith(JUNK_SUFFIXES)
        or parts[-1].lower() in JUNK_NAMES
    )

MessageFor = Callable[[str], str]


@dataclass
class ChatWorktree:
    """A chat's branch and worktree."""

    repo: Path
    chat_id: str
    branch: str
    base: str
    start: str
    baseline: str
    subdir: str = ""

    @property
    def path(self) -> Path:
        return self.repo / WORKTREE_DIR / self.chat_id

    @property
    def work_dir(self) -> Path:
        """Where the agent works: the same subdirectory the user started from."""
        return self.path / self.subdir if self.subdir else self.path

    @property
    def carries_user_changes(self) -> bool:
        return self.baseline != self.start


@dataclass
class Prepared:
    """Result of ``prepare``: a worktree, or the reason the task runs directly in the user's directory."""

    worktree: ChatWorktree | None
    created: bool = False
    restored: bool = False
    reason: str = ""


@dataclass
class MergeResult:
    """``status`` is ``merged``, ``nothing``, ``conflict`` or ``failed``."""

    status: str
    message: str
    commit: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class BranchSummary:
    """A pending chat branch, for status displays."""

    chat_id: str
    branch: str
    base: str
    commits: int
    files: int
    checked_out: bool


@dataclass
class RepoStatus:
    """What a status bar shows: the user's branch, uncommitted files, and pending chat branches."""

    repo: Path | None
    branch: str | None
    dirty_files: int
    pending: list[BranchSummary]


def _lock(repo: Path):
    return file_lock(git.common_dir(repo) / "localagent.lock")


def branch_name(chat_id: str) -> str:
    return BRANCH_PREFIX + chat_id


def _exclude_worktree_dir(repo: Path) -> None:
    exclude = git.common_dir(repo) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if f"/{WORKTREE_DIR}/" not in lines:
        with open(exclude, "a", encoding="utf-8") as f:
            f.write(("\n" if lines and lines[-1] else "") + f"/{WORKTREE_DIR}/\n")


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        remove_tree(path)
    elif path.exists() or path.is_symlink():
        if path.exists():
            os.chmod(path, stat.S_IWRITE)
        path.unlink()


def _ignore_junk(directory: str, names: list[str]) -> set[str]:
    return {n for n in names if n in JUNK_DIRS or n.endswith(JUNK_SUFFIXES) or n.lower() in JUNK_NAMES}


def copy_uncommitted(repo: Path, destination: Path) -> None:
    """Reproduce the working-tree changes of *repo* (edits, new files and folders, deletions, renames)
    in *destination*, skipping junk such as an unignored ``.venv``."""
    for entry in git.status_entries(repo, untracked="normal"):
        if is_junk(entry.path):
            continue
        if entry.original:
            _remove(destination / entry.original)
        source = repo / entry.path
        target = destination / entry.path
        if not source.exists():
            _remove(target)
        elif source.is_dir():  # an untracked folder (``git status`` lists it once, with a trailing /)
            shutil.copytree(source, target, ignore=_ignore_junk, dirs_exist_ok=True)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def load(repo: Path, chat_id: str) -> ChatWorktree | None:
    """The chat's worktree record, read back from git config, or ``None``."""
    branch = branch_name(chat_id)
    if not git.branch_exists(repo, branch):
        return None
    result = git.git("config", "--get-regexp", rf"^branch\.{re.escape(branch)}\.la-", cwd=repo, check=False)
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        values[key.rsplit(".la-", 1)[-1]] = value
    if not {"base", "start", "baseline"} <= values.keys():
        return None
    return ChatWorktree(repo, chat_id, branch, values["base"], values["start"], values["baseline"],
                        values.get("subdir", ""))


def pending(repo: Path) -> list[ChatWorktree]:
    """Every chat branch in *repo* that has not been merged or discarded."""
    names = git.out("for-each-ref", "--format=%(refname:short)", f"refs/heads/{BRANCH_PREFIX}", cwd=repo)
    found = [load(repo, name[len(BRANCH_PREFIX):]) for name in names.splitlines() if name]
    return [wt for wt in found if wt is not None]


def _create(repo: Path, chat_id: str, base: str, subdir: str, copy_changes: bool) -> ChatWorktree:
    branch = branch_name(chat_id)
    _exclude_worktree_dir(repo)
    start = git.out("rev-parse", f"refs/heads/{base}^{{commit}}", cwd=repo)
    worktree = ChatWorktree(repo, chat_id, branch, base, start, start, subdir)
    git.git("worktree", "add", "--quiet", "-b", branch, str(worktree.path), start, cwd=repo)
    if copy_changes and git.is_dirty(repo):
        copy_uncommitted(repo, worktree.path)
        if _stage(worktree.path):
            git.git(*git.identity_args(repo), "commit", "--quiet", "--no-verify", "-m", BASELINE_MESSAGE,
                    cwd=worktree.path)
            worktree.baseline = git.head(worktree.path)
    for key, value in (("base", base), ("start", start), ("baseline", worktree.baseline),
                       ("subdir", subdir), ("chat", chat_id)):
        git.git("config", f"branch.{branch}.la-{key}", value, cwd=repo)
    return worktree


def prepare(work_dir: str | Path, chat_id: str, base: str | None = None) -> Prepared:
    """Get the chat's worktree, creating it (or re-creating a kept one) as needed.

    Args:
        work_dir: The directory the user asked the agent to work in.
        chat_id: The chat; its branch is ``la/<chat_id>``.
        base: Branch to start from and merge back into (default: the checked-out branch).
            Ignored when the chat already has a branch.
    """
    work_dir = Path(work_dir).resolve()
    repo = git.repo_root(work_dir)
    if repo is None:
        return Prepared(None, reason="not a git repository")
    if WORKTREE_DIR in work_dir.relative_to(repo).parts:
        return Prepared(None, reason="the directory is itself inside a LocalAgent worktree")
    if not git.has_commits(repo):
        return Prepared(None, reason="the repository has no commits yet")
    subdir = work_dir.relative_to(repo).as_posix() if work_dir != repo else ""
    with _lock(repo):
        existing = load(repo, chat_id)
        if existing is not None:
            if (existing.path / ".git").exists():
                return Prepared(existing)
            git.git("worktree", "prune", cwd=repo)
            git.git("worktree", "add", "--quiet", str(existing.path), existing.branch, cwd=repo)
            return Prepared(existing, restored=True)
        current = git.current_branch(repo)
        base = base or current
        if base is None:
            return Prepared(None, reason="HEAD is detached; choose a base branch")
        if not git.branch_exists(repo, base):
            return Prepared(None, reason=f"there is no branch named {base!r}")
        return Prepared(_create(repo, chat_id, base, subdir, copy_changes=base == current), created=True)


def working_diff(worktree: ChatWorktree, limit: int = MAX_DIFF_CHARS) -> str:
    """What this task changed in the worktree and has not committed yet.

    ``add -N`` records the intent to add untracked files, which is what makes new
    files show up in ``git diff``; the real staging still happens at commit time.
    """
    directory = worktree.path
    if not (directory / ".git").exists():
        return ""
    git.git("add", "-N", "--", ".", *JUNK_PATHSPECS, cwd=directory, check=False)
    diff = git.out("diff", "--", ".", *JUNK_PATHSPECS, cwd=directory)
    return diff[:limit] + ("\n[... diff truncated]" if len(diff) > limit else "")


def _stage(directory: Path) -> bool:
    """Stage every change except junk; return whether anything is staged."""
    git.git("add", "-A", "--", ".", *JUNK_PATHSPECS, cwd=directory)
    return not git.succeeds("diff", "--cached", "--quiet", cwd=directory)


def commit_changes(worktree: ChatWorktree, message_for: MessageFor) -> tuple[str, str] | None:
    """Commit what the agent changed; return ``(sha, subject)``, or ``None`` if nothing changed.

    *message_for* receives the staged diff (stat plus truncated patch) and returns the message.
    """
    path = worktree.path
    if not _stage(path):
        return None
    diff = git.out("diff", "--cached", "--stat", cwd=path) + "\n\n" + git.git("diff", "--cached", cwd=path).stdout
    message = message_for(diff[:MAX_DIFF_CHARS])
    git.git(*git.identity_args(worktree.repo), "commit", "--quiet", "--no-verify", "-m", message, cwd=path)
    return git.head(path), message.splitlines()[0]


def commits(worktree: ChatWorktree) -> list[tuple[str, str]]:
    """``(short sha, subject)`` of the chat's commits after the baseline, oldest first."""
    text = git.out("log", "--reverse", "--format=%h%x09%s", f"{worktree.baseline}..{worktree.branch}", cwd=worktree.repo)
    return [tuple(line.split("\t", 1)) for line in text.splitlines() if line]  # type: ignore[misc]


def diff(worktree: ChatWorktree, stat: bool = False) -> str:
    """The chat's changes relative to where it started (the baseline), as a patch or a stat."""
    args = ["diff", "--stat"] if stat else ["diff"]
    return git.git(*args, worktree.baseline, worktree.branch, cwd=worktree.repo).stdout


def _cleanup(worktree: ChatWorktree, delete_branch: bool) -> list[str]:
    """Remove the worktree directory (and optionally the branch); return notes about anything left behind."""
    repo = worktree.repo
    notes = []
    git.git("worktree", "remove", "--force", str(worktree.path), cwd=repo, check=False)
    if not remove_tree(worktree.path):
        notes.append(f"The folder {worktree.path} could not be deleted (is a program using it?); "
                     "delete it by hand once it is free.")
    git.git("worktree", "prune", cwd=repo, check=False)
    if delete_branch:
        git.git("branch", "-D", worktree.branch, cwd=repo, check=False)
        git.git("config", "--remove-section", f"branch.{worktree.branch}", cwd=repo, check=False)
    return notes


def discard(worktree: ChatWorktree) -> list[str]:
    """Throw away the chat's branch and worktree; return notes about anything left behind.

    The user's checkout is not touched.
    """
    with _lock(worktree.repo):
        return _cleanup(worktree, delete_branch=True)


def keep(worktree: ChatWorktree, message_for: MessageFor) -> tuple[tuple[str, str] | None, list[str]]:
    """Commit leftovers and remove the worktree directory, keeping the branch for later.

    Returns the leftover commit's ``(sha, subject)`` (or ``None``) and notes about anything left behind.
    """
    with _lock(worktree.repo):
        committed = commit_changes(worktree, message_for) if (worktree.path / ".git").exists() else None
        notes = _cleanup(worktree, delete_branch=False)
    return committed, notes


def merge(worktree: ChatWorktree, message_for: MessageFor) -> MergeResult:
    """Squash the chat's work onto its base branch as one commit, then remove the chat branch.

    The merge happens where the base branch is checked out (normally the
    user's checkout, whose uncommitted changes are stashed around it and
    restored), or in a temporary worktree when it is not checked out, so the
    user's checkout is never switched to another branch.
    """
    repo = worktree.repo
    with _lock(repo):
        if (worktree.path / ".git").exists():
            commit_changes(worktree, message_for)
        if not commits(worktree):
            notes = _cleanup(worktree, delete_branch=True)
            return MergeResult("nothing", "The chat made no changes; its branch was removed.", notes=notes)
        temporary = None
        try:
            target = git.worktree_checkouts(repo).get(worktree.base)
            if target is None:
                # The base branch is not checked out anywhere: merge in a scratch worktree
                # rather than switching the user's checkout to it.
                temporary = repo / WORKTREE_DIR / f"_merge-{worktree.chat_id}"
                git.git("worktree", "add", "--quiet", str(temporary), worktree.base, cwd=repo)
                target = temporary
            result = _squash_into(worktree, target, message_for, stash=temporary is None)
        except git.GitError as e:
            # Anything git refuses (a base branch the user deleted, a locked index, a hook):
            # say so and keep the branch, instead of letting a traceback reach the user.
            result = MergeResult("failed", f"git refused the merge: {e}. The branch {worktree.branch} is kept.")
        if temporary is not None:
            git.git("worktree", "remove", "--force", str(temporary), cwd=repo, check=False)
            remove_tree(temporary)
            git.git("worktree", "prune", cwd=repo, check=False)
        if result.status == "merged":
            result.notes += _cleanup(worktree, delete_branch=True)
        return result


def _squash_into(worktree: ChatWorktree, target: Path, message_for: MessageFor, stash: bool) -> MergeResult:
    notes: list[str] = []
    stashed = False
    # Only tracked edits are stashed: untracked files (possibly a whole unignored virtualenv) stay
    # put, and if the merge would overwrite one, cherry-pick refuses and we report a conflict.
    if stash and git.has_tracked_changes(target):
        pushed = git.git("stash", "push", "-m", f"localagent: before merging {worktree.branch}",
                         cwd=target, check=False)
        if pushed.returncode != 0:
            return MergeResult("failed", f"Could not stash your uncommitted changes: {pushed.stderr.strip()}")
        stashed = True
    before = git.head(target)
    args = ["cherry-pick", "--no-commit"]
    if worktree.carries_user_changes and before == worktree.start:
        args += ["-X", "theirs"]
    picked = git.git(*args, f"{worktree.baseline}..{worktree.branch}", cwd=target, check=False)
    if picked.returncode != 0:
        git.git("cherry-pick", "--abort", cwd=target, check=False)
        git.git("reset", "--hard", "--quiet", before, cwd=target, check=False)
        result = MergeResult(
            "conflict",
            f"The chat's changes conflict with {worktree.base}. Nothing was merged and the branch "
            f"{worktree.branch} is kept. To resolve by hand: git checkout {worktree.base} && "
            f"git merge --squash {worktree.branch}",
        )
    elif git.succeeds("diff", "--cached", "--quiet", cwd=target):
        git.git("cherry-pick", "--quit", cwd=target, check=False)
        result = MergeResult("nothing", f"{worktree.base} already contains the chat's changes.")
    else:
        staged = git.out("diff", "--cached", "--stat", cwd=target) + "\n\n" + git.git("diff", "--cached", cwd=target).stdout
        committed = git.git(*git.identity_args(worktree.repo), "commit", "--quiet", "-m",
                            message_for(staged[:MAX_DIFF_CHARS]), cwd=target, check=False)
        git.git("cherry-pick", "--quit", cwd=target, check=False)
        if committed.returncode != 0:
            git.git("reset", "--hard", "--quiet", before, cwd=target, check=False)
            detail = (committed.stderr or committed.stdout).strip()
            result = MergeResult("failed", f"The merge commit was rejected (a pre-commit hook?): {detail}. "
                                           f"The branch {worktree.branch} is kept.")
        else:
            sha = git.head(target)
            result = MergeResult("merged", f"Merged {worktree.branch} into {worktree.base} as {sha[:8]}.", sha)
    if stashed and not git.succeeds("stash", "pop", cwd=target):
        notes.append("Your uncommitted changes could not be restored automatically; they are in `git stash` "
                     "(run `git stash pop` after resolving).")
    result.notes = notes
    return result


def repo_status(work_dir: str | Path) -> RepoStatus:
    """Current branch, uncommitted file count, and pending chat branches of the repository at *work_dir*."""
    repo = git.repo_root(Path(work_dir).resolve())
    if repo is None:
        return RepoStatus(None, None, 0, [])
    dirty = len(git.status_entries(repo, untracked="normal"))
    summaries = []
    for wt in pending(repo) if git.has_commits(repo) else []:
        count = git.out("rev-list", "--count", f"{wt.baseline}..{wt.branch}", cwd=repo)
        files = git.out("diff", "--name-only", wt.baseline, wt.branch, cwd=repo).splitlines()
        summaries.append(BranchSummary(wt.chat_id, wt.branch, wt.base, int(count or 0), len(files),
                                       (wt.path / ".git").exists()))
    return RepoStatus(repo, git.current_branch(repo), dirty, summaries)
