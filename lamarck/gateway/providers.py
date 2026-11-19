"""Upstream model providers behind the gateway.

Every provider takes an OpenAI-style chat request body and returns an
OpenAI-style response body. That keeps the gateway, the sandbox runner and
the host search roles provider-agnostic.

  DashScopeProvider  Alibaba Cloud Model Studio, OpenAI-compatible endpoint (China region).
                     The API key is read from a dotenv file when the gateway starts and lives
                     only in gateway memory: never logged, never written, never forwarded.
  FakeProvider       deterministic, offline responders keyed by model name (tests and CI).
  GeminiProvider     placeholder showing where a Google Gemini backend plugs in: map the body
                     to `generateContent` and the response back. Not implemented (no key here).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import httpx

DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
FORWARDED_FIELDS = (
    "model",
    "messages",
    "tools",
    "tool_choice",
    "temperature",
    "top_p",
    "max_tokens",
    "seed",
    "response_format",
    "enable_thinking",
)


class UpstreamError(RuntimeError):
    def __init__(self, message: str, status: int, transient: bool) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient


class Provider(Protocol):
    name: str

    def complete(self, body: dict[str, Any]) -> dict[str, Any]: ...


def read_dotenv_key(path: Path, name: str) -> str:
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith(name) and "=" in line:
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    raise RuntimeError(f"{name} not found in {path}")


class DashScopeProvider:
    name = "dashscope"

    def __init__(self, api_key: str, base_url: str = DASHSCOPE_URL, timeout_s: float = 240.0) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_s, connect=15.0),
        )

    def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = {k: body[k] for k in FORWARDED_FIELDS if k in body}
        try:
            r = self._client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise UpstreamError(f"timeout: {exc}", 504, transient=True) from exc
        except httpx.TransportError as exc:
            raise UpstreamError(f"transport: {exc}", 502, transient=True) from exc
        if r.status_code != 200:
            transient = r.status_code == 429 or r.status_code >= 500
            raise UpstreamError(f"upstream {r.status_code}: {r.text[:300]}", r.status_code, transient)
        data: dict[str, Any] = r.json()
        return data


Responder = Callable[[dict[str, Any]], str | dict[str, Any]]


class FakeProvider:
    """Deterministic responders keyed by model name (see lamarck.gateway.fakes).

    A responder returns either the assistant text or a full message dict
    (e.g. with tool_calls). Token usage is a deterministic function of the text.
    """

    name = "fake"

    def __init__(self, responders: dict[str, Responder]) -> None:
        self.responders = responders

    def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        model = str(body.get("model", ""))
        if model not in self.responders:
            raise UpstreamError(f"fake provider has no model {model!r}", 404, transient=False)
        out = self.responders[model](body)
        message: dict[str, Any] = {"role": "assistant", "content": out} if isinstance(out, str) else out
        prompt_chars = sum(len(str(m.get("content") or "")) for m in body.get("messages", []))
        completion_chars = len(str(message.get("content") or "")) + len(str(message.get("tool_calls") or ""))
        return {
            "id": "fake",
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_chars // 4 + 1, "completion_tokens": completion_chars // 4 + 1},
        }


class GeminiProvider:
    """Extension point. A Gemini backend would translate `messages`/`tools` into the
    `generateContent` request shape and map candidates/usageMetadata back."""

    name = "gemini"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Gemini is not wired up in this repository (no key available)")


class Router:
    """Picks a provider per model name: fake models by prefix, everything else upstream."""

    def __init__(self, upstream: Provider | None, fake: FakeProvider | None) -> None:
        self.upstream = upstream
        self.fake = fake

    def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        model = str(body.get("model", ""))
        if model.startswith("fake-"):
            if self.fake is None:
                raise UpstreamError("fake models are disabled", 400, transient=False)
            return self.fake.complete(body)
        if self.upstream is None:
            raise UpstreamError(f"no upstream provider configured for {model!r}", 400, transient=False)
        return self.upstream.complete(body)
