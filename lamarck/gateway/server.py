"""The LLM gateway: the only component that talks to model providers.

Sandboxed evaluations and the host-side search roles call one small
OpenAI-compatible endpoint. The gateway

  * authenticates each request with a per-run bearer token (a *grant*) that names the
    roles and models the run may use and its per-role call budget;
  * enforces hard caps through the Ledger (global pools + per-run/per-role budgets);
  * rate-limits with token buckets (one per model and one per grant);
  * retries transient upstream failures with exponential backoff (each attempt is a
    budgeted call, because each attempt costs a request upstream);
  * records token usage per run/role/model and appends an audit record per request
    (hash of the request body, never its content, and never the API key).

The provider key lives only in this process. Grants are created through an admin
endpoint protected by a random secret written to the gateway's state directory.

Endpoints
  POST /v1/chat/completions       Authorization: Bearer <grant>, X-Lamarck-Role: <role>
  POST /admin/grants              X-Admin-Secret: <secret>  {run_id, roles, models, budget}
  GET  /admin/ledger              X-Admin-Secret: <secret>
  GET  /healthz
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .ledger import BudgetExceeded, Ledger, Usage, pool_of
from .providers import DashScopeProvider, FakeProvider, Router, UpstreamError, read_dotenv_key
from .ratelimit import TokenBucket


@dataclass
class Grant:
    run_id: str
    roles: list[str]
    models: list[str]  # allowed model names ("*" = any)
    budget: dict[str, int] = field(default_factory=dict)  # role -> max calls for this run
    rate: float = 20.0  # requests per second for this grant
    created: str = ""


@dataclass
class GatewayConfig:
    model_rates: dict[str, float] = field(default_factory=dict)  # model -> requests/second
    default_rate: float = 10.0
    max_attempts: int = 5
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 30.0


class Gateway:
    """Transport-independent core, so it can be unit-tested without HTTP."""

    def __init__(
        self,
        router: Router,
        ledger: Ledger,
        state_dir: Path | None = None,
        config: GatewayConfig | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.router = router
        self.ledger = ledger
        self.state_dir = state_dir
        self.cfg = config or GatewayConfig()
        self._sleep = sleep
        self._grants: dict[str, Grant] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self._audit_lock = threading.Lock()
        self.admin_secret = secrets.token_hex(16)
        if state_dir is not None:
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "admin_secret").write_text(self.admin_secret)
            grants_file = state_dir / "grants.json"
            if grants_file.exists():
                for tok, g in json.loads(grants_file.read_text()).items():
                    self._grants[tok] = Grant(**g)

    # -------------------------------------------------------------- grants
    def create_grant(self, grant: Grant) -> str:
        token = "lmk_" + secrets.token_hex(16)
        grant.created = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._grants[token] = grant
            if self.state_dir is not None:
                data = {t: g.__dict__ for t, g in self._grants.items()}
                (self.state_dir / "grants.json").write_text(json.dumps(data, indent=1))
        return token

    def _bucket(self, key: str, rate: float) -> TokenBucket:
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = TokenBucket(rate=rate, burst=max(1.0, rate * 2))
            return self._buckets[key]

    # ------------------------------------------------------------ requests
    def chat(self, token: str, role: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Handle one chat completion. Returns (HTTP status, JSON body)."""
        t0 = time.perf_counter()
        grant = self._grants.get(token)
        if grant is None:
            return 401, {"error": "unknown or missing grant token"}
        try:
            pool_of(role)
        except ValueError:
            return 400, {"error": f"unknown role {role!r}"}
        if role not in grant.roles:
            return 403, {"error": f"grant for {grant.run_id} does not allow role {role!r}"}
        model = str(body.get("model", ""))
        if "*" not in grant.models and model not in grant.models:
            return 403, {"error": f"grant for {grant.run_id} does not allow model {model!r}"}
        if not isinstance(body.get("messages"), list):
            return 400, {"error": "messages must be a list"}

        req_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
        attempts, status = 0, 500
        out: dict[str, Any] = {"error": "no attempt made"}
        usage = Usage()
        for attempt in range(1, self.cfg.max_attempts + 1):
            attempts = attempt
            if not self._bucket(f"grant:{token}", grant.rate).acquire(timeout=300):
                status, out = 429, {"error": "rate limit wait exceeded (grant)"}
                break
            if not self._bucket(
                f"model:{model}", self.cfg.model_rates.get(model, self.cfg.default_rate)
            ).acquire(timeout=300):
                status, out = 429, {"error": "rate limit wait exceeded (model)"}
                break
            try:
                self.ledger.reserve(grant.run_id, role, model, grant.budget)
            except BudgetExceeded as exc:
                status, out = 429, {"error": f"budget: {exc}", "budget_exceeded": True}
                break
            try:
                resp = self.router.complete(body)
            except UpstreamError as exc:
                self.ledger.record(grant.run_id, role, model, None, failed=True)
                status, out = exc.status, {"error": str(exc)[:500]}
                if not exc.transient:
                    break
                self._sleep(min(self.cfg.backoff_cap_s, self.cfg.backoff_base_s * 2 ** (attempt - 1)))
                continue
            u = resp.get("usage") or {}
            usage = Usage(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0))
            self.ledger.record(grant.run_id, role, model, usage)
            status, out = 200, resp
            break
        self._audit(
            {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "run": grant.run_id,
                "role": role,
                "model": model,
                "status": status,
                "attempts": attempts,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "request_sha256_16": req_hash,
            }
        )
        return status, out

    def _audit(self, rec: dict[str, Any]) -> None:
        if self.state_dir is None:
            return
        with self._audit_lock, open(self.state_dir / "audit.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")


# ------------------------------------------------------------------- HTTP layer
def make_handler(gw: Gateway) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # quiet; the audit log is the record
            return

        def _send(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            parsed = json.loads(raw or b"{}")
            if not isinstance(parsed, dict):
                raise ValueError("body must be a JSON object")
            return parsed

        def _is_admin(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Admin-Secret", ""), gw.admin_secret)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send(200, {"ok": True})
            elif self.path == "/admin/ledger" and self._is_admin():
                self._send(200, gw.ledger.snapshot())
            else:
                self._send(404 if self.path != "/admin/ledger" else 403, {"error": "not found"})

        def do_POST(self) -> None:
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError):
                self._send(400, {"error": "invalid JSON body"})
                return
            if self.path in ("/v1/chat/completions", "/chat/completions"):
                auth = self.headers.get("Authorization", "")
                token = auth.removeprefix("Bearer ").strip()
                role = self.headers.get("X-Lamarck-Role", "policy")
                status, out = gw.chat(token, role, body)
                self._send(status, out)
            elif self.path == "/admin/grants":
                if not self._is_admin():
                    self._send(403, {"error": "admin secret required"})
                    return
                try:
                    grant = Grant(
                        run_id=str(body["run_id"]),
                        roles=list(body["roles"]),
                        models=list(body.get("models", ["*"])),
                        budget={k: int(v) for k, v in body.get("budget", {}).items()},
                        rate=float(body.get("rate", 20.0)),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    self._send(400, {"error": f"bad grant: {exc}"})
                    return
                self._send(200, {"token": gw.create_grant(grant)})
            else:
                self._send(404, {"error": "not found"})

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 512  # the stdlib default (5) resets connections under bursty load


def serve(gw: Gateway, host: str, port: int) -> ThreadingHTTPServer:
    return _Server((host, port), make_handler(gw))


def start_in_thread(gw: Gateway, host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Start the HTTP server on a background thread (tests, local backend). Returns (server, base URL)."""
    server = serve(gw, host, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}"


def build_gateway(
    state_dir: Path, keyfile: Path | None, fake: bool, caps: dict[str, int] | None = None
) -> Gateway:
    from .fakes import RESPONDERS

    upstream = DashScopeProvider(read_dotenv_key(keyfile, "DASHSCOPE_API_KEY")) if keyfile else None
    router = Router(upstream=upstream, fake=FakeProvider(RESPONDERS) if fake else None)
    cfg = GatewayConfig(
        model_rates={"qwen3.8-flash": 25.0, "deepseek-v4.1-flash": 10.0, "qwen4-max": 3.0}, default_rate=5.0
    )
    return Gateway(router, Ledger(state_dir / "ledger.json", caps), state_dir, cfg)


def main() -> None:
    p = argparse.ArgumentParser(description="lamarck LLM gateway")
    p.add_argument(
        "--state", type=Path, required=True, help="directory for ledger, audit log, grants, secret"
    )
    p.add_argument(
        "--keyfile", type=Path, default=None, help="dotenv file with DASHSCOPE_API_KEY (omit = offline)"
    )
    p.add_argument("--fake", action="store_true", help="enable deterministic fake-* models")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--policy-cap", type=int, default=80_000)
    p.add_argument("--search-cap", type=int, default=3_000)
    a = p.parse_args()
    gw = build_gateway(a.state, a.keyfile, a.fake, {"policy": a.policy_cap, "search": a.search_cap})
    print(
        f"lamarck gateway on {a.host}:{a.port} (upstream={'on' if a.keyfile else 'off'}, fake={a.fake})",
        flush=True,
    )
    serve(gw, a.host, a.port).serve_forever()


if __name__ == "__main__":
    main()
