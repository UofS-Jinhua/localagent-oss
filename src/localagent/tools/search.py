"""Search tools: Grep and Glob. Pure Python, so they work without ripgrep."""

import fnmatch
import os
import re
import subprocess
from pathlib import Path

from localagent.attachments import ATTACHMENT_DIR
from localagent.procutil import quiet
from localagent.tools.base import Tool, ToolError
from localagent.tools.workspace import Workspace, is_binary

IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".la-worktrees", ATTACHMENT_DIR,
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".idea", ".vscode",
}
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_GLOB_RESULTS = 200
DEFAULT_GREP_RESULTS = 100
MAX_MATCH_CHARS = 240


def list_files(start: Path) -> list[Path]:
    """Files under *start*, honouring .gitignore when inside a git repository.

    The attachments folder ignores itself for git, so it is only searched when
    named explicitly (``path=".la-attachments"``).
    """
    if ATTACHMENT_DIR in start.parts:
        return sorted(p for p in start.rglob("*") if p.is_file() and p.name != ".gitignore")
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."],
            cwd=start, capture_output=True, timeout=30, **quiet(),
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        names = [n for n in result.stdout.decode("utf-8", "replace").split("\0") if n]
        kept = [n for n in names if not IGNORED_DIRS.intersection(n.split("/")[:-1])]
        return sorted(p for p in (start / n for n in kept) if p.is_file())
    found = []
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        found.extend(Path(dirpath) / f for f in sorted(filenames))
    return found


def _matches_glob(relative: str, pattern: str) -> bool:
    if "/" not in pattern:
        return fnmatch.fnmatch(relative.rsplit("/", 1)[-1], pattern)
    return fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(relative, pattern.replace("**/", ""))


def _search_root(ws: Workspace, path: str) -> Path:
    start = ws.resolve(path or ".")
    if not start.exists():
        raise ToolError(f"path not found: {ws.display(start)}")
    return start


def make_search_tools(ws: Workspace) -> list[Tool]:
    """Return Grep and Glob bound to *ws*."""

    def grep(pattern: str, path: str = ".", glob: str = "", ignore_case: bool = False,
             max_results: int = DEFAULT_GREP_RESULTS) -> str:
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            raise ToolError(f"invalid regular expression {pattern!r}: {e}") from None
        start = _search_root(ws, path)
        files = [start] if start.is_file() else list_files(start)
        hits: list[str] = []
        total = 0
        for file in files:
            rel = ws.display(file)
            if glob and not _matches_glob(rel, glob):
                continue
            try:
                if file.stat().st_size > MAX_FILE_BYTES:
                    continue
                data = file.read_bytes()
            except OSError:
                continue
            if is_binary(data):
                continue
            for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if regex.search(line):
                    total += 1
                    if len(hits) < max_results:
                        hits.append(f"{rel}:{number}: {line.strip()[:MAX_MATCH_CHARS]}")
        if not hits:
            return f"No matches for {pattern!r} in {ws.display(start)}."
        more = f"\n[{total - len(hits)} more matches not shown; narrow the pattern or path]" if total > len(hits) else ""
        return "\n".join(hits) + more

    def glob_files(pattern: str, path: str = ".") -> str:
        start = _search_root(ws, path)
        matched = [ws.display(f) for f in list_files(start) if _matches_glob(f.relative_to(start).as_posix(), pattern)]
        if not matched:
            return f"No files match {pattern!r} in {ws.display(start)}."
        more = f"\n[{len(matched) - MAX_GLOB_RESULTS} more files not shown]" if len(matched) > MAX_GLOB_RESULTS else ""
        return "\n".join(matched[:MAX_GLOB_RESULTS]) + more

    return [
        Tool(
            "Grep",
            "Search file contents with a regular expression. Returns path:line: text. Respects .gitignore.",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression."},
                    "path": {"type": "string", "description": "File or directory to search (default: working directory)."},
                    "glob": {"type": "string", "description": "Only search files matching this pattern, e.g. *.py"},
                    "ignore_case": {"type": "boolean"},
                    "max_results": {"type": "integer", "description": f"Default {DEFAULT_GREP_RESULTS}."},
                },
                "required": ["pattern"],
            },
            grep,
        ),
        Tool(
            "Glob",
            "List files whose path matches a pattern such as *.py or src/**/*.ts. "
            "Patterns without '/' match file names at any depth. Respects .gitignore.",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Directory to search (default: working directory)."},
                },
                "required": ["pattern"],
            },
            glob_files,
        ),
    ]
