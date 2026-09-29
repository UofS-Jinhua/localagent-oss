"""AskUser: the agent can stop and ask when only the user can decide."""

import threading

from localagent.agent import Agent
from localagent.tools import make_tools
from localagent.tools.ask import MAX_QUESTIONS
from localagent.tools.workspace import Workspace


def run(registry, project, task="do it", **kwargs):
    events = []
    agent = Agent(registry, project, on_event=events.append, **kwargs)
    result = agent.run(task)
    agent.close()
    return result, events


def test_the_tool_exists_only_when_someone_can_answer(registry, project):
    without = Agent(registry, project)
    assert "AskUser" not in without.tools
    assert "AskUser" not in without.schemas[0]["function"]["description"]
    with_asker = Agent(registry, project, asker=lambda q, o: "yes")
    assert "AskUser" in with_asker.tools
    without.close()
    with_asker.close()


def test_the_answer_reaches_the_model(llm, registry, project):
    asked = []

    def asker(question, options):
        asked.append((question, options))
        return "the second one, calc.py"

    llm.add_tool_calls([("AskUser", {"question": "Which file did you mean?", "options": ["a.py", "calc.py"]})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "used calc.py"})])
    result, _ = run(registry, project, asker=asker)

    assert result.success and asked == [("Which file did you mean?", ["a.py", "calc.py"])]
    answer = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "The user answered: the second one, calc.py" in answer
    assert "call AskUser once" in llm.requests[0]["messages"][0]["content"]  # the prompt mentions it


def test_no_answer_does_not_block_the_task(llm, registry, project):
    llm.add_tool_calls([("AskUser", {"question": "Which one?"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "assumed the first"})])
    result, _ = run(registry, project, asker=lambda q, o: "")
    assert result.success
    assert "No answer" in [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][0]["content"]


def test_asking_over_and_over_is_capped(project):
    tool = make_tools(Workspace(project), asker=lambda q, o: "sure")[-1]
    assert tool.name == "AskUser"
    for _ in range(MAX_QUESTIONS):
        assert "The user answered" in tool.call({"question": "again?"})
    assert "already asked" in tool.call({"question": "and again?"})


def test_a_question_from_the_ui_is_answered_over_the_websocket(llm, registry, tmp_path, project):
    """End to end through the real server: the question reaches a client and the answer reaches the agent."""
    from fastapi.testclient import TestClient

    from localagent.server import create_app
    from localagent.store import Store

    token = "test-token"
    store = Store(tmp_path / "ask.db")
    llm.add_tool_calls([("AskUser", {"question": "Which database?", "options": ["sqlite", "postgres"]})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "used sqlite"})])
    registry.save()
    with TestClient(create_app(token, store=store, static_dir=None)) as client:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_json()
            ws.send_json({"type": "run", "work_dir": str(project), "task": "add a database"})
            for _ in range(200):
                event = ws.receive_json()
                if event["type"] == "question_request":
                    break
            else:
                raise AssertionError("no question arrived")
            assert event["question"] == "Which database?" and event["options"] == ["sqlite", "postgres"]
            ws.send_json({"type": "answer", "id": event["id"], "text": "sqlite"})
            for _ in range(200):
                event = ws.receive_json()
                if event["type"] == "task_finished":
                    break
    store.close()
    assert "The user answered: sqlite" in str(llm.requests[-1]["messages"])


def test_stopping_a_task_releases_a_waiting_question(llm, registry, tmp_path, project):
    """Stop must not leave the agent thread blocked on a question nobody will answer."""
    from localagent.server.runner import Questions

    published = []
    questions = Questions(lambda event, remember=True: published.append(event))
    answer: list[str] = []
    waiting = threading.Thread(target=lambda: answer.append(questions.ask("stuck?", [])), daemon=True)
    waiting.start()
    while not published:
        pass
    questions.close_all()
    waiting.join(timeout=5)
    assert not waiting.is_alive() and answer == [""]
