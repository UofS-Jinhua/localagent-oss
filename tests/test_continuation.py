from localagent.agent import Agent
from localagent.agent.handoff import session_digest
from localagent.agent.loop import handoff_threshold
from localagent.agent.prompts import HANDOFF_REQUEST, build_session_message, capped_progress, relevant_notes

NOTES = "Done: read a.txt, which says hello. Next: change hello to bye in a.txt and verify. " * 2


def run(registry, project, **kwargs):
    events = []
    result = Agent(registry, project, on_event=events.append, **kwargs).run("change hello to bye in a.txt")
    return result, events


def fill_session(llm, notes=NOTES):
    """Script a session that starts small, then crosses the handoff threshold (650 of 1000 tokens)."""
    llm.add_tool_calls([("Glob", {"pattern": "*.txt"})], prompt_tokens=100)
    llm.add_tool_calls([("Glob", {"pattern": "*.py"})], prompt_tokens=700)
    llm.add_text(notes)


def test_handoff_starts_fresh_session_with_notes(llm, registry, project):
    registry.models["test"].context_length = 1000
    (project / "a.txt").write_text("hello\n", encoding="utf-8")
    llm.add_tool_calls([("Read", {"file_path": "a.txt"})], prompt_tokens=100)
    llm.add_tool_calls([("Glob", {"pattern": "*.txt"})], prompt_tokens=700)
    llm.add_text(NOTES)
    llm.add_tool_calls([("Edit", {"file_path": "a.txt", "old_string": "hello", "new_string": "bye"})])
    llm.add_tool_calls([("Read", {"file_path": "a.txt"})])
    llm.add_tool_calls([("Edit", {"file_path": "a.txt", "old_string": "hello", "new_string": "bye"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "a.txt now says bye"})])

    result, events = run(registry, project)

    assert result.success and result.sessions == 2
    assert (project / "a.txt").read_text(encoding="utf-8") == "bye\n"
    first, last_step, handoff, fresh = llm.requests[0], llm.requests[1], llm.requests[2], llm.requests[3]
    # The notes are requested inside the cached conversation, with identical tools and thinking off.
    assert handoff["messages"][-1] == {"role": "user", "content": HANDOFF_REQUEST}
    assert handoff["messages"][: len(last_step["messages"])] == last_step["messages"]
    assert handoff["tools"] == first["tools"] and handoff["reasoning_effort"] == "none"
    # The next session starts from scratch but with the same system prompt (reusable prefix).
    assert len(fresh["messages"]) == 2 and fresh["messages"][0] == first["messages"][0]
    opening = fresh["messages"][1]["content"]
    assert NOTES.strip()[:40] in opening and "# Continue" in opening and "# Task (work on it now)" in opening
    # Reads do not carry over: the first Edit of session 2 was refused.
    edits = [e for e in events if e["type"] == "tool_result" and e["name"] == "Edit"]
    assert edits[0]["is_error"] and "Read it before" in edits[0]["output"]
    assert [e["session"] for e in events if e["type"] == "continuation"] == [2]
    assert [e["source"] for e in events if e["type"] == "handoff"] == ["model"]


def test_session_that_starts_large_still_gets_room(llm, registry, project):
    registry.models["test"].context_length = 1000
    llm.add_tool_calls([("Glob", {"pattern": "*"})], prompt_tokens=640)  # starts near the 650 mark
    llm.add_tool_calls([("Glob", {"pattern": "*"})], prompt_tokens=700)  # below 640 + 250
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})], prompt_tokens=800)
    result, _ = run(registry, project)
    assert result.success and result.sessions == 1


def test_context_overflow_falls_back_to_digest(llm, registry, project):
    llm.add_tool_calls([("Glob", {"pattern": "*.txt"})])
    llm.add_error(400, "request (9000 tokens) exceeds the available context size (8192 tokens)")
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])
    result, events = run(registry, project)
    assert result.success and result.sessions == 2
    opening = llm.requests[2]["messages"][1]["content"]
    assert "Glob(pattern=*.txt)" in opening and "ran out of context" in opening
    assert [e["source"] for e in events if e["type"] == "handoff"] == ["digest"]


def test_short_notes_are_replaced_by_digest(llm, registry, project):
    registry.models["test"].context_length = 1000
    fill_session(llm, notes="ok")
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])
    _, events = run(registry, project)
    assert "Glob(pattern=*.py)" in llm.requests[3]["messages"][1]["content"]
    assert [e["source"] for e in events if e["type"] == "handoff"] == ["digest"]


def test_only_the_newest_model_notes_are_carried(llm, registry, project):
    registry.models["test"].context_length = 1000
    fill_session(llm, notes="FIRST NOTES " * 10)
    fill_session(llm, notes="SECOND NOTES, which recap the first session too. " * 3)
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])
    result, _ = run(registry, project)
    assert result.sessions == 3
    opening = llm.requests[6]["messages"][1]["content"]
    assert "SECOND NOTES" in opening and "FIRST NOTES" not in opening
    assert "# Progress so far (continuation 2)" in opening and "### Notes from session 2" in opening


def test_sessions_are_bounded(llm, registry, project):
    registry.models["test"].context_length = 1000
    fill_session(llm)
    fill_session(llm)
    result, _ = run(registry, project, max_sessions=2)
    assert result.reason == "sessions_exhausted" and not result.success and result.sessions == 2
    assert NOTES.strip()[:40] in result.summary


def test_handoff_threshold():
    assert handoff_threshold(1000, 0.65, 100) == 650
    assert handoff_threshold(1000, 0.65, 600) == 850
    assert handoff_threshold(1000, 0.65, 800) == 900
    assert handoff_threshold(122880, 0.65, 3000) == int(0.65 * 122880)


def test_relevant_notes():
    notes = [(1, "a", "model"), (2, "b", "model"), (3, "c", "digest")]
    assert relevant_notes(notes) == [(2, "b"), (3, "c")]
    assert relevant_notes([(1, "x", "digest"), (2, "y", "digest")]) == [(1, "x"), (2, "y")]
    assert relevant_notes([]) == []


def test_digest_lists_calls_and_outcomes():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": '{"command": "pytest -q"}'}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "F.\n1 failed, 1 passed\n[exit code 1]\n\n[step 1/100 · context 900/8,192 tokens]"},
        {"role": "assistant", "content": "The test fails because of an off-by-one."},
    ]
    digest = session_digest(messages)
    assert "- Bash(command=pytest -q) -> F. … [exit code 1]" in digest
    assert "off-by-one" in digest and "[step" not in digest


def test_progress_keeps_newest_notes():
    text = capped_progress([(1, "a" * 100), (2, "b" * 100), (3, "c" * 100)], limit=250)
    assert "(1 earlier session notes omitted.)" in text and "b" * 100 in text and "c" * 100 in text


def test_session_message_is_plain_task_without_context():
    assert build_session_message("fix it") == "fix it"
    assert build_session_message("fix it", chat_context="## Earlier").startswith("## Earlier\n\n# Task")
