"""End-to-end run against the real model in $LOCALAGENT_HOME/models.yaml (``pytest -m live``)."""

import shutil
import sys
from pathlib import Path

import pytest

from localagent.agent import Agent
from localagent.agent.delegation import make_delegate
from localagent.agent.subtask import MAX_SUBTASK_CONTEXT, make_task_tool
from localagent.models.registry import Registry
from localagent.store import Store
from localagent.tools.workspace import Workspace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import demo_fix_bug  # noqa: E402


@pytest.mark.live
def test_agent_fixes_buggy_stats(tmp_path):
    registry = Registry.load()
    project = demo_fix_bug.prepare(tmp_path)
    result = Agent(registry, project, max_steps=40).run(demo_fix_bug.TASK)
    ok, report = demo_fix_bug.check(project)
    assert ok, f"{report}\nagent said: {result.reason}: {result.summary}"


SUBTASK = (
    "Report every public function in stats.py: its name, its signature, and one line on what it does. "
    "Read the file; do not change anything."
)


@pytest.mark.live
def test_sub_agent_returns_a_report_not_its_tool_output(tmp_path):
    """The Task tool end to end: the sub-agent works in its own window and the caller gets a report."""
    registry = Registry.load()
    project = tmp_path / "buggy_stats"
    shutil.copytree(demo_fix_bug.EXAMPLE, project)
    store = Store(tmp_path / "live.db")
    chat_id = store.new_chat(project)
    parent = store.add_task(chat_id, "live check of the Task tool", "(harness)", project)
    seen: list[dict] = []
    delegate = make_delegate(registry, Workspace(project), store, chat_id, lambda: parent, seen.append)

    report = make_task_tool(delegate, lambda: MAX_SUBTASK_CONTEXT).call(
        {"instructions": SUBTASK, "read_only": True}
    )

    assert all(name in report for name in ("mean", "median", "moving_average")), report
    assert [e["type"] for e in seen] == ["subtask_start", "subtask_finish"]

    children = store.tasks(chat_id, parent_id=parent)
    assert len(children) == 1 and children[0].success, report
    assert [t.id for t in store.tasks(chat_id)] == [parent]  # sub-tasks stay out of the chat's task list

    events = store.events(children[0].id)
    assert {e["context_limit"] for e in events if e["type"] == "usage"} == {MAX_SUBTASK_CONTEXT}
    absorbed = sum(len(e.get("output") or "") for e in events if e["type"] == "tool_result")
    assert absorbed > len(report)  # the tool output stayed in the sub-agent's context
