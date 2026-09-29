"""A real HTTP server that replays scripted chat-completion streams.

Tests talk to it over HTTP exactly as they would to LM Studio, and it records
every request body so tests can assert on what the client actually sent.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # clients that stop reading mid-stream (cancellation tests) are expected


class ScriptedLLM:
    """Queue responses with ``add_*``; read what was sent from ``requests``."""

    def __init__(self, lmstudio: bool = False, models: list[dict[str, Any]] | None = None):
        self.lmstudio = lmstudio
        self.models = models or [{"id": "test-model", "type": "llm", "state": "loaded",
                                  "loaded_context_length": 8192, "max_context_length": 32768,
                                  "capabilities": ["tool_use"], "quantization": "Q4", "arch": "qwen"}]
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self._responses: list[tuple[int, Any]] = []
        self._lock = threading.Lock()
        self._server = _QuietServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> "ScriptedLLM":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def add_stream(self, chunks: list[dict[str, Any]]) -> None:
        with self._lock:
            self._responses.append((200, chunks))

    def add_text(self, content: str, reasoning: str = "", prompt_tokens: int = 100) -> None:
        chunks = []
        if reasoning:
            chunks.append(_chunk({"reasoning_content": reasoning}))
        for piece in (content[: len(content) // 2], content[len(content) // 2:]):
            if piece:
                chunks.append(_chunk({"content": piece}))
        chunks.append(_chunk({}, "stop"))
        chunks.append({"choices": [], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 10,
                                                "completion_tokens_details": {"reasoning_tokens": len(reasoning)}}})
        self.add_stream(chunks)

    def add_tool_calls(self, calls: list[tuple[str, dict[str, Any]]], content: str = "",
                       prompt_tokens: int = 100) -> None:
        """Queue a response calling tools; arguments arrive split across chunks like real servers send them."""
        chunks = [_chunk({"content": content})] if content else []
        for index, (name, args) in enumerate(calls):
            text = json.dumps(args)
            cut = len(text) // 2
            chunks.append(_chunk({"tool_calls": [{"index": index, "id": f"call_{len(self._responses)}_{index}",
                                                  "type": "function", "function": {"name": name, "arguments": text[:cut]}}]}))
            chunks.append(_chunk({"tool_calls": [{"index": index, "function": {"arguments": text[cut:]}}]}))
        chunks.append(_chunk({}, "tool_calls"))
        chunks.append({"choices": [], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 20}})
        self.add_stream(chunks)

    def add_cut_off(self, reasoning: str, prompt_tokens: int = 100) -> None:
        """Queue a reply that runs into the output-token limit while still thinking (finish_reason "length")."""
        self.add_stream([
            _chunk({"reasoning_content": reasoning}),
            _chunk({}, "length"),
            {"choices": [], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 16384,
                                      "completion_tokens_details": {"reasoning_tokens": 16384}}},
        ])

    def add_long_think(self, tokens: int, prompt_tokens: int = 100) -> None:
        """Queue a reply that thinks on and on without repeating itself and never calls a tool.

        One token per chunk, three characters each, as llama.cpp streams it.
        """
        text = "".join(f"Step {i}: checking module_{i}.py, where buffer {i} is built before index {i * 3}. "
                       for i in range(1, tokens * 3 // 60 + 2))
        pieces = [text[i:i + 3] for i in range(0, tokens * 3, 3)]
        self.add_stream([_chunk({"reasoning_content": piece}) for piece in pieces]
                        + [_chunk({}, "stop"),
                           {"choices": [], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 9000}}])

    def add_loop(self, passage: str, times: int = 300) -> None:
        """Queue a reply whose thinking repeats *passage* forever (the client is expected to cut it short)."""
        usage = {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 9000}}
        self.add_stream([_chunk({"reasoning_content": passage}) for _ in range(times)]
                        + [_chunk({}, "stop"), usage])

    def add_error(self, status: int, body: str) -> None:
        with self._lock:
            self._responses.append((status, body))

    def _next(self) -> tuple[int, Any]:
        with self._lock:
            if not self._responses:
                return 500, "no scripted response left"
            return self._responses.pop(0)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        script = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args: object) -> None:
                pass

            def _json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/v1/models":
                    self._json(200, {"data": [{"id": m["id"]} for m in script.models]})
                elif self.path == "/api/v0/models" and script.lmstudio:
                    self._json(200, {"data": script.models})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                script.requests.append(json.loads(self.rfile.read(length)))
                script.headers.append(dict(self.headers))
                status, payload = script._next()
                if status != 200:
                    body = payload.encode()
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in payload:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")

        return Handler
