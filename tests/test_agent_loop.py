import re
import shlex
import sys
from pathlib import Path

import pytest

from localagent.agent import Agent
from localagent.models.thinking import CHARS_PER_TOKEN

# Not a bare `python`: many Linux servers only have python3, and commands never see LocalAgent's venv.
PYTHON = shlex.quote(Path(sys.executable).as_posix())


def run(registry, project, task="fix it", **kwargs):
    events = []
    agent = Agent(registry, project, on_event=events.append, **kwargs)
    return agent.run(task), events


def assert_append_only(requests):
    """Each request must extend the previous one; editing history would defeat the prompt cache."""
    for before, after in zip(requests, requests[1:]):
        assert after["messages"][: len(before["messages"])] == before["messages"]


def test_fix_bug_end_to_end(llm, registry, project):
    (project / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    llm.add_tool_calls([("Read", {"file_path": "calc.py"})])
    llm.add_tool_calls([("Edit", {"file_path": "calc.py", "old_string": "a - b", "new_string": "a + b"})])
    llm.add_tool_calls([("Bash", {"command": f'{PYTHON} -c "import calc; print(calc.add(2, 3))"'})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "Fixed add(); add(2, 3) now prints 5."})])

    result, events = run(registry, project)

    assert result.success and result.reason == "finished" and result.steps == 4
    assert "a + b" in (project / "calc.py").read_text(encoding="utf-8")
    bash_result = next(e for e in events if e["type"] == "tool_result" and e["name"] == "Bash")
    assert "5" in bash_result["output"] and "[exit code 0]" in bash_result["output"]
    assert_append_only(llm.requests)
    last = llm.requests[-1]["messages"]
    assert last[0]["role"] == "system" and str(project.resolve()) in last[0]["content"]
    assert last[-1]["role"] == "tool" and "[step 3/100 · context" in last[-1]["content"]


def test_status_line_only_on_last_result_of_a_turn(llm, registry, project):
    (project / "a.txt").write_text("a\n", encoding="utf-8")
    llm.add_tool_calls([("Read", {"file_path": "a.txt"}), ("Glob", {"pattern": "*.txt"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])
    run(registry, project)
    tool_messages = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert "[step" not in tool_messages[0]["content"] and "[step 1/" in tool_messages[1]["content"]


def test_no_tool_call_is_nudged_then_finishes_implicitly(llm, registry, project):
    llm.add_text("I think the answer is 42.")
    llm.add_text("The answer is 42.")
    result, _ = run(registry, project)
    assert result.reason == "implicit_finish" and result.summary == "The answer is 42."
    assert "must call at least one tool" in llm.requests[1]["messages"][-1]["content"]


def test_bad_tool_calls_are_reported_to_the_model(llm, registry, project):
    llm.add_tool_calls([("Delete", {"path": "x"})])
    llm.add_tool_calls([("Read", {})])
    llm.add_tool_calls([("finish", {"success": False, "summary": "gave up"})])
    result, _ = run(registry, project)
    assert not result.success and result.reason == "finished"
    tool_messages = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert "unknown tool 'Delete'" in tool_messages[0]
    assert "missing required argument(s) file_path" in tool_messages[1]


def test_finish_without_summary_is_rejected(llm, registry, project):
    llm.add_tool_calls([("finish", {"success": True})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "ok"})])
    result, _ = run(registry, project)
    assert result.success and result.steps == 2


def test_transient_errors_are_retried(llm, registry, project, monkeypatch):
    monkeypatch.setattr("localagent.agent.loop.MAX_RETRIES", 2)
    llm.add_error(503, "loading model")
    llm.add_tool_calls([("finish", {"success": True, "summary": "ok"})])
    result, events = run(registry, project)
    assert result.success
    assert [e["attempt"] for e in events if e["type"] == "retry"] == [1]


def test_step_limit_ends_with_notes_to_continue_from(llm, registry, project):
    """The work is real even though the task is not done; the summary is what the next task starts from."""
    llm.add_tool_calls([("Glob", {"pattern": "*"})])
    llm.add_tool_calls([("Glob", {"pattern": "*"})])
    llm.add_text("Wrote index.html and src/main.js; the build passes. Next: the waterfall still falls "
                 "through the terrain, fix the height lookup in carve() and run the build again.")

    result, events = run(registry, project, max_steps=2)

    assert result.reason == "max_steps" and result.steps == 2
    assert "step limit (2)" in result.summary and "fix the height lookup" in result.summary
    assert any(e["type"] == "notice" and "step limit was reached" in e["text"] for e in events)


def test_a_step_whose_thinking_was_cut_off_does_not_use_up_the_step_budget(llm, registry, project):
    """Retrying one step is not progress through the task, and must not eat the steps left for it."""
    registry.models["test"].max_thinking_tokens = 3_000
    llm.add_long_think(10_000)  # step 1, stopped and tried again
    llm.add_tool_calls([("Glob", {"pattern": "*"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])

    result, events = run(registry, project, max_steps=2)

    assert result.success and result.steps == 2
    assert [e["step"] for e in events if e["type"] == "step_start"] == [1, 2]


def test_thinking_can_be_disabled_per_run(llm, registry, project):
    llm.add_tool_calls([("finish", {"success": True, "summary": "ok"})])
    run(registry, project, thinking=False)
    assert llm.requests[0]["reasoning_effort"] == "none"


def test_a_zero_session_budget_ends_cleanly(llm, registry, project):
    """max_sessions=0 must report why it stopped instead of crashing on missing notes."""
    result, _ = run(registry, project, max_sessions=0)
    assert not result.success and result.reason == "sessions_exhausted"
    assert "without finishing" in result.summary and result.sessions == 0


def test_cost_follows_the_model_that_ran_each_step(llm, registry, project):
    """Token counts are per step; a priced model must add up, and a free one must stay at zero."""
    registry.models["test"].price_input = 3.0  # $/M tokens
    registry.models["test"].price_output = 15.0
    llm.add_tool_calls([("Glob", {"pattern": "*.py"})], prompt_tokens=1000)  # + 20 completion tokens
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})], prompt_tokens=2000)
    result, events = run(registry, project)
    steps = [e for e in events if e["type"] == "usage"]
    assert steps[0]["cost"] == pytest.approx((1000 * 3 + 20 * 15) / 1e6)
    assert result.cost == pytest.approx(sum(e["cost"] for e in steps)) and result.cost > 0

    registry.models["test"].price_input = registry.models["test"].price_output = 0.0
    llm.add_tool_calls([("finish", {"success": True, "summary": "free"})])
    assert run(registry, project)[0].cost == 0.0


POINTING = "通读了论文（43页），对5层架构逐层给出评价：总体是务实的分层设计。详见回复正文。"
ANSWER = "逐层评价：\n1. KISSAgent：ReAct 循环很朴素，但预算追踪做得扎实。\n2. Relentless：交接笔记是最薄弱的一环。"


def test_a_finish_that_points_to_a_reply_that_does_not_exist_is_sent_back(llm, registry, project):
    """Seen live: the analysis was drafted in thinking (never shown), finish only said "see the reply body"."""
    from localagent.agent import Agent

    llm.add_tool_calls([("finish", {"success": True, "summary": POINTING})])
    llm.add_tool_calls([("finish", {"success": True, "summary": ANSWER})])
    agent = Agent(registry, project)

    result = agent.run("这篇论文的5个layer的观点你觉得如何呢?")

    assert result.success and result.summary == ANSWER
    refusal = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"][-1]
    assert "only thing the user sees" in refusal and "详见" in refusal
    agent.close()


def test_a_model_that_insists_is_believed_the_second_time(llm, registry, project):
    from localagent.agent import Agent

    llm.add_tool_calls([("finish", {"success": True, "summary": POINTING})])
    llm.add_tool_calls([("finish", {"success": True, "summary": POINTING})])
    agent = Agent(registry, project)
    assert agent.run("这篇论文你觉得如何?").summary == POINTING and len(llm.requests) == 2
    agent.close()


def test_an_ordinary_summary_finishes_at_once(llm, registry, project):
    from localagent.agent import Agent

    llm.add_tool_calls([("finish", {"success": True, "summary": "Fixed the off-by-one in stats.py; 6 tests pass."})])
    agent = Agent(registry, project)
    assert agent.run("fix the bug").success and len(llm.requests) == 1
    agent.close()


def test_pointing_to_text_the_model_did_write_is_fine(llm, registry, project):
    """Some models answer in the message and finish with a short note; the page shows that message."""
    from localagent.agent import Agent

    llm.add_tool_calls([("finish", {"success": True, "summary": "See above for the full review."})],
                       content="Here is my review of the five layers: ...")
    agent = Agent(registry, project)
    assert agent.run("review the paper").success and len(llm.requests) == 1
    agent.close()


def test_the_finish_tool_says_the_summary_is_the_whole_reply():
    from localagent.agent.prompts import FINISH_TOOL, build_system_prompt

    summary = FINISH_TOOL["function"]["parameters"]["properties"]["summary"]["description"]
    assert "complete answer" in summary and "question" in summary
    assert "never point to one" in build_system_prompt(__import__("pathlib").Path("."))


def test_a_reply_cut_off_by_the_output_limit_thinks_again_on_half_the_budget(llm, registry, project):
    """A thinking model can fill the whole output budget and call nothing; the page must say so, not just show it.

    The retry keeps thinking -- a step without it is a worse step -- but on half the budget, so a second
    runaway costs half as much before thinking does go away.
    """
    llm.add_cut_off("thinking on and on" * 50)
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])

    result, events = run(registry, project)

    assert result.success
    notice = next(e for e in events if e["type"] == "notice" and "output limit" in e["text"])
    assert "16,384 tokens" in notice["text"] and "half that budget (8,192 tokens)" in notice["text"]
    assert llm.requests[0]["max_tokens"] == 16384 and llm.requests[0].get("reasoning_effort") is None
    assert llm.requests[1]["max_tokens"] == 8192 and llm.requests[1].get("reasoning_effort") is None
    assert "cut off" in llm.requests[1]["messages"][-1]["content"]


def test_a_second_cut_off_in_a_row_turns_thinking_off(llm, registry, project):
    for _ in range(2):
        llm.add_cut_off("thinking on and on" * 50)
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])

    result, events = run(registry, project)

    assert result.success
    assert [e["text"] for e in events if e["type"] == "notice"][-1].endswith("with thinking off.")
    assert llm.requests[2]["reasoning_effort"] == "none" and llm.requests[2]["max_tokens"] == 8192


def test_thinking_that_never_leaves_room_for_a_tool_call_says_what_to_change(llm, registry, project):
    for _ in range(4):
        llm.add_cut_off("thinking on and on" * 50)

    result, _ = run(registry, project)

    assert not result.success
    assert "max_output_tokens" in result.summary and "with thinking off as well" in result.summary


def test_the_first_step_of_a_session_may_think_longer(llm, registry, project):
    """Planning the job is where a thinking model legitimately thinks longest; carrying it out is not."""
    registry.models["test"].max_output_tokens_first = 24576
    llm.add_tool_calls([("Read", {"file_path": "a.txt"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "read it"})])
    (project / "a.txt").write_text("hello\n", encoding="utf-8")

    run(registry, project)

    assert [r["max_tokens"] for r in llm.requests] == [24576, 16384]


def test_thinking_that_goes_in_circles_is_stopped_where_it_starts_repeating(llm, registry, project):
    """Waiting for the output limit costs the whole budget -- half an hour on a local model -- for nothing."""
    llm.add_loop("Checking how the values flow through the helper, and whether the helper is even reached. ")
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])

    result, events = run(registry, project)

    assert result.success
    notice = next(e for e in events if e["type"] == "notice" and "circles" in e["text"])
    assert "400-character passage" in notice["text"]
    # The whole 300-chunk reply was never read: the stream was dropped once the loop was clear.
    thinking = next(e for e in events if e["type"] == "assistant")["reasoning"]
    assert 8_000 <= len(thinking) < 20_000
    # The output budget is untouched -- it holds the files this step may still write -- but the retry
    # may only think half as long as the stopped reply managed.
    assert llm.requests[1]["max_tokens"] == 16384
    thought = int(re.search(r"after ([\d,]+) tokens of thinking", notice["text"])[1].replace(",", ""))
    assert thought == len(thinking) // CHARS_PER_TOKEN  # chunks of a hundred characters are several tokens
    assert f"half the thinking ({thought // 2:,} tokens)" in notice["text"]
    assert "in circles" in llm.requests[1]["messages"][-1]["content"]


def test_thinking_stops_at_the_model_bound_long_before_the_output_limit(llm, registry, project):
    """The root fix: a step's output budget holds a whole file, its thinking does not get to use that room."""
    registry.models["test"].max_thinking_tokens = 3_000
    llm.add_long_think(10_000)  # would have run to the output limit
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])

    result, events = run(registry, project)

    assert result.success
    thinking = next(e for e in events if e["type"] == "assistant")["reasoning"]
    assert len(thinking) == 3_000 * 3  # stopped on the 3,000th token, not at a character count
    notice = next(e for e in events if e["type"] == "notice")
    assert "thinking limit of 3,000 tokens" in notice["text"]
    assert "half the thinking (1,500 tokens)" in notice["text"]
    assert llm.requests[1]["max_tokens"] == 16384  # unchanged: the thinking was the problem, not the room
    assert "ran past its limit" in llm.requests[1]["messages"][-1]["content"]


def test_a_second_overlong_think_in_a_row_turns_thinking_off(llm, registry, project):
    registry.models["test"].max_thinking_tokens = 3_000
    for _ in range(2):
        llm.add_long_think(10_000)
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])

    result, events = run(registry, project)

    assert result.success
    assert [e["text"] for e in events if e["type"] == "notice"][-1].endswith("with thinking off.")
    assert llm.requests[2]["reasoning_effort"] == "none"
