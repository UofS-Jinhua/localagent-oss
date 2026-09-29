"""M1 acceptance demo: let the agent fix the bugs in examples/buggy_stats.

The example is copied to a temporary git repository with its own venv, the
agent runs with the registry's main model, and pytest then checks the result
independently of what the agent claims.

    .venv/Scripts/python scripts/demo_fix_bug.py [--model NAME] [--no-thinking]
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from rich.console import Console

from localagent.agent import Agent, ChatSession
from localagent.cli.output import prepare_streams
from localagent.cli.render import TerminalRenderer
from localagent.fsutil import remove_tree
from localagent.models.registry import Registry
from localagent.store import Store
from localagent.worktree import merge, repo_status
from localagent.worktree.commit_message import with_tasks, write_message

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "buggy_stats"
TASK = (
    "The test suite of this project fails. Find and fix the bugs in stats.py so that all tests pass. "
    "Do not modify the tests."
)


def venv_python(project: Path) -> Path:
    return project / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def prepare(dest: Path) -> Path:
    """Copy the example into *dest*, make it a git repo, and give it a venv with pytest."""
    project = dest / "buggy_stats"
    shutil.copytree(EXAMPLE, project)
    git = ["git", "-c", "user.name=demo", "-c", "user.email=demo@example.com"]
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run([*git, "add", "."], cwd=project, check=True)
    subprocess.run([*git, "commit", "-qm", "initial"], cwd=project, check=True)
    subprocess.run([sys.executable, "-m", "venv", ".venv"], cwd=project, check=True)
    subprocess.run([str(venv_python(project)), "-m", "pip", "install", "-q", "pytest"], cwd=project, check=True)
    return project


def check(project: Path) -> tuple[bool, str]:
    """Run the tests and confirm the agent left them untouched."""
    tests = subprocess.run([str(venv_python(project)), "-m", "pytest", "-q"], cwd=project,
                           capture_output=True, text=True)
    untouched = subprocess.run(["git", "diff", "--quiet", "--", "tests"], cwd=project).returncode == 0
    report = tests.stdout.strip().splitlines()[-1] if tests.stdout.strip() else tests.stderr
    return tests.returncode == 0 and untouched, f"pytest: {report}; tests untouched: {untouched}"


def run_on_branch(console: Console, registry: Registry, project: Path, tmp: Path, options: dict) -> int:
    """Fix the bugs as a chat on its own branch, then merge it the way `la merge` does."""
    store = Store(tmp / "demo.db")
    session = ChatSession(registry, project, store=store, on_event=TerminalRenderer(console), **options)
    session.run(TASK)
    wt = session.worktree
    untouched = subprocess.run(["git", "status", "--porcelain", "--", "stats.py", "tests"], cwd=project,
                               capture_output=True, text=True).stdout == ""
    console.print(f"[bold]checkout untouched while the agent worked: {untouched}[/]")
    status = repo_status(project)
    console.print(f"status: on {status.branch}; pending {[(p.branch, p.commits, p.files) for p in status.pending]}")
    tasks = [TASK]
    result = merge(wt, lambda diff: with_tasks(write_message(registry, TASK, diff), tasks))
    console.print(f"[bold]merge: {result.status}[/] {result.message}")
    console.print(subprocess.run(["git", "log", "-1", "--format=%B"], cwd=project, capture_output=True,
                                 text=True).stdout)
    ok, report = check(project)
    console.print(f"[bold {'green' if ok else 'red'}]independent check on {status.branch}: {report}[/]")
    store.close()
    return 0 if ok and untouched and result.status == "merged" else 1


def main() -> int:
    prepare_streams()
    parser = argparse.ArgumentParser()
    parser.add_argument("--model")
    parser.add_argument("--no-thinking", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep the temporary project")
    parser.add_argument("--context-budget", type=int,
                        help="pretend the model's context window is this many tokens (forces handoffs)")
    parser.add_argument("--worktree", action="store_true",
                        help="run as a chat on its own branch, check the checkout stays untouched, then merge")
    args = parser.parse_args()
    console = Console()
    registry = Registry.load()
    if args.context_budget:
        spec = registry.resolve(model=args.model).model if args.model else registry.resolve().model
        spec.context_length = args.context_budget
    tmp = Path(tempfile.mkdtemp(prefix="la-demo-"))
    try:
        project = prepare(tmp)
        options = dict(model=args.model, thinking=False if args.no_thinking else None, max_steps=40,
                       max_sessions=6)
        if args.worktree:
            return run_on_branch(console, registry, project, tmp, options)
        agent = Agent(registry, project, on_event=TerminalRenderer(console), **options)
        agent.run(TASK)
        ok, report = check(project)
        console.print(f"[bold {'green' if ok else 'red'}]independent check: {report}[/]")
        console.print(subprocess.run(["git", "diff", "--stat"], cwd=project, capture_output=True, text=True).stdout)
        return 0 if ok else 1
    finally:
        if args.keep:
            console.print(f"project kept at {tmp}")
        elif not remove_tree(tmp):
            console.print(f"[yellow]could not delete {tmp}; remove it by hand[/]")


if __name__ == "__main__":
    sys.exit(main())
