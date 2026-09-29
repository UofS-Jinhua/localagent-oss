"""The RunTests tool: run the project's tests and report an unambiguous verdict.

Small models often run ``pytest | tail``, whose exit code is tail's, and then
misread the result. This tool detects the test command, never pipes it, and
leads its output with the real verdict and exit code.
"""

import json
import re
import threading
from pathlib import Path
from typing import Any

from localagent.tools.base import Tool, ToolError
from localagent.tools.shell import Approver, OutputCallback, guard_command, run_command, truncate_middle
from localagent.tools.workspace import Workspace

DEFAULT_TIMEOUT = 600
_PYTEST_SUMMARY = re.compile(r"\b\d+ (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b")


def detect_test_command(root: Path) -> str | None:
    """Guess the project's test command from its files."""
    pyproject = root / "pyproject.toml"
    python_markers = (root / "pytest.ini", root / "conftest.py", root / "setup.cfg", root / "tox.ini")
    if (
        any(p.is_file() for p in python_markers)
        or (pyproject.is_file() and "pytest" in pyproject.read_text(encoding="utf-8", errors="replace"))
        or any((root / "tests").glob("test_*.py"))
        or any(root.glob("test_*.py"))
    ):
        return "python -m pytest -q --tb=short"
    package = root / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except ValueError:
            scripts = {}
        if "test" in scripts:
            return "npm test --silent"
    if (root / "go.mod").is_file():
        return "go test ./..."
    if (root / "Cargo.toml").is_file():
        return "cargo test"
    return None


def summary_line(output: str) -> str:
    """The most informative line of test output: pytest's tally if present, else the last line."""
    lines = [line.strip(" =") for line in output.strip().splitlines() if line.strip(" =")]
    for line in reversed(lines):
        if _PYTEST_SUMMARY.search(line):
            return line
    return lines[-1] if lines else "(no output)"


def make_test_tool(
    ws: Workspace,
    approver: Approver | None = None,
    on_output: OutputCallback | None = None,
    stop_event: threading.Event | None = None,
    read_only: bool = False,
) -> Tool:
    """Return the RunTests tool bound to *ws*.

    With *read_only* the ``command`` argument is gone: an agent that is not
    allowed to run shell commands must not get one through this door. It can
    still run the project's own test command, with or without a target.
    """

    def run_tests(target: str = "", command: str = "", timeout: int = DEFAULT_TIMEOUT) -> str:
        if command and read_only:  # belt and braces: it is not in the schema, so Tool already drops it
            raise ToolError("this agent may only run the project's own test command; use target to narrow it.")
        if command:
            guard_command(command, ws, approver)
            full = command
        else:
            detected = detect_test_command(ws.root)
            if detected is None:
                # Do not tell a read-only agent to pass `command`: it does not have one.
                hint = "" if read_only else " pass command, e.g. 'python -m pytest -q'."
                raise ToolError(f"could not detect how to run the tests here.{hint}")
            full = f"{detected} {target}".strip()
        output, code, status = run_command(full, ws, max(1, timeout), on_output, stop_event)
        if status != "ok":
            verdict = "TIMED OUT" if status == "timeout" else "STOPPED"
        else:
            verdict = "PASSED" if code == 0 else "FAILED"
        return f"[tests {verdict} · exit code {code}] {summary_line(output)}\n$ {full}\n{truncate_middle(output.rstrip())}"

    properties: dict[str, Any] = {
        "target": {"type": "string", "description": "Test file, directory or test id to run."},
        "timeout": {"type": "integer", "description": f"Seconds before the run is killed (default {DEFAULT_TIMEOUT})."},
    }
    description = ("Run the project's tests and report PASSED/FAILED with the real exit code. The test command "
                   "is detected (pytest, npm test, go test, cargo test); pass target to run only some tests "
                   "(e.g. tests/test_x.py::test_y)")
    if read_only:
        description += "."
    else:
        properties["command"] = {"type": "string",
                                 "description": "Full test command to use instead of the detected one."}
        description += " or command to override it."
    return Tool("RunTests", description, {"type": "object", "properties": properties}, run_tests)
