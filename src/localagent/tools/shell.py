"""The Bash tool.

On Windows commands run in Git for Windows' bash. ``shutil.which("bash")``
is deliberately not used: it usually finds the WSL launcher in System32,
which would run the command in a different operating system.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from localagent.procutil import kill_tree, new_group
from localagent.tools.base import Tool, ToolError
from localagent.tools.workspace import Workspace

Approver = Callable[[str, str], bool]
OutputCallback = Callable[[str], None]

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 1800
MAX_OUTPUT_CHARS = 8000

_SEGMENTS = re.compile(r"&&|\|\||[;|\n&]")
_MSYS_DRIVE = re.compile(r"^/([a-zA-Z])(/.*)?$")
_RISKY = [
    (re.compile(r"\bgit\s+push\b"), "pushes to a remote"),
    (re.compile(r"\bgit\s+(switch|rebase|merge)(?![-\w])"), "switches, rebases or merges branches"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "discards commits or changes (git reset --hard)"),
    (re.compile(r"\bgit\s+clean\s+-\w*f"), "deletes untracked files (git clean)"),
    (re.compile(r"\bgit\s+branch\s+-D\b"), "force-deletes a branch"),
    (re.compile(r"\bgit\s+(checkout|restore)\s+(--\s|\.)"), "discards working-tree changes"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(ba|z)?sh\b"), "pipes a download into a shell"),
    (re.compile(r"\b(shutdown|reboot|mkfs|diskpart)\b"), "is a system command"),
]
# CMD's null device, which models write out of habit ("2>nul", ">NUL 2>&1"). In bash it is an ordinary file
# name: the command leaves a file called nul in the project, which Windows programs cannot open or delete
# (the name means the device to them) and which makes git for Windows fail "add -A" outright.
_NUL_REDIRECT = re.compile(r"(&|\d*)(>>?)[ \t]*nul(?![\w.:/\\-])", re.IGNORECASE)
NUL_NOTE = "[note: in bash `nul` is a file name, not the null device; this ran with /dev/null instead]"


def find_bash() -> str:
    """Return the bash executable to use; raise ``ToolError`` if none is found."""
    if sys.platform != "win32":
        return shutil.which("bash") or "/bin/sh"
    candidates = []
    git = shutil.which("git")
    if git:
        for parent in Path(git).resolve().parents:
            if (parent / "usr" / "bin").is_dir():
                candidates.append(parent / "bin" / "bash.exe")
                break
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise ToolError("Git Bash was not found. Install Git for Windows (https://git-scm.com/download/win).")


def _cd_target(target: str, cwd: Path) -> Path | None:
    """Resolve a ``cd`` argument the way bash would, or ``None`` if it depends on variables."""
    home = Path.home()
    if target in ("~", "$HOME", "${HOME}"):
        return home
    for prefix in ("~/", "$HOME/", "${HOME}/"):
        if target.startswith(prefix):
            return (home / target[len(prefix):]).resolve()
    if "$" in target or "`" in target:
        return None
    if sys.platform == "win32":
        match = _MSYS_DRIVE.match(target)
        if match:
            return Path(f"{match.group(1)}:{match.group(2) or '/'}").resolve()
        if target.startswith("/"):
            return Path("/msys-root") / target.lstrip("/")  # e.g. /tmp: outside any Windows project
    path = Path(target)
    return (path if path.is_absolute() else cwd / path).resolve()


def leaves_workspace(command: str, root: Path) -> str | None:
    """Return the first ``cd``/``pushd`` target in *command* that leaves *root*, if any."""
    cwd = root
    for segment in _SEGMENTS.split(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        if not tokens or tokens[0] not in ("cd", "pushd"):
            continue
        target = tokens[1] if len(tokens) > 1 else "~"
        if target == "-":
            continue
        dest = _cd_target(target, cwd)
        if dest is None:
            continue
        if dest != root and root not in dest.parents:
            return target
        cwd = dest
    return None


def _rm_is_risky(command: str) -> bool:
    for segment in _SEGMENTS.split(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        if not tokens or tokens[0] != "rm":
            continue
        flags = "".join(t[1:] for t in tokens[1:] if t.startswith("-") and not t.startswith("--"))
        recursive = "r" in flags.lower() or "--recursive" in tokens
        forced = "f" in flags or "--force" in tokens
        targets = [t for t in tokens[1:] if not t.startswith("-")]
        scratch_only = targets and all(t.removeprefix("./").startswith("tmp/") or t in ("tmp", "./tmp") for t in targets)
        if recursive and forced and not scratch_only:
            return True
    return False


def risk_of(command: str) -> str | None:
    """Return why *command* needs user approval, or ``None`` if it may run directly."""
    for pattern, reason in _RISKY:
        if pattern.search(command):
            return reason
    if _rm_is_risky(command):
        return "recursively force-deletes files"
    return None


def _quoted(text: str, end: int) -> bool:
    """Whether position *end* of *text* lies inside single or double quotes."""
    quote, i = "", 0
    while i < end:
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = ""
        elif c == "\\":
            i += 1  # the next character is escaped
        elif quote == '"':
            if c == '"':
                quote = ""
        elif c in "'\"":
            quote = c
        i += 1
    return bool(quote)


def null_redirects(command: str) -> str:
    """*command* with redirections to CMD's ``nul`` sent to ``/dev/null``; quoted text is left alone."""
    return _NUL_REDIRECT.sub(
        lambda m: m.group(0) if _quoted(command, m.start()) else f"{m.group(1)}{m.group(2)}/dev/null", command)


def command_env(ws: Workspace) -> dict[str, str]:
    """Environment for commands: the project's venv first, never LocalAgent's own venv or model keys."""
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    for name in ws.hidden_env:  # model API keys: the agent must not be able to read them out
        env.pop(name, None)
    own_prefix = Path(sys.prefix).resolve() if sys.prefix != sys.base_prefix else None
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    if own_prefix is not None:
        parts = [p for p in parts if own_prefix not in Path(p).resolve().parents]
    venv = ws.venv()
    if venv is not None:
        parts.insert(0, str(venv / ("Scripts" if sys.platform == "win32" else "bin")))
        env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = os.pathsep.join(parts)
    env.update(
        LC_ALL="C.UTF-8",
        LANG="C.UTF-8",
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        GIT_PAGER="cat",
        PAGER="cat",
        GIT_TERMINAL_PROMPT="0",
        LOCALAGENT_WORKDIR=str(ws.root),
    )
    return env


def _path_forms(path: Path) -> list[str]:
    """Spellings of *path* a command may use, normalized to lower case with forward slashes."""
    text = str(path).replace("\\", "/").rstrip("/").lower()
    forms = [text]
    if len(text) > 2 and text[1] == ":":
        forms.append(f"/{text[0]}{text[2:]}")  # Git Bash: C:/x -> /c/x
    return forms


def names_protected_path(command: str, ws: Workspace) -> Path | None:
    """Return a protected directory that *command* names outside the workspace, if any.

    In a worktree, the main checkout contains the worktree itself, so a
    mention is allowed when it continues into the workspace root.
    """
    text = command.replace("\\", "/").lower()
    allowed = _path_forms(ws.root)
    for protected in ws.protected_roots:
        for form in _path_forms(protected):
            start = text.find(form)
            while start != -1:
                if not any(text.startswith(ok, start) for ok in allowed):
                    return protected
                start = text.find(form, start + 1)
    return None


def truncate_middle(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the head and tail of long output; the middle is rarely what matters."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters omitted ...]\n{text[-half:]}"


def run_command(
    command: str,
    ws: Workspace,
    timeout: float,
    on_output: OutputCallback | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[str, int | None, str]:
    """Run *command* in bash inside the workspace root.

    Returns:
        ``(output, exit_code, status)`` where status is ``"ok"``, ``"timeout"`` or ``"stopped"``.
    """
    # Its own process group, so a timeout or Stop kills the whole tree -- and so that whatever the
    # command leaves running in the background can be stopped when the task ends.
    process = subprocess.Popen(
        [find_bash(), "-c", null_redirects(command)],
        cwd=ws.root,
        env=command_env(ws),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **new_group(),
    )
    ws.background.adopt(process)
    chunks: list[str] = []

    def pump() -> None:
        assert process.stdout is not None
        for raw in iter(process.stdout.readline, b""):
            text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
            chunks.append(text)
            if on_output is not None:
                on_output(text)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    status = "ok"
    while True:
        try:
            process.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if stop_event is not None and stop_event.is_set():
                status = "stopped"
            elif time.monotonic() > deadline:
                status = "timeout"
            else:
                continue
            kill_tree(process)
            break
    # A background child may keep the pipe open; do not wait for it forever.
    reader.join(timeout=5)
    return "".join(chunks), process.returncode, status


def guard_command(command: str, ws: Workspace, approver: Approver | None) -> None:
    """Raise ``ToolError`` if *command* leaves the workspace, names a protected
    directory, or is risky and not approved."""
    escaped = leaves_workspace(command, ws.root)
    if escaped is not None:
        raise ToolError(
            f"`cd {escaped}` leaves the working directory {ws.root}. Commands already start there; "
            "use paths inside it (put scratch files in ./tmp/)."
        )
    protected = names_protected_path(command, ws)
    if protected is not None:
        raise ToolError(
            f"the command refers to {protected}, the user's own checkout. You work in an isolated copy at "
            f"{ws.root}; use relative paths instead."
        )
    reason = risk_of(command)
    if reason is not None and (approver is None or not approver(command, reason)):
        raise ToolError(f"the user did not approve this command because it {reason}.")


def make_bash_tool(
    ws: Workspace,
    approver: Approver | None = None,
    on_output: OutputCallback | None = None,
    stop_event: threading.Event | None = None,
) -> Tool:
    """Return the Bash tool bound to *ws*."""

    def bash(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        guard_command(command, ws, approver)
        timeout = min(max(1, timeout), MAX_TIMEOUT)
        output, code, status = run_command(command, ws, timeout, on_output, stop_event)
        output = truncate_middle(output.rstrip("\n")) or "(no output)"
        if null_redirects(command) != command:  # say so, or the model keeps writing it
            output += f"\n{NUL_NOTE}"
        if status == "timeout":
            return f"{output}\n[timed out after {timeout}s and was killed; use a larger timeout or run it in the background]"
        if status == "stopped":
            return f"{output}\n[stopped by the user]"
        return f"{output}\n[exit code {code}]"

    return Tool(
        "Bash",
        "Run a shell command with bash (Git Bash on Windows) in the working directory. Use it for tests, "
        "git, builds and package managers, not for reading or editing files. Output is truncated to "
        f"{MAX_OUTPUT_CHARS} characters (head and tail).",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": f"Seconds before the command is killed (default {DEFAULT_TIMEOUT})."},
            },
            "required": ["command"],
        },
        bash,
    )
