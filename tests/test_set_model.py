import json

import pytest
from scripted_server import _chunk
from test_agent_loop import assert_append_only

from localagent.agent import Agent
from localagent.attachments import AttachmentStore, place
from localagent.models.openai_compat import GEMINI_SKIP_SIGNATURE, IMAGE_OMITTED, OpenAICompatClient, adapt_messages
from localagent.models.probe import solid_png
from localagent.models.registry import ModelSpec, Provider, Registry, RoleSpec, is_local_url

PNG = solid_png(4, 4, (255, 0, 0))


@pytest.fixture
def models(llm):
    local = Provider(name="local", base_url=llm.base_url)
    cloud = Provider(name="cloud", base_url=llm.base_url, local=False)
    specs = [
        ModelSpec(name="small", provider="local", model_id="small-model", context_length=8192,
                  thinking_off={"reasoning_effort": "none"}),
        ModelSpec(name="big", provider="cloud", model_id="big-model", context_length=100_000, vision=True,
                  switchable=True, notes="Use it for images."),
        ModelSpec(name="other", provider="local", model_id="other-model", context_length=4000, switchable=True),
        ModelSpec(name="hidden", provider="local", model_id="hidden-model", context_length=32768),
    ]
    return Registry(providers={"local": local, "cloud": cloud}, models={m.name: m for m in specs},
                    roles={"main": RoleSpec(model="small", thinking=True)})


def run(registry, project, task="do it", attachments=None, hook=None, **kwargs):
    events = []
    agent = Agent(registry, project, on_event=lambda e: (events.append(e), hook and hook(agent, e)), **kwargs)
    result = agent.run(task, attachments=attachments)
    agent.close()
    return result, events


def set_model_schema(agent):
    return next((s for s in agent.schemas if s["function"]["name"] == "set_model"), None)


def test_local_and_cloud_urls():
    assert is_local_url("http://localhost:1234/v1") and is_local_url("http://192.168.1.5:1234/v1")
    assert is_local_url("http://100.101.102.103:1234/v1")  # Tailscale
    assert is_local_url("http://gpu-box:8080/v1") and is_local_url("http://studio.local:1234")
    assert not is_local_url("https://generativelanguage.googleapis.com/v1beta/openai")
    assert not is_local_url("https://8.8.8.8/v1")


def test_set_model_offers_only_allowed_models(models, project):
    local_only = set_model_schema(Agent(models, project))
    assert local_only["function"]["parameters"]["properties"]["model"]["enum"] == ["small", "other"]
    with_cloud = set_model_schema(Agent(models, project, allow_cloud=True))
    assert with_cloud["function"]["parameters"]["properties"]["model"]["enum"] == ["small", "big", "other"]
    assert "- big: cloud, 100,000-token context, can view images. Use it for images." in with_cloud["function"]["description"]
    for spec in models.models.values():
        spec.switchable = False
    assert set_model_schema(Agent(models, project, allow_cloud=True)) is None


def test_agent_switches_model_mid_task(llm, models, project):
    llm.add_tool_calls([("set_model", {"model": "big", "reason": "need a bigger window"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "done on big"})])
    result, events = run(models, project, allow_cloud=True, thinking=False)
    assert result.success
    assert [r["model"] for r in llm.requests] == ["small-model", "big-model"]
    assert llm.requests[0]["reasoning_effort"] == "none" and "reasoning_effort" not in llm.requests[1]
    switch = next(e for e in events if e["type"] == "model_switch")
    assert switch["previous"] == "small" and switch["model"] == "big" and switch["by"] == "agent"
    assert not switch["local"] and switch["reason"] == "need a bigger window"
    assert "Switched from small to big" in llm.requests[1]["messages"][-1]["content"]
    assert [e["context_limit"] for e in events if e["type"] == "usage"] == [8192, 100_000]
    assert_append_only(llm.requests)


def test_switch_is_refused_when_the_conversation_does_not_fit(llm, models, project):
    llm.add_tool_calls([("set_model", {"model": "other", "reason": "cheaper"})], prompt_tokens=3500)
    llm.add_tool_calls([("finish", {"success": True, "summary": "stayed"})])
    result, events = run(models, project)
    assert result.success and [r["model"] for r in llm.requests] == ["small-model", "small-model"]
    assert "too many for other" in llm.requests[1]["messages"][-1]["content"]
    assert not any(e["type"] == "model_switch" for e in events)


def test_user_can_move_a_running_task_to_any_model(llm, models, project):
    (project / "a.txt").write_text("a\n", encoding="utf-8")
    llm.add_tool_calls([("Read", {"file_path": "a.txt"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "ok"})])

    def hook(agent, event):
        if event["type"] == "tool_result":
            agent.request_model("hidden")  # not switchable: only the agent is limited to the whitelist

    result, events = run(models, project, hook=hook)
    assert result.success and [r["model"] for r in llm.requests] == ["small-model", "hidden-model"]
    assert llm.requests[1]["messages"][-1]["content"].startswith("[The user moved this task to another model]")
    assert next(e for e in events if e["type"] == "model_switch")["by"] == "user"


def test_images_reach_vision_models_and_a_note_reaches_the_others(llm, models, project, tmp_path):
    store = AttachmentStore(tmp_path / "attachments")
    placed = place(store, [store.add(PNG, "shot.png")], project)
    for _ in range(2):
        llm.add_tool_calls([("finish", {"success": True, "summary": "looked"})])
    _, events = run(models, project, "what is wrong in this screenshot?", attachments=placed, model="big")
    run(models, project, "what is wrong in this screenshot?", attachments=placed, model="small")
    seen, blind = llm.requests[0]["messages"][1]["content"], llm.requests[1]["messages"][1]["content"]
    assert seen[0]["text"].startswith("what is wrong") and seen[-1]["type"] == "image_url"
    assert isinstance(blind, str) and IMAGE_OMITTED in blind and "[image .la-attachments/shot.png]" in blind
    start = next(e for e in events if e["type"] == "task_start")
    assert start["vision"] and start["attachments"][0]["path"] == ".la-attachments/shot.png"


def test_read_of_an_image_is_shown_only_to_vision_models(llm, models, project):
    (project / "ui.png").write_bytes(PNG)
    for _ in range(2):
        llm.add_tool_calls([("Read", {"file_path": "ui.png"})])
        llm.add_tool_calls([("finish", {"success": True, "summary": "ok"})])
    run(models, project, model="big")
    run(models, project, model="small")
    big_messages, small_messages = llm.requests[1]["messages"], llm.requests[3]["messages"]
    assert "It is shown in the next message" in big_messages[-2]["content"]
    assert big_messages[-1]["role"] == "user" and big_messages[-1]["content"][1]["type"] == "image_url"
    assert small_messages[-1]["role"] == "tool" and "cannot view images" in small_messages[-1]["content"]


def test_steering_can_carry_attachments(llm, models, project, tmp_path):
    store = AttachmentStore(tmp_path / "attachments")
    placed = place(store, [store.add(b"Use snake_case names.\n", "style.md")], project)
    llm.add_tool_calls([("finish", {"success": True, "summary": "ok"})])
    events = []
    agent = Agent(models, project, on_event=events.append)
    agent.steer("", placed)
    agent.run("rename things")
    steering = llm.requests[0]["messages"][2]["content"]
    assert "(see the attachments)" in steering and "Use snake_case names." in steering
    assert next(e for e in events if e["type"] == "steer")["attachments"][0]["name"] == "style.md"


def test_messages_are_adapted_per_model():
    call = {"id": "c1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}
    signed = {**call, "extra_content": {"google": {"thought_signature": "abc"}}}
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "look"},
                                     {"type": "image_url", "image_url": {"url": "data:x"}}]},
        {"role": "assistant", "content": "", "tool_calls": [call, call]},
        {"role": "assistant", "content": "", "tool_calls": [signed]},
    ]
    blind = adapt_messages(messages, vision=False, gemini=False)
    assert blind[0]["content"] == f"look\n\n{IMAGE_OMITTED}"
    assert "extra_content" not in blind[2]["tool_calls"][0]
    gemini = adapt_messages(messages, vision=True, gemini=True)
    assert gemini[0] == messages[0]
    assert gemini[1]["tool_calls"][0]["extra_content"] == GEMINI_SKIP_SIGNATURE
    assert "extra_content" not in gemini[1]["tool_calls"][1]
    assert gemini[2]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "abc"
    assert "extra_content" not in messages[1]["tool_calls"][0]  # the conversation itself is untouched


def test_provider_data_on_tool_calls_is_kept(llm, models):
    extra = {"google": {"thought_signature": "sig-1"}}
    llm.add_stream([
        _chunk({"tool_calls": [{"index": 0, "id": "c1", "extra_content": extra,
                                "function": {"name": "Read", "arguments": json.dumps({"file_path": "a"})}}]}),
        _chunk({}, "stop"),
    ])
    client = OpenAICompatClient(models.providers["local"], models.models["small"])
    response = client.chat([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "Read"}}])
    client.close()
    assert response.tool_calls[0].extra_content == extra
    assert response.tool_calls[0].to_message()["extra_content"] == extra


def test_a_failing_switched_model_falls_back_to_the_original(llm, models, project):
    """A cloud model that rejects the request (bad key, no quota) must not end the task."""
    llm.add_tool_calls([("set_model", {"model": "big", "reason": "to look at the image"})])
    llm.add_error(401, '{"error": {"message": "invalid authentication credentials"}}')
    llm.add_tool_calls([("finish", {"success": True, "summary": "did it without the cloud model"})])
    result, events = run(models, project, allow_cloud=True)
    assert result.success
    assert [r["model"] for r in llm.requests] == ["small-model", "big-model", "small-model"]
    switches = [(e["previous"], e["model"], e["by"]) for e in events if e["type"] == "model_switch"]
    assert switches == [("small", "big", "agent"), ("big", "small", "fallback")]
    note = llm.requests[-1]["messages"][-1]["content"]
    assert "The request to big failed" in note and "do not switch to big again" in note
