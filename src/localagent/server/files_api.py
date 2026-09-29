"""The project's files for the UI: a folder tree with git status, file contents, and each file's diff.

A tree has one of two roots:

* the folder the window works on (the user's checkout): changes are measured against ``HEAD``;
* a chat's worktree: changes are measured against the chat's baseline, so everything the chat
  did shows up, committed or not.

Only reads. Paths are relative to the root and may not leave it.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from localagent.config import scratch_dir
from localagent.store import Store
from localagent.tools.workspace import decode, is_binary
from localagent.worktree import git
from localagent.worktree import manager as wm

MAX_ENTRIES = 3000
MAX_CHANGED = 2000
MAX_TEXT_BYTES = 512 * 1024
MAX_DIFF_CHARS = 400_000
HIDDEN = {".git", wm.WORKTREE_DIR}
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's hash of the empty tree
IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".ico": "image/x-icon", ".svg": "image/svg+xml",
}


@dataclass
class Root:
    """The folder a tree shows, and what its changes are measured against."""

    path: Path
    git: bool
    base: str  # a commit (or the empty tree); meaningless when ``git`` is false
    label: str


def resolve_root(store: Store, work_dir: str, chat_id: str | None) -> Root:
    if chat_id:
        chat = store.chat(chat_id)
        if chat is None:
            raise HTTPException(404, f"no chat {chat_id}")
        repo = git.repo_root(Path(chat.work_dir))
        wt = wm.load(repo, chat_id) if repo is not None else None
        if wt is None or not (wt.path / ".git").exists() or not wt.work_dir.is_dir():
            raise HTTPException(404, "the chat has no worktree to show")
        return Root(wt.work_dir.resolve(), True, wt.baseline, wt.branch)
    if not work_dir.strip():
        raise HTTPException(400, "give work_dir or chat_id")
    root = Path(work_dir).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(404, f"{root} is not a directory")
    # The scratch folder is never a repository, even inside a home directory that is one.
    repo = None if root == scratch_dir().resolve() else git.repo_root(root)
    if repo is None:
        return Root(root, False, "", "")
    return Root(root, True, "HEAD" if git.has_commits(repo) else EMPTY_TREE, git.current_branch(repo) or "HEAD")


def _inside(root: Root, relative: str) -> Path:
    target = (root.path / relative).resolve() if relative else root.path
    if target != root.path and not target.is_relative_to(root.path):
        raise HTTPException(400, "the path leaves the folder")
    return target


def changed(root: Root, cwd: Path, pathspec: str = ".") -> dict[str, str]:
    """Changed paths under *pathspec*, relative to *cwd*: M, A, D, T, or U (untracked).

    An untracked folder is listed once, with a trailing ``/``, like ``git status`` does.
    """
    if not root.git or not cwd.is_dir():
        return {}
    found: dict[str, str] = {}
    fields = git.git("diff", "--name-status", "--no-renames", "-z", "--relative", root.base, "--", pathspec,
                     cwd=cwd, check=False).stdout.split("\0")
    for status, path in zip(fields[0::2], fields[1::2]):
        if status and path:
            found[path] = status[0]
    others = git.git("ls-files", "--others", "--exclude-standard", "--directory", "--no-empty-directory", "-z",
                     "--", pathspec, cwd=cwd, check=False).stdout
    for path in others.split("\0"):
        if path:
            found.setdefault(path, "U")
    return found


def _ignored(root: Root, directory: Path, names: list[str]) -> set[str]:
    if not root.git or not names:
        return set()
    result = git.git("check-ignore", "-z", "--stdin", cwd=directory, check=False, input="\0".join(names) + "\0")
    return {name for name in result.stdout.split("\0") if name}


def list_directory(root: Root, relative: str) -> dict[str, Any]:
    directory = _inside(root, relative)
    if not directory.is_dir():
        raise HTTPException(404, f"{relative or root.path} is not a directory")
    children: list[tuple[str, bool, int]] = []
    try:
        with os.scandir(directory) as scan:
            for entry in scan:
                if entry.name in HIDDEN:
                    continue
                try:
                    is_dir = entry.is_dir()
                    children.append((entry.name, is_dir, 0 if is_dir else entry.stat().st_size))
                except OSError:
                    continue
    except PermissionError:
        pass
    children.sort(key=lambda c: (not c[1], c[0].lower()))
    truncated = len(children) > MAX_ENTRIES
    children = children[:MAX_ENTRIES]

    statuses: dict[str, str] = {}
    for path, status in changed(root, directory).items():
        head, _, rest = path.rstrip("/").partition("/")
        if not rest and not path.endswith("/"):
            statuses[head] = status
        elif not rest:
            statuses[head] = "U"  # a whole untracked folder
        else:
            statuses.setdefault(head, "M")  # a folder with changes somewhere inside
    present = {name for name, _, _ in children}
    # Deleted files are not on disk, but their deletion is a change worth opening.
    gone = [(name, status) for name, status in statuses.items() if name not in present]
    ignored = _ignored(root, directory, [name + ("/" if is_dir else "") for name, is_dir, _ in children])
    prefix = f"{relative.strip('/')}/" if relative.strip("/") else ""
    entries = [{"name": name, "path": prefix + name, "dir": is_dir, "size": size, "status": statuses.get(name, ""),
                "ignored": (name + ("/" if is_dir else "")) in ignored or name in ignored, "deleted": False}
               for name, is_dir, size in children]
    entries += [{"name": name, "path": prefix + name, "dir": status != "D", "size": 0, "status": "D",
                 "ignored": False, "deleted": True} for name, status in sorted(gone)]
    return {"root": str(root.path), "git": root.git, "label": root.label, "entries": entries, "truncated": truncated}


def _added_diff(relative: str, text: str) -> str:
    """A patch that adds *text* as a new file (untracked files have no diff in git until they are added)."""
    lines = text.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    return f"diff --git a/{relative} b/{relative}\nnew file (untracked)\n--- /dev/null\n+++ b/{relative}\n" \
           f"@@ -0,0 +1,{len(lines)} @@\n{body}"


def read_file(root: Root, relative: str) -> dict[str, Any]:
    path = _inside(root, relative)
    relative = path.relative_to(root.path).as_posix()
    if path.is_dir():
        raise HTTPException(400, f"{relative} is a folder")
    status = changed(root, root.path, relative).get(relative, "")
    info: dict[str, Any] = {"path": relative, "root": str(root.path), "git": root.git, "label": root.label,
                            "status": status, "size": 0, "kind": "missing", "text": "", "encoding": "",
                            "truncated": False, "diff": "", "diff_truncated": False,
                            "base_text": "", "base_truncated": False}
    if path.is_file():
        info["size"] = path.stat().st_size
        if path.suffix.lower() in IMAGE_TYPES:
            info["kind"] = "image"
        else:
            with path.open("rb") as handle:
                data = handle.read(MAX_TEXT_BYTES + 1)
            if is_binary(data):
                info["kind"] = "binary"
            else:
                if len(data) > MAX_TEXT_BYTES:
                    data = data[:data.rfind(b"\n", 0, MAX_TEXT_BYTES) + 1 or MAX_TEXT_BYTES]
                    info["truncated"] = True
                info["kind"] = "text"
                info["text"], info["encoding"] = decode(data)
    elif status != "D":
        raise HTTPException(404, f"{relative} does not exist")
    if status == "U" and info["kind"] == "text":
        diff = _added_diff(relative, info["text"])
    elif status and status != "U":
        diff = git.git("diff", "--no-renames", root.base, "--", relative, cwd=root.path, check=False).stdout
    else:
        diff = ""
    info["diff"], info["diff_truncated"] = diff[:MAX_DIFF_CHARS], len(diff) > MAX_DIFF_CHARS
    # The text before the change, for a side-by-side diff: only a file that existed at the base has one.
    if status in ("M", "T", "D"):
        before = git.blob(root.base, relative, root.path, MAX_TEXT_BYTES)
        if before is not None and not is_binary(before[0]):
            info["base_text"], _ = decode(before[0])
            info["base_truncated"] = before[1]
    return info


def make_files_router(api: Any, store: Store) -> APIRouter:
    router = APIRouter(dependencies=[api])

    @router.get("/api/tree")
    def tree(work_dir: str = Query(""), chat_id: str | None = None, path: str = "") -> dict[str, Any]:
        """One folder of the tree (the UI expands folders one at a time)."""
        return list_directory(resolve_root(store, work_dir, chat_id), path)

    @router.get("/api/tree/changes")
    def tree_changes(work_dir: str = Query(""), chat_id: str | None = None) -> dict[str, Any]:
        """Every changed path under the root, for the "changed files" view."""
        root = resolve_root(store, work_dir, chat_id)
        files = sorted(changed(root, root.path).items())
        return {"root": str(root.path), "git": root.git, "label": root.label, "truncated": len(files) > MAX_CHANGED,
                "files": [{"path": path, "status": status} for path, status in files[:MAX_CHANGED]]}

    @router.get("/api/file")
    def file(path: str, work_dir: str = Query(""), chat_id: str | None = None) -> dict[str, Any]:
        """A file's text (or its kind, when it is not text) and its diff against the root's base."""
        return read_file(resolve_root(store, work_dir, chat_id), path)

    @router.get("/api/file/raw")
    def raw(path: str, work_dir: str = Query(""), chat_id: str | None = None) -> FileResponse:
        """An image in the tree, for the viewer. It is never run: the sandbox header stops an SVG's scripts."""
        target = _inside(resolve_root(store, work_dir, chat_id), path)
        if not target.is_file():
            raise HTTPException(404, f"{path} is not a file")
        return FileResponse(target, media_type=IMAGE_TYPES.get(target.suffix.lower(), "application/octet-stream"),
                            headers={"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"},
                            content_disposition_type="inline")

    return router
