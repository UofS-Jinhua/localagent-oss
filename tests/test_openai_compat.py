import threading

import pytest

from localagent.models import make_client
from localagent.models.openai_compat import extract_text_tool_calls
from localagent.models.types import Cancelled, ContextOverflowError, ModelError


def test_streams_content_reasoning_and_usage(llm, registry):
    llm.add_text("hello world", reasoning="let me think")
    deltas = []
    response = make_client(registry.resolve()).chat(
        [{"role": "user", "content": "hi"}], on_delta=lambda kind, text: deltas.append(kind)
    )
    assert response.content == "hello world"
    assert response.reasoning == "let me think"
    assert response.usage.prompt_tokens == 100
    assert response.ttft is not None and response.finish_reason == "stop"
    assert deltas[0] == "reasoning" and "content" in deltas


def test_reassembles_split_tool_calls(llm, registry):
    llm.add_tool_calls([("Read", {"file_path": "a.py"}), ("Grep", {"pattern": "def f"})])
    response = make_client(registry.resolve()).chat([{"role": "user", "content": "go"}], tools=[])
    assert [(c.name, c.parsed_arguments()) for c in response.tool_calls] == [
        ("Read", {"file_path": "a.py"}), ("Grep", {"pattern": "def f"})]


def test_thinking_off_adds_model_params(llm, registry):
    llm.add_text("a")
    llm.add_text("b")
    client = make_client(registry.resolve())
    client.chat([{"role": "user", "content": "x"}], thinking=True)
    client.chat([{"role": "user", "content": "x"}], thinking=False, max_tokens=64)
    first, second = llm.requests
    assert "reasoning_effort" not in first
    assert second["reasoning_effort"] == "none" and second["max_tokens"] == 64
    assert first["model"] == "test-model" and first["stream"] is True


def test_sampling_travels_with_the_model_and_thinking_off_overrides_it(llm, registry):
    """Qwen wants one sampling preset while thinking and another without it; both live on the model."""
    model = registry.models["test"]
    model.extra_params = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0}
    model.thinking_off = {"reasoning_effort": "none", "temperature": 0.7, "presence_penalty": 1.5}
    llm.add_text("a")
    llm.add_text("b")
    client = make_client(registry.resolve())

    client.chat([{"role": "user", "content": "x"}], thinking=True)
    client.chat([{"role": "user", "content": "x"}], thinking=False)

    thinking, quiet = llm.requests
    assert thinking["temperature"] == 1.0 and thinking["top_k"] == 20 and thinking["min_p"] == 0.0
    assert "presence_penalty" not in thinking
    assert quiet["temperature"] == 0.7 and quiet["presence_penalty"] == 1.5
    assert quiet["top_k"] == 20  # what thinking_off does not mention stays as the model's own


def test_http_errors(llm, registry):
    client = make_client(registry.resolve())
    llm.add_error(503, "busy")
    with pytest.raises(ModelError) as info:
        client.chat([{"role": "user", "content": "x"}])
    assert info.value.retryable
    llm.add_error(400, "The number of tokens to keep is greater than the context length")
    with pytest.raises(ContextOverflowError):
        client.chat([{"role": "user", "content": "x"}])


def test_stop_event_cancels(llm, registry):
    llm.add_text("long answer")
    stop = threading.Event()
    stop.set()
    with pytest.raises(Cancelled):
        make_client(registry.resolve()).chat([{"role": "user", "content": "x"}], stop_event=stop)


def test_tool_calls_written_as_text_are_recovered(llm, registry):
    llm.add_text('<tool_call>\n{"name": "Read", "arguments": {"file_path": "x.py"}}\n</tool_call>')
    tools = [{"type": "function", "function": {"name": "Read", "parameters": {}}}]
    response = make_client(registry.resolve()).chat([{"role": "user", "content": "x"}], tools=tools)
    assert [(c.name, c.parsed_arguments()) for c in response.tool_calls] == [("Read", {"file_path": "x.py"})]
    assert response.content == ""


@pytest.mark.parametrize("text, name, args", [
    ('<tool_call><function=Bash><parameter=command>\nls -la\n</parameter></function></tool_call>',
     "Bash", {"command": "ls -la"}),
    ('```json\n{"name": "Glob", "arguments": {"pattern": "*.py"}}\n```', "Glob", {"pattern": "*.py"}),
])
def test_text_tool_call_formats(text, name, args):
    calls, _ = extract_text_tool_calls(text, {"Bash", "Glob"})
    assert [(c.name, c.parsed_arguments()) for c in calls] == [(name, args)]


def test_text_that_merely_mentions_json_is_left_alone():
    calls, content = extract_text_tool_calls('Use {"name": "other"} config.', {"Read"})
    assert calls == [] and content == 'Use {"name": "other"} config.'


def test_a_malformed_chunk_does_not_kill_the_stream(llm, registry):
    """Servers do emit the occasional broken line; the rest of the answer still counts."""
    from localagent.models import make_client

    llm.add_stream([
        {"choices": [{"index": 0, "delta": {"content": "the answer "}, "finish_reason": None}]},
        "not json at all",
        {"choices": [{"index": 0, "delta": {"content": "is 42"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
    ])
    client = make_client(registry.resolve())
    response = client.chat([{"role": "user", "content": "what is it?"}])
    client.close()
    assert response.content == "the answer is 42" and response.usage.prompt_tokens == 7
