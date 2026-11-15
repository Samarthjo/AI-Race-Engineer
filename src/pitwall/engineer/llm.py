"""Language-model providers. Local first: Ollama on this Mac.

The engineer only talks to :class:`LLMProvider`, so another provider (for example the Claude
API) can be added later without touching the engineer logic.

Ollama (https://ollama.com) runs models on the Mac and serves an HTTP API on
``127.0.0.1:11434``. Pitwall uses ``POST /api/chat`` without streaming, with a JSON schema in
``format`` for structured answers and ``tools`` for tool calls, and ``GET /api/version`` /
``GET /api/tags`` to check what is installed. Requests to the local machine never go
through a proxy: your data does not leave the Mac.
"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = "qwen3:14b"
DEFAULT_URL = "http://127.0.0.1:11434"


class LLMError(RuntimeError):
    """The model answered with something unusable (or the request failed)."""


class LLMUnavailable(LLMError):
    """No model to talk to (Ollama not running, model not installed)."""


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall]
    model: str
    duration_s: float
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    raw_message: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str
    model: str

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult: ...

    def status(self) -> dict[str, Any]: ...


def _is_local(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")


@dataclass
class OllamaProvider:
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_URL
    timeout_s: float = 180.0
    temperature: float = 0.2
    num_ctx: int = 8192
    seed: int | None = 42
    think: bool = False
    keep_alive: str = "10m"
    name: str = "ollama"

    def _open(self, request: urllib.request.Request, timeout: float) -> bytes:
        handlers: list[urllib.request.BaseHandler] = []
        if _is_local(self.base_url):
            handlers.append(urllib.request.ProxyHandler({}))  # never via a proxy
        opener = urllib.request.build_opener(*handlers)
        with opener.open(request, timeout=timeout) as resp:
            return resp.read()

    def _request(self, method: str, path: str, body: dict[str, Any] | None, timeout: float) -> Any:
        url = self.base_url.rstrip("/") + path
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            raw = self._open(req, timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            with contextlib.suppress(ValueError, AttributeError):
                detail = json.loads(exc.read() or b"{}").get("error", "")
            if exc.code == 404 and "not found" in detail:
                raise LLMUnavailable(
                    f"model {self.model!r} is not installed in Ollama: run `ollama pull "
                    f"{self.model}`"
                ) from exc
            raise LLMError(f"Ollama answered HTTP {exc.code}: {detail or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError):
                raise LLMError(f"Ollama did not answer within {timeout:.0f} s") from exc
            raise LLMUnavailable(
                f"Ollama is not running at {self.base_url} ({reason}): open the Ollama app "
                "or run `ollama serve`"
            ) from exc
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise LLMError("Ollama sent a response that is not JSON") from exc

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": self.think,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        if self.seed is not None:
            body["options"]["seed"] = self.seed
        if schema is not None:
            body["format"] = schema
        if tools:
            body["tools"] = tools
        t0 = time.monotonic()
        out = self._request("POST", "/api/chat", body, self.timeout_s)
        msg = out.get("message") or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append(ToolCall(str(fn.get("name", "")), dict(args)))
        return ChatResult(
            content=str(msg.get("content") or ""),
            tool_calls=calls,
            model=str(out.get("model") or self.model),
            duration_s=time.monotonic() - t0,
            prompt_tokens=out.get("prompt_eval_count"),
            output_tokens=out.get("eval_count"),
            raw_message=msg,
        )

    def status(self) -> dict[str, Any]:
        st: dict[str, Any] = {
            "provider": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "reachable": False,
            "version": None,
            "installed": [],
            "model_installed": False,
            "error": None,
        }
        try:
            st["version"] = self._request("GET", "/api/version", None, 5.0).get("version")
            tags = self._request("GET", "/api/tags", None, 5.0)
        except LLMError as exc:
            st["error"] = str(exc)
            return st
        st["reachable"] = True
        names = sorted(str(m.get("name") or m.get("model")) for m in tags.get("models") or [])
        st["installed"] = names
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        st["model_installed"] = wanted in names or self.model in names
        if not st["model_installed"]:
            st["error"] = f"model {self.model!r} is not installed: run `ollama pull {self.model}`"
        return st


def make_provider(
    provider: str = "ollama",
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_URL,
    timeout_s: float = 180.0,
    temperature: float = 0.2,
    num_ctx: int = 8192,
    think: bool = False,
) -> LLMProvider:
    if provider != "ollama":
        raise ValueError(f"unknown engineer provider {provider!r} (supported: ollama)")
    return OllamaProvider(
        model=model,
        base_url=base_url,
        timeout_s=timeout_s,
        temperature=temperature,
        num_ctx=num_ctx,
        think=think,
    )
