"""Wiring: gateway + sandbox backend + evaluator + search model for one experiment process.

`LocalStack` starts an in-process gateway (fake models, optional upstream) and the
subprocess backend: everything the offline tests and `make ci` need.
`DockerStack` (lamarck.sandbox.docker_backend) uses the gateway container and the
Docker backend for real experiments.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RunConfig
from .domains.base import Domain
from .domains.registry import get_domain
from .engine.evaluator import Evaluator
from .engine.loop import EvolutionRun, SearchLLM
from .gateway.client import GatewayClient, create_grant
from .gateway.ledger import Ledger
from .gateway.providers import DashScopeProvider, FakeProvider, Router, read_dotenv_key
from .gateway.server import Gateway, GatewayConfig, start_in_thread
from .gateway.spend import SpendLedger
from .sandbox.backends import Backend, LocalBackend


@dataclass
class Stack:
    gateway_url: str  # as seen from the host
    admin_secret: str
    backend: Backend
    root: Path  # experiment root: runs/<...>

    def grant(self, run_id: str, roles: list[str], budget: dict[str, int] | None = None) -> str:
        return create_grant(self.gateway_url, self.admin_secret, run_id, roles, budget=budget)

    def make_run(
        self,
        cfg: RunConfig,
        *,
        source: EvolutionRun | None = None,
        source_ref: str = "evolve",
        domain: Domain | None = None,
        search_extra: dict[str, Any] | None = None,
        policy_params: dict[str, Any] | None = None,
    ) -> EvolutionRun:
        domain = domain or get_domain(cfg.domain)
        policy_token = self.grant(cfg.run_id, ["policy"])
        search_token = self.grant(cfg.run_id, ["proposer", "selector", "critic"])
        evaluator = Evaluator(
            domain=domain,
            backend=self.backend,
            cache_dir=self.root / "cache" / "evals",
            work_dir=self.root / "work",
            token=policy_token,
            task_parallel=cfg.task_parallel,
            policy_params=policy_params,
        )
        search = SearchLLM(
            GatewayClient(self.gateway_url, search_token), cfg.search_model, extra=search_extra or {}
        )
        run_dir = self.root / "runs" / cfg.run_id
        return EvolutionRun.create(
            cfg, domain, evaluator, search, run_dir, source=source, source_ref=source_ref
        )


def local_stack(
    root: Path,
    keyfile: Path | None = None,
    caps: dict[str, int] | None = None,
    spend_file: Path | None = None,
    spend_cap: float = 9.50,
) -> Stack:
    from .gateway.fakes import RESPONDERS

    if keyfile is not None and spend_file is None:
        raise ValueError("an upstream gateway needs a spend ledger file")
    upstream = DashScopeProvider(read_dotenv_key(keyfile, "DASHSCOPE_API_KEY")) if keyfile else None
    gw = Gateway(
        Router(upstream=upstream, fake=FakeProvider(RESPONDERS)),
        Ledger(root / "gateway" / "ledger.json", caps),
        root / "gateway",
        GatewayConfig(default_rate=1000.0, backoff_base_s=0.01),
        spend=SpendLedger(spend_file, spend_cap) if spend_file is not None else None,
    )
    _server, url = start_in_thread(gw)
    return Stack(url, gw.admin_secret, LocalBackend(url), root)


def docker_stack(
    root: Path,
    keyfile: Path | None = None,
    fake: bool = True,
    warm: int = 2,
    memory_mb: int = 1024,
    use_runsc: bool = True,
    spend_file: Path | None = None,
) -> Stack:
    """Gateway container + Docker sandbox backend (gVisor when available)."""
    from .sandbox import docker_backend, image

    tag = image.build(root / "build" / "ctx")
    state = root / "gateway-state"
    url = docker_backend.start_gateway(
        tag, state, keyfile, fake=fake, spend_file=spend_file if keyfile is not None else None
    )
    secret = (state / "admin_secret").read_text().strip()
    backend = docker_backend.DockerBackend(
        tag, root / "slots", warm=warm, memory_mb=memory_mb, use_runsc=use_runsc
    )
    return Stack(url, secret, backend, root)
