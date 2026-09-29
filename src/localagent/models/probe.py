"""Measure what a model endpoint can do: tool calls, prefill, prompt cache, speed, thinking toggle."""

import base64
import json
import struct
import uuid
import zlib
from dataclasses import dataclass

from localagent.models.openai_compat import OpenAICompatClient
from localagent.models.types import ModelError

_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "Read",
        "description": "Read a file.",
        "parameters": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        },
    },
}


@dataclass
class ProbeResult:
    """Outcome of one probe."""

    name: str
    ok: bool
    detail: str


def _filler(lines: int) -> str:
    nonce = uuid.uuid4().hex  # unique prefix, so the server cannot have it cached
    return f"Session {nonce}\n" + "".join(
        f"record {i}: id={nonce[:8]}-{i} value={(i * 7919) % 1000} status={'ok' if i % 3 else 'retry'}\n"
        for i in range(lines)
    )


def probe_tool_call(client: OpenAICompatClient) -> ProbeResult:
    response = client.chat(
        [
            {"role": "system", "content": "You are a coding agent. Use tools to act."},
            {"role": "user", "content": "Open the file src/app.py."},
        ],
        [_READ_TOOL],
        thinking=False,
        max_tokens=512,
    )
    if not response.tool_calls:
        return ProbeResult("tool call", False, f"no tool call; replied {response.content[:80]!r}")
    call = response.tool_calls[0]
    try:
        args = call.parsed_arguments()
    except ValueError:
        return ProbeResult("tool call", False, f"invalid JSON arguments {call.arguments!r}")
    ok = call.name == "Read" and "app.py" in json.dumps(args)
    return ProbeResult("tool call", ok, f"{call.name}({call.arguments}) in {response.elapsed:.1f}s")


def probe_prefill_and_cache(client: OpenAICompatClient, lines: int = 350) -> list[ProbeResult]:
    messages = [
        {"role": "system", "content": "Answer in one short sentence."},
        {"role": "user", "content": _filler(lines) + "\nWhat is the value of record 3?"},
    ]
    cold = client.chat(messages, thinking=False, max_tokens=16)
    prompt = cold.usage.prompt_tokens
    speed = prompt / cold.ttft if cold.ttft else 0.0
    results = [ProbeResult("cold prefill", cold.ttft is not None,
                           f"{prompt:,} tokens -> first token after {cold.ttft or 0:.1f}s ({speed:,.0f} tok/s)")]
    warm_messages = messages + [
        {"role": "assistant", "content": cold.content or "(answer)"},
        {"role": "user", "content": "And record 5?"},
    ]
    warm = client.chat(warm_messages, thinking=False, max_tokens=16)
    hit = warm.ttft is not None and cold.ttft is not None and warm.ttft < cold.ttft / 3
    results.append(ProbeResult("prefix cache", hit,
                               f"appended turn -> first token after {warm.ttft or 0:.2f}s"
                               + ("" if hit else " (no clear cache reuse)")))
    return results


def probe_generation(client: OpenAICompatClient) -> ProbeResult:
    response = client.chat(
        [{"role": "user", "content": "Write about 150 words explaining what a hash map is."}],
        thinking=False,
        max_tokens=256,
    )
    tps = response.tokens_per_second
    return ProbeResult("generation", tps is not None,
                       f"{response.usage.completion_tokens} tokens at {tps or 0:.1f} tok/s")


def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A one-colour PNG, made without an imaging library."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def probe_vision(client: OpenAICompatClient) -> ProbeResult:
    """Send a red square and ask for its colour (one small request)."""
    url = "data:image/png;base64," + base64.b64encode(solid_png(64, 64, (220, 20, 20))).decode()
    response = client.chat(
        [{"role": "user", "content": [
            {"type": "text", "text": "What colour is this image? Answer with one word."},
            {"type": "image_url", "image_url": {"url": url}},
        ]}],
        thinking=False,
        max_tokens=256,
    )
    answer = response.content.strip()
    return ProbeResult("vision", "red" in answer.lower(), f"answered {answer[:60]!r} in {response.elapsed:.1f}s")


def run_quick_probes(client: OpenAICompatClient) -> list[ProbeResult]:
    """One tool-call request, plus one image request for vision models: cheap enough for cloud quotas."""
    steps = [probe_tool_call] + ([probe_vision] if client.model.vision else [])
    results = []
    for step in steps:
        try:
            results.append(step(client))
        except ModelError as e:
            results.append(ProbeResult(step.__name__.removeprefix("probe_").replace("_", " "), False, str(e)))
    return results


def probe_thinking_toggle(client: OpenAICompatClient) -> ProbeResult:
    if client.model.thinking != "toggle":
        return ProbeResult("thinking off", True, f"model thinking mode is {client.model.thinking!r}; nothing to toggle")
    messages = [{"role": "user", "content": "What is 17*23? Reply with just the number."}]
    on = client.chat(messages, thinking=True, max_tokens=600)
    off = client.chat(messages, thinking=False, max_tokens=600)
    thought_on = on.usage.reasoning_tokens or len(on.reasoning)
    thought_off = off.usage.reasoning_tokens or len(off.reasoning)
    if not thought_on:
        return ProbeResult("thinking off", True, "the model did not think even when allowed")
    ok = thought_off == 0
    params = json.dumps(client.model.thinking_off) or "{}"
    return ProbeResult(
        "thinking off", ok,
        f"on: {on.elapsed:.1f}s, off: {off.elapsed:.1f}s with {params}"
        + ("" if ok else " (still thinking; set thinking_off for this model)"),
    )


def run_probes(client: OpenAICompatClient, include_cache: bool = True) -> list[ProbeResult]:
    """Run all probes, turning request failures into failed results."""
    steps = [probe_tool_call, probe_generation, probe_thinking_toggle]
    if client.model.vision:
        steps.append(probe_vision)
    results: list[ProbeResult] = []
    for step in steps:
        try:
            results.append(step(client))
        except ModelError as e:
            results.append(ProbeResult(step.__name__.removeprefix("probe_"), False, str(e)))
    if include_cache:
        try:
            results.extend(probe_prefill_and_cache(client))
        except ModelError as e:
            results.append(ProbeResult("prefill", False, str(e)))
    return results
