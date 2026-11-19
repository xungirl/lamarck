"""Run configuration: method hyperparameters plus the engineering knobs of a run.

Symbols follow RRSI (arXiv 2609.24972) and SIM (arXiv 2611.18856):

  rounds (N)     rounds t = 0..N-1
  k              candidate proposals per round (|C_t| before screening)
  b_min/b_max    bounds of the annealed edit budget b_t (RRSI Eq. anneal)
  delta          noise band; None = calibrate from repeated base evaluations
  beta0/beta1    cost rule for candidates that gain more than delta (RRSI Eq. tokenbudget)
  w_s/w_c/w_n    shaped rule for candidates inside the noise band (RRSI Alg. 2)
  n_prune        window of the recent-yield summary g_t used for the prune set
  memory         SIM condition: "none" | "problems" | "full" | "retrieved"

Every regularizer can be switched off, which turns the engine into plain
greedy RSI (useful for ablations and for the tests).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

MemoryMode = Literal["none", "problems", "full", "retrieved"]


@dataclass
class RunConfig:
    # ---- identity --------------------------------------------------------
    run_id: str = "run"
    domain: str = "toy"
    evolve_split: str = "evolve"
    heldout_split: str = "heldout"
    policy_model: str = "fake-policy"
    search_model: str = "fake-search"
    seed: int = 0
    # ---- horizon -----------------------------------------------------------
    rounds: int = 4
    k: int = 3
    # ---- memory (SIM) ------------------------------------------------------
    memory: MemoryMode = "full"
    sim_domain_tag: str = ""  # provenance string stored in each attempt; default "<split>/<model>"
    retrieval_top_k: int = 4  # memory == "retrieved": entries kept
    retrieval_token_budget: int = 1200  # memory == "retrieved": approximate token cap of the SIM block
    # ---- RRSI regularizers (each can be disabled) --------------------------
    use_edit_budget: bool = True
    b_min: int = 1
    b_max: int = 3
    use_noise_floor: bool = True
    delta: float | None = None
    delta_z: float = 2.0
    use_cost_rule: bool = True
    beta0: float = 0.10
    beta1: float = 10.0
    w_s: float = 100.0
    w_c: float = 15.0
    w_n: float = 0.5
    use_critic: bool = True
    use_llm_screen: bool = True
    use_prune_set: bool = True
    n_prune: int = 3
    # ---- engineering -------------------------------------------------------
    heldout_curve: bool = False  # evaluate each retained harness on held-out (never shown to the search)
    eval_parallel: int = 3  # candidate evaluations at once
    task_parallel: int = 8  # tasks at once inside one sandbox job
    rollout: int = 0  # rollout index used in the evaluation cache key
    max_trace_chars: int = 9000  # feedback budget handed to the proposer
    notes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.memory not in ("none", "problems", "full", "retrieved"):
            raise ValueError(f"unknown memory mode {self.memory!r}")
        if self.rounds < 1 or self.k < 1:
            raise ValueError("rounds and k must be >= 1")
        if self.b_min < 1 or self.b_max < self.b_min:
            raise ValueError("need 1 <= b_min <= b_max")

    @property
    def provenance(self) -> str:
        return self.sim_domain_tag or f"{self.evolve_split}/{self.policy_model}"

    def dump(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.dump(), indent=1))

    @classmethod
    def load(cls, path: Path, **overrides: Any) -> RunConfig:
        raw = json.loads(path.read_text())
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in raw.items() if k in known}
        kw.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kw)
