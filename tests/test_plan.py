"""The Plan tool: a plan made in a tool call outlives the thinking that made it."""

import json

import pytest

from localagent.agent import Agent, ChatSession
from localagent.agent.chat import STALE_STATUSES, format_history
from localagent.agent.prompts import OUTLINE_RULE, PLAN_RULE
from localagent.store import Store, TaskRecord
from localagent.tools import ToolError
from localagent.tools.plan import make_plan_tool, parse_plan

STEPS = [{"step": "index.html + main.js skeleton; check: npm run build", "status": "in_progress"},
         {"step": "terrain.js: voxel heightmap; check: screenshot", "status": "pending"}]
NOTES = "Heightmap 96x96 from 3 octaves of value noise; waterfall follows steepest descent from the peak."


def test_a_plan_is_read_the_way_models_write_it():
    todo_write = parse_plan([{"content": "write the skeleton", "status": "done"}, {"content": "add terrain"}])
    assert todo_write.steps == [("write the skeleton", "completed"), ("add terrain", "pending")]
    strings = parse_plan(["- [x] skeleton", "- [>] terrain", "waterfall"])
    assert strings.steps == [("skeleton", "completed"), ("terrain", "in_progress"), ("waterfall", "pending")]
    as_json = parse_plan(json.dumps(STEPS), NOTES)
    assert [s for s, _ in as_json.steps] == [s["step"] for s in STEPS] and as_json.notes == NOTES
    assert parse_plan("1. skeleton\n2. terrain").steps[1] == ("2. terrain", "pending")
    assert parse_plan([{"step": "x", "status": "In Progress"}]).steps == [("x", "in_progress")]
    assert parse_plan([{"step": "x", "status": "blocked"}]).steps == [("x", "pending")]
    for empty in ([], "", [{"status": "pending"}], ["  "]):
        with pytest.raises(ToolError, match="no steps"):
            parse_plan(empty)


def test_the_tool_records_the_plan_and_points_to_the_next_step():
    recorded = []
    tool = make_plan_tool(recorded.append)
    assert tool.call({"steps": STEPS, "notes": NOTES}).endswith(f"0 of 2 steps completed. Next: {STEPS[0]['step']}")
    assert recorded[0].render() == (f"- [>] {STEPS[0]['step']}\n- [ ] {STEPS[1]['step']}\n\nNotes:\n{NOTES}")
    done = [{**s, "status": "completed"} for s in STEPS]
    assert tool.call({"steps": done}) == "Plan recorded: all 2 steps completed."


def test_a_plan_stays_in_the_conversation_the_next_steps_see(llm, registry, project):
    """Thinking is not sent back; the plan is, as the Plan call's own arguments."""
    llm.add_tool_calls([("Plan", {"steps": STEPS, "notes": NOTES})])
    llm.add_tool_calls([("Write", {"file_path": "index.html", "content": "<canvas></canvas>\n"})])
    llm.add_tool_calls([("Plan", {"steps": [{**STEPS[0], "status": "completed"}, STEPS[1]], "notes": NOTES})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "skeleton written"})])
    events = []

    result = Agent(registry, project, on_event=events.append).run("build a voxel landscape")

    first = llm.requests[0]
    assert "Plan" in [t["function"]["name"] for t in first["tools"]]
    assert PLAN_RULE in first["messages"][0]["content"] and OUTLINE_RULE not in first["messages"][0]["content"]
    later = json.dumps(llm.requests[2]["messages"], ensure_ascii=False)
    assert NOTES in later and "0 of 2 steps completed. Next: index.html" in later
    plans = [e for e in events if e["type"] == "plan"]
    calls = [e["id"] for e in events if e["type"] == "tool_call" and e["name"] == "Plan"]
    assert [p["id"] for p in plans] == calls and plans[1]["steps"][0]["status"] == "completed"
    assert result.plan.startswith(f"- [x] {STEPS[0]['step']}\n- [ ] {STEPS[1]['step']}")


def test_the_status_line_counts_the_completed_steps_of_the_plan(llm, registry, project):
    """Measured: three runs recorded a plan once and never marked a step done; now every step shows the count."""
    from localagent.agent.handoff import session_digest

    llm.add_tool_calls([("Glob", {"pattern": "*"})])
    llm.add_tool_calls([("Plan", {"steps": STEPS})])
    llm.add_tool_calls([("Plan", {"steps": [{**STEPS[0], "status": "completed"}, STEPS[1]]})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])

    Agent(registry, project).run("build a voxel landscape")

    results = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert results[0].endswith("tokens]")  # no plan yet, nothing to count
    assert results[1].endswith("tokens · plan 0/2 done]") and results[2].endswith("tokens · plan 1/2 done]")
    digest = session_digest(llm.requests[-1]["messages"])  # the mechanical handoff still drops the status line
    assert "Plan recorded: 1 of 2 steps completed" in digest and "plan 1/2 done" not in digest


def test_an_agent_that_only_reads_has_no_plan_to_make(llm, registry, project):
    llm.add_tool_calls([("finish", {"success": True, "summary": "read it"})])
    Agent(registry, project, read_only=True).run("what does a.txt say?")
    assert "Plan" not in [t["function"]["name"] for t in llm.requests[0]["tools"]]
    assert OUTLINE_RULE in llm.requests[0]["messages"][0]["content"]


def test_a_fresh_session_starts_from_the_plan(llm, registry, project):
    registry.models["test"].context_length = 1000  # hands off past 650 tokens
    llm.add_tool_calls([("Plan", {"steps": STEPS, "notes": NOTES})], prompt_tokens=100)
    llm.add_tool_calls([("Glob", {"pattern": "*"})], prompt_tokens=700)
    llm.add_text("Done: planned the scene. Next: the skeleton, as the plan says. " * 2)
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])

    result = Agent(registry, project).run("build a voxel landscape")

    assert result.sessions == 2
    opening = llm.requests[3]["messages"][1]["content"]
    assert "# Plan so far" in opening and f"- [>] {STEPS[0]['step']}" in opening and NOTES in opening
    assert "statuses may be out of date" in opening and "handoff notes and the files" in opening
    assert opening.index("# Plan so far") < opening.index("# Continue")


def test_thinking_stopped_at_its_limit_is_told_to_keep_what_it_decided_in_a_plan(llm, registry, project):
    registry.models["test"].max_thinking_tokens = 3_000
    llm.add_long_think(10_000)
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])

    Agent(registry, project).run("build a voxel landscape")

    assert "record it with Plan" in llm.requests[1]["messages"][-1]["content"]


def plan_record(n: int, success: bool | None, plan: str) -> TaskRecord:
    return TaskRecord(f"t{n}", "c", float(n), float(n), f"task {n}", "result", success, "finished",
                      "m", ".", 1, 1, 10, 1.0, plan=plan)


def test_only_an_unfinished_task_hands_its_plan_to_the_next_one():
    text = format_history([plan_record(1, True, "- [x] all of it"), plan_record(2, False, "- [ ] the rest"),
                           plan_record(3, None, "- [>] half")])
    assert "Plan 1" not in text and "all of it" not in text
    # Models seldom mark steps done after recording them, so the next task is told not to trust the ticks.
    assert f"### Plan 2 when it stopped ({STALE_STATUSES})\n- [ ] the rest" in text
    assert "### Plan 3 when it stopped" in text


def test_trying_again_starts_from_the_plan_of_the_task_that_stopped(llm, registry, project, tmp_path):
    """Seen live: after an error, "try again" designed the whole project again for 16 minutes."""
    store = Store(tmp_path / "chat.db")
    session = ChatSession(registry, project, store=store, isolate=False)
    llm.add_tool_calls([("Plan", {"steps": STEPS, "notes": NOTES})])
    llm.add_tool_calls([("finish", {"success": False, "summary": "the model server went away"})])
    session.run("build a voxel landscape")
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])
    session.run("try again")

    assert store.tasks(session.chat_id)[0].plan.startswith(f"- [>] {STEPS[0]['step']}")
    opening = llm.requests[2]["messages"][1]["content"]
    assert "### Plan 1 when it stopped" in opening and NOTES in opening
    store.close()
