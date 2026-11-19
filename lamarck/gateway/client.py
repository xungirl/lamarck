"""Client for the gateway, used by the host search roles and by the sandbox runner.

Only the standard library is used so the sandbox image stays small and the
harness under test cannot reach anything but this one URL anyway.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class GatewayError(RuntimeError):
    def __init__(self, status: int, message: str, budget_exceeded: bool = False) -> None:
        super().__init__(f"gateway {status}: {message}")
        self.status = status
        self.budget_exceeded = budget_exceeded


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw_message: dict[str, Any] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class GatewayClient:
    def __init__(self, base_url: str, token: str, timeout_s: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    def chat(
        self,
        role: str,
        model: str,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> ChatResult:
        """One chat completion. Connection-level failures (the request never got a response) are
        retried a few times with backoff; HTTP errors from the gateway are not (it retries upstream)."""
        for attempt in range(4):
            try:
                return self._chat_once(role, model, messages, **params)
            except GatewayError as exc:
                if exc.status != 599 or attempt == 3:
                    raise
                time.sleep(0.5 * 2**attempt)
        raise AssertionError("unreachable")

    def _chat_once(
        self,
        role: str,
        model: str,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> ChatResult:
        body = {"model": model, "messages": messages, **params}
        req = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                "X-Lamarck-Role": role,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read())
            except (ValueError, OSError):
                err = {"error": str(exc)}
            raise GatewayError(exc.code, str(err.get("error")), bool(err.get("budget_exceeded"))) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GatewayError(599, f"connection failed: {exc}") from exc
        msg = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        return ChatResult(
            content=msg.get("content") or "",
            tool_calls=list(msg.get("tool_calls") or []),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            raw_message=msg,
        )


def create_grant(
    base_url: str,
    admin_secret: str,
    run_id: str,
    roles: list[str],
    models: list[str] | None = None,
    budget: dict[str, int] | None = None,
    rate: float = 20.0,
) -> str:
    body = {"run_id": run_id, "roles": roles, "models": models or ["*"], "budget": budget or {}, "rate": rate}
    req = urllib.request.Request(
        base_url.rstrip("/") + "/admin/grants",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Admin-Secret": admin_secret},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return str(json.loads(resp.read())["token"])


def ledger_snapshot(base_url: str, admin_secret: str) -> dict[str, Any]:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/admin/ledger", headers={"X-Admin-Secret": admin_secret}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        out: dict[str, Any] = json.loads(resp.read())
        return out
