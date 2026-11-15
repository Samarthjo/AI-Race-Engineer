"""A stand-in for the Ollama HTTP API in tests: scripted replies, recorded requests.

Implements ``GET /api/version``, ``GET /api/tags`` and ``POST /api/chat`` (non-streaming)
with the response shape of Ollama's API reference.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Reply = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]


def text_reply(content: str | dict[str, Any]) -> dict[str, Any]:
    if not isinstance(content, str):
        content = json.dumps(content)
    return {"message": {"role": "assistant", "content": content}}


def tool_reply(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"index": i, "name": name, "arguments": args}}
                for i, (name, args) in enumerate(calls)
            ],
        }
    }


class MockOllama:
    def __init__(self, models: tuple[str, ...] = ("qwen3:14b",)) -> None:
        self.models = list(models)
        self.replies: list[Reply] = []
        self.requests: list[dict[str, Any]] = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path == "/api/version":
                    self._send(200, {"version": "0.12.0"})
                elif self.path == "/api/tags":
                    self._send(200, {"models": [{"name": m, "model": m} for m in mock.models]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                mock.requests.append(body)
                if body.get("model") not in mock.models:
                    self._send(404, {"error": f"model '{body.get('model')}' not found"})
                    return
                if not mock.replies:
                    self._send(500, {"error": "no scripted reply left"})
                    return
                reply = mock.replies.pop(0)
                out = reply(body) if callable(reply) else reply
                self._send(
                    200,
                    {
                        "model": body.get("model"),
                        "created_at": "2026-11-15T10:00:00Z",
                        "done": True,
                        "done_reason": "stop",
                        "total_duration": 1_000_000,
                        "prompt_eval_count": 100,
                        "eval_count": 50,
                        **out,
                    },
                )

            def log_message(self, format: str, *args: object) -> None:  # stdlib signature
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> MockOllama:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
