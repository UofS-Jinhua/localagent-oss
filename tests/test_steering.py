from localagent.agent import Agent


def test_queued_message_is_added_before_the_next_model_call(llm, registry, project):
    llm.add_tool_calls([("finish", {"success": True, "summary": "ok"})])
    agent = Agent(registry, project)
    agent.steer("also update the README")
    agent.run("fix the bug")
    last = llm.requests[0]["messages"][-1]
    assert last["role"] == "user" and "also update the README" in last["content"]


def test_finish_waits_for_a_pending_message(llm, registry, project):
    llm.add_tool_calls([("Glob", {"pattern": "*"}), ("finish", {"success": True, "summary": "early"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "handled the new request"})])
    events = []
    agent = Agent(registry, project, on_event=events.append)

    def steer_during_first_tool(event):
        events.append(event)
        if event["type"] == "tool_call" and event["name"] == "Glob":
            agent.steer("please also run the tests")

    agent.on_event = steer_during_first_tool
    result = agent.run("task")

    assert result.summary == "handled the new request" and result.steps == 2
    second = llm.requests[1]["messages"]
    assert "finish rejected" in second[-2]["content"]
    assert second[-1]["role"] == "user" and "please also run the tests" in second[-1]["content"]
    assert [e["text"] for e in events if e["type"] == "steer"] == ["please also run the tests"]


def test_blank_messages_are_ignored(llm, registry, project):
    llm.add_tool_calls([("finish", {"success": True, "summary": "ok"})])
    agent = Agent(registry, project)
    agent.steer("   ")
    agent.run("task")
    assert llm.requests[0]["messages"][-1]["content"] == "task"
