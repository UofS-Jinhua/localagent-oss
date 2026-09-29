"""Sub-agents: a context filter with a budget, and a transcript you can go back to."""

import pytest

from localagent.agent import ChatSession
from localagent.agent.loop import MIN_SESSION_ROOM, Agent
from localagent.agent.subtask import MAX_SUBTASK_CONTEXT, MIN_SUBTASK_CONTEXT, make_task_tool
from localagent.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "subtask.db")
    yield s
    s.close()


@pytest.fixture
def registry(llm):
    """A model with a realistic local window: a sub-agent is refused below 8K of free context."""
    from localagent.models.registry import ModelSpec, Provider, Registry, RoleSpec

    return Registry(
        providers={"scripted": Provider(name="scripted", base_url=llm.base_url)},
        models={"test": ModelSpec(name="test", provider="scripted", model_id="test-model", context_length=120_000)},
        roles={"main": RoleSpec(model="test", thinking=True)},
    )


def test_the_budget_is_what_is_left_of_the_window(llm, registry, project):
    """Both conversations share the server's KV pool, so a sub-agent only gets the remainder."""
    agent = Agent(registry, project, delegate=lambda *a: "", context_limit=40_000)
    agent._context = 10_000
    reserve = int(MIN_SESSION_ROOM * 40_000)  # the caller keeps room to carry on
    assert agent._delegate_budget() == 40_000 - 10_000 - reserve
    agent._context = 39_000  # nearly full: nothing to lend
    assert agent._delegate_budget() < MIN_SUBTASK_CONTEXT
    agent.close()


def test_a_sub_agent_is_refused_when_there_is_no_room():
    called = []
    tool = make_task_tool(lambda *a: called.append(a) or "ran", budget=lambda: 2_000)
    assert "not enough context left" in tool.call({"instructions": "look around"})
    assert not called


def test_the_budget_is_capped_even_in_a_huge_window():
    given = []
    tool = make_task_tool(lambda i, r, b: given.append(b) or "ok", budget=lambda: 900_000)
    tool.call({"instructions": "look around"})
    assert given == [MAX_SUBTASK_CONTEXT]


def test_the_caller_sees_only_the_report_and_the_steps_are_kept(llm, registry, project, store):
    llm.add_tool_calls([("Task", {"instructions": "Find where the greeting lives and report the file and line."})])
    # the sub-agent's own conversation:
    llm.add_tool_calls([("Grep", {"pattern": "hello"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "greeting.py line 2 returns 'hello'."})])
    # back in the caller:
    llm.add_tool_calls([("finish", {"success": True, "summary": "It is in greeting.py."})])
    (project / "greeting.py").write_text("def greet():\n    return 'hello'\n", encoding="utf-8")

    events = []
    chat = ChatSession(registry, project, store=store, isolate=False, on_event=events.append)
    result = chat.run("where is the greeting?")

    assert result.success
    started = next(e for e in events if e["type"] == "subtask_start")
    finished = next(e for e in events if e["type"] == "subtask_finish")
    assert finished["success"] and finished["steps"] == 2 and started["read_only"] is True

    # The caller's context got the report, not the sub-agent's Grep output.
    caller_tool_results = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert "greeting.py line 2" in caller_tool_results[0]
    assert "def greet" not in "".join(caller_tool_results)

    # The sub-agent's run is its own task, hanging off the caller's, with its steps kept.
    [parent] = store.tasks(chat.chat_id)
    [child] = store.tasks(chat.chat_id, parent_id=parent.id)
    assert child.parent_id == parent.id and child.steps == 2
    assert "Grep" in str(store.events(child.id))
    assert store.chat(chat.chat_id).task_count == 1  # the sub-task is not a task of the chat


def test_a_sub_agent_cannot_spawn_sub_agents_or_edit_when_read_only(llm, registry, project, store):
    llm.add_tool_calls([("Task", {"instructions": "look at the project", "read_only": True})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "nothing to report"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])

    tools_seen = []
    chat = ChatSession(registry, project, store=store, isolate=False)
    chat.run("have a look")
    for request in llm.requests:
        tools_seen.append({t["function"]["name"] for t in request.get("tools", [])})
    caller, sub = tools_seen[0], tools_seen[1]
    assert "Task" in caller and "Write" in caller
    assert "Task" not in sub and "Write" not in sub and "Edit" not in sub and "Bash" not in sub
    assert {"Read", "Grep", "Glob", "RunTests"} <= sub


def test_a_failing_sub_agent_does_not_end_the_task(llm, registry, project, store):
    llm.add_tool_calls([("Task", {"instructions": "count the files"})])
    llm.add_error(500, "the server fell over")
    llm.add_error(500, "the server fell over")
    llm.add_error(500, "the server fell over")
    llm.add_tool_calls([("finish", {"success": True, "summary": "counted them myself"})])

    chat = ChatSession(registry, project, store=store, isolate=False)
    result = chat.run("count the files")
    assert result.success
    report = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"][0]
    assert "sub-agent" in report.lower()


def test_a_sub_task_does_not_make_the_caller_forget_what_it_read(llm, registry, project, store):
    """A session starts by forgetting its reads; the sub-agent must not clear the caller's."""
    (project / "greeting.py").write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    llm.add_tool_calls([("Read", {"file_path": "greeting.py"})])
    llm.add_tool_calls([("Task", {"instructions": "Does anything else greet? Report the files."})])
    llm.add_tool_calls([("Grep", {"pattern": "hello"})])  # the sub-agent
    llm.add_tool_calls([("finish", {"success": True, "summary": "only greeting.py greets."})])
    # back in the caller: the Read from before the sub-task still counts
    llm.add_tool_calls([("Edit", {"file_path": "greeting.py", "old_string": "'hello'", "new_string": "'hi'"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "greeting changed to hi."})])

    chat = ChatSession(registry, project, store=store, isolate=False)
    result = chat.run("make it say hi instead")

    assert result.success
    assert "'hi'" in (project / "greeting.py").read_text(encoding="utf-8")
    edit = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"][-1]
    assert "Read it before modifying" not in edit


def test_a_model_on_a_server_with_a_big_kv_pool_gives_sub_agents_more_room(llm, registry, project):
    """32K suits a server that holds one conversation; an A100 server holds 524K across its slots."""
    default = Agent(registry, project, delegate=lambda instructions, read_only, budget: str(budget))
    assert default.tools["Task"].call({"instructions": "look around"}) == str(MAX_SUBTASK_CONTEXT)
    default.close()

    registry.models["test"].max_subtask_context = 64_000
    roomy = Agent(registry, project, delegate=lambda instructions, read_only, budget: str(budget))
    assert roomy.tools["Task"].call({"instructions": "look around"}) == "64000"
    roomy._context = 50_000  # what is left of the window still binds: 120K - 50K - 25% reserve
    assert roomy.tools["Task"].call({"instructions": "look around"}) == str(120_000 - 50_000 - 30_000)
    roomy.close()


def test_the_cap_never_goes_below_what_a_sub_agent_needs():
    given = []
    tool = make_task_tool(lambda i, r, b: given.append(b) or "ok", budget=lambda: 50_000, cap=lambda: 1_000)
    tool.call({"instructions": "look around"})
    assert given == [MIN_SUBTASK_CONTEXT]


class Stopwatch:
    """A real delegate that takes a while and records when each sub-agent ran and what room it got."""

    def __init__(self, seconds: float = 0.6):
        self.seconds = seconds
        self.runs: list[tuple[str, float, float, int, bool]] = []
        self._lock = __import__("threading").Lock()

    def __call__(self, instructions: str, read_only: bool, budget: int) -> str:
        import time

        started = time.monotonic()
        time.sleep(self.seconds)
        with self._lock:
            self.runs.append((instructions, started, time.monotonic(), budget, read_only))
        return f"report on {instructions}"

    def overlapping(self) -> bool:
        runs = sorted(self.runs, key=lambda r: r[1])
        return any(later[1] < earlier[2] for earlier, later in zip(runs, runs[1:]))


def three_reads(llm, extra=None):
    calls = [("Task", {"instructions": f"part {n}"}) for n in (1, 2, 3)]
    llm.add_tool_calls(calls + (extra or []))
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])


def test_on_a_model_that_allows_it_read_only_sub_agents_of_one_turn_run_together(llm, registry, project):
    """An A100 server serves four requests at 1.8x the throughput of one, so three reads need not queue."""
    import time

    registry.models["test"].max_parallel_subtasks = 3
    three_reads(llm)
    watch = Stopwatch()
    agent = Agent(registry, project, delegate=watch)
    started = time.monotonic()
    assert agent.run("look at three things").success
    elapsed = time.monotonic() - started

    assert len(watch.runs) == 3 and watch.overlapping() and elapsed < 3 * watch.seconds
    # They split the room one sub-agent would get alone, so all of them still fit the pool together.
    alone = 120_000 - (100 + 20) - int(0.25 * 120_000)  # window - the context so far (prompt + reply) - reserve
    assert {run[3] for run in watch.runs} == {min(alone // 3, MAX_SUBTASK_CONTEXT)}
    # The reports go into the conversation in the order the calls were made.
    reports = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert [r.split("\n")[0] for r in reports] == ["report on part 1", "report on part 2", "report on part 3"]
    assert "run at the same time (up to 3)" in agent.tools["Task"].description
    agent.close()


def test_elsewhere_they_run_one_after_another(llm, registry, project):
    three_reads(llm)
    watch = Stopwatch(0.2)
    agent = Agent(registry, project, delegate=watch)
    assert agent.run("look at three things").success
    assert len(watch.runs) == 3 and not watch.overlapping()
    assert "at the same time" not in agent.tools["Task"].description
    agent.close()


def test_a_sub_agent_that_may_write_never_runs_alongside_others(llm, registry, project):
    """Two writers in one worktree would edit the same files at once."""
    registry.models["test"].max_parallel_subtasks = 3
    three_reads(llm, extra=[("Task", {"instructions": "change it", "read_only": False})])
    watch = Stopwatch(0.4)
    agent = Agent(registry, project, delegate=watch)
    assert agent.run("look, then change").success

    writer = next(r for r in watch.runs if not r[4])
    assert all(r[2] <= writer[1] or r[1] >= writer[2] for r in watch.runs if r is not writer)
    agent.close()


def test_without_room_for_two_they_queue_instead(llm, registry, project):
    registry.models["test"].max_parallel_subtasks = 3
    registry.models["test"].context_length = 32_000  # 32K - 25% reserve leaves room for two 8K sub-agents at most
    llm.add_tool_calls([("Task", {"instructions": f"part {n}"}) for n in (1, 2, 3)], prompt_tokens=10_000)
    llm.add_tool_calls([("finish", {"success": True, "summary": "done"})])
    watch = Stopwatch(0.2)
    agent = Agent(registry, project, delegate=watch)
    assert agent.run("look at three things").success
    assert not watch.overlapping()  # 32K - 10K - 8K = 14K: not enough for two 8K sub-agents at once
    agent.close()
