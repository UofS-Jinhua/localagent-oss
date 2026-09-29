import json
import shlex
import sys
from pathlib import Path

import pytest

from localagent.tools import ToolError, Workspace
from localagent.tools.testing import detect_test_command, make_test_tool, summary_line

# Not a bare `python`: many Linux servers only have python3, and commands never see LocalAgent's venv.
PYTHON = shlex.quote(Path(sys.executable).as_posix())


def tool(project):
    return make_test_tool(Workspace(project))


def test_detects_common_runners(project):
    assert detect_test_command(project) is None
    (project / "tests").mkdir()
    (project / "tests" / "test_a.py").write_text("", encoding="utf-8")
    assert detect_test_command(project).startswith("python -m pytest")


@pytest.mark.parametrize("name, content, expected", [
    ("package.json", json.dumps({"scripts": {"test": "jest"}}), "npm test --silent"),
    ("go.mod", "module x", "go test ./..."),
    ("Cargo.toml", "[package]", "cargo test"),
])
def test_detects_other_ecosystems(project, name, content, expected):
    (project / name).write_text(content, encoding="utf-8")
    assert detect_test_command(project) == expected


def test_summary_line_prefers_pytest_tally():
    output = "....F\n=== FAILURES ===\nboom\n==== 1 failed, 4 passed in 0.1s ====\n"
    assert summary_line(output) == "1 failed, 4 passed in 0.1s"
    assert summary_line("ok\nall good\n") == "all good"


def test_reports_pass_and_fail_with_real_exit_code(project):
    passing = tool(project).call({"command": f"{PYTHON} -c \"print('3 passed in 0.01s')\""})
    assert passing.startswith("[tests PASSED · exit code 0] 3 passed in 0.01s")
    failing = tool(project).call({"command": f"{PYTHON} -c \"import sys; print('1 failed'); sys.exit(1)\""})
    assert failing.startswith("[tests FAILED · exit code 1] 1 failed")


def test_undetectable_project_asks_for_a_command(project):
    with pytest.raises(ToolError, match="could not detect"):
        tool(project).call({})


def test_custom_command_is_guarded(project):
    with pytest.raises(ToolError, match="leaves the working directory"):
        tool(project).call({"command": "cd .. && pytest"})


def test_a_read_only_agent_gets_no_command_argument(project):
    """`command` would be a shell; the reviewer and read-only sub-agents must not get one that way."""
    read_only = make_test_tool(Workspace(project), read_only=True)
    assert "command" not in read_only.parameters["properties"]
    assert "command to override it" not in read_only.description  # not advertised either
    with pytest.raises(ToolError, match="could not detect|own test command"):
        read_only.call({"command": "git show HEAD:app.py"})  # not run, whichever way it is refused

    (project / "tests").mkdir()
    (project / "tests" / "test_a.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    assert "[tests " in read_only.call({})  # the project's own tests still run
    smuggled = read_only.call({"command": "git show HEAD:app.py", "target": "tests"})
    assert "$ python -m pytest" in smuggled and "git show" not in smuggled
