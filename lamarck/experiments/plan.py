"""The pre-registered experiment plan (PREREG.md) as code: one RunSpec per run, with dependencies.

Naming:
  sql-src-i        text-to-SQL source RSI on spider_evolve (seed i), builds SIM
  sql-full-i       continue sql-src-i on bird_evolve with Full SIM          } paired study (RQ1)
  sql-transfer-i   continue sql-src-i on bird_evolve without SIM            }
  sql-cold         seed harness on bird_evolve, no SIM                       } four conditions (RQ2a)
  sql-problems-1   continue sql-src-1 with root causes only                  } (+ sql-full-1, sql-transfer-1)
  so-src-i         support-ops source RSI on so_evolve
  so-{cold,transfer,problems,full}   support-ops standard -> shifted policy (RQ2b)
  xm-{cold,transfer,problems,full}   cross-model on BIRD: qwen-evolved (sql-cold) -> deepseek (RQ2c)
  ret-{full,top}-s pooled SIM of all sql-src runs, full vs retrieved (RQ4)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import RunConfig

QWEN = "qwen3.8-flash"
DEEPSEEK = "deepseek-v4.1-flash"
SEARCH = "qwen4-max"
POLICY_PARAMS: dict[str, dict[str, Any]] = {
    QWEN: {"temperature": 0.0, "max_tokens": 2048, "seed": 1234, "enable_thinking": False},
    DEEPSEEK: {"temperature": 0.0, "max_tokens": 2048, "seed": 1234},
}
# qwen4-max is a hybrid reasoning model: with thinking on, the reasoning consumed the whole output
# budget (observed in the first sql-src-1 attempt), so the search roles run with thinking off.
SEARCH_EXTRA: dict[str, Any] = {"enable_thinking": False}
N_SQL_SOURCES = 10
N_SO_SOURCES = 2
# per-run caps on top of the global caps (defence against a runaway run)
RUN_BUDGET = {"policy": 9000, "proposer": 40, "selector": 30, "critic": 10}


@dataclass
class RunSpec:
    name: str
    cfg: RunConfig
    source: str | None = None  # run to continue from
    pooled_sim_from: list[str] = field(default_factory=list)  # retrieval ablation: replace SIM with a pool
    group: str = ""


def _cfg(
    run_id: str, domain: str, evolve: str, heldout: str, policy: str, memory: str, seed: int, curve: bool
) -> RunConfig:
    return RunConfig(
        run_id=run_id,
        domain=domain,
        evolve_split=evolve,
        heldout_split=heldout,
        policy_model=policy,
        search_model=SEARCH,
        seed=seed,
        rounds=4,
        k=3,
        memory=memory,  # type: ignore[arg-type]
        heldout_curve=curve,
        delta_z=1.0,  # PREREG deviation 2
        beta1=40.0,  # PREREG deviation 2 (RRSI's default)
        w_c=5.0,  # PREREG deviation 2
        task_parallel=8,
        eval_parallel=3,
    )


def build_plan() -> dict[str, RunSpec]:
    plan: dict[str, RunSpec] = {}

    def add(spec: RunSpec) -> None:
        plan[spec.name] = spec

    for i in range(1, N_SQL_SOURCES + 1):
        add(
            RunSpec(
                f"sql-src-{i}",
                _cfg(f"sql-src-{i}", "text2sql", "spider_evolve", "spider_heldout", QWEN, "full", i, False),
                group="sql-source",
            )
        )
        for arm, mem in (("full", "full"), ("transfer", "none")):
            name = f"sql-{arm}-{i}"
            add(
                RunSpec(
                    name,
                    _cfg(name, "text2sql", "bird_evolve", "bird_heldout", QWEN, mem, 100 + i, i == 1),
                    source=f"sql-src-{i}",
                    group="sql-paired",
                )
            )
    add(
        RunSpec(
            "sql-cold",
            _cfg("sql-cold", "text2sql", "bird_evolve", "bird_heldout", QWEN, "none", 101, True),
            group="sql-conditions",
        )
    )
    add(
        RunSpec(
            "sql-problems-1",
            _cfg("sql-problems-1", "text2sql", "bird_evolve", "bird_heldout", QWEN, "problems", 101, True),
            source="sql-src-1",
            group="sql-conditions",
        )
    )

    for i in range(1, N_SO_SOURCES + 1):
        add(
            RunSpec(
                f"so-src-{i}",
                _cfg(f"so-src-{i}", "supportops", "so_evolve", "so_heldout", QWEN, "full", i, False),
                group="so-source",
            )
        )
    add(
        RunSpec(
            "so-cold",
            _cfg("so-cold", "supportops", "ood_evolve", "ood_heldout", QWEN, "none", 201, True),
            group="so-conditions",
        )
    )
    for arm, mem in (("transfer", "none"), ("problems", "problems"), ("full", "full")):
        add(
            RunSpec(
                f"so-{arm}",
                _cfg(f"so-{arm}", "supportops", "ood_evolve", "ood_heldout", QWEN, mem, 201, True),
                source="so-src-1",
                group="so-conditions",
            )
        )

    add(
        RunSpec(
            "xm-cold",
            _cfg("xm-cold", "text2sql", "bird_evolve", "bird_heldout", DEEPSEEK, "none", 301, True),
            group="xm-conditions",
        )
    )
    for arm, mem in (("transfer", "none"), ("problems", "problems"), ("full", "full")):
        add(
            RunSpec(
                f"xm-{arm}",
                _cfg(f"xm-{arm}", "text2sql", "bird_evolve", "bird_heldout", DEEPSEEK, mem, 301, True),
                source="sql-cold",
                group="xm-conditions",
            )
        )

    sources = [f"sql-src-{i}" for i in range(1, N_SQL_SOURCES + 1)]
    for s in (1, 2):
        for arm, mem in (("full", "full"), ("top", "retrieved")):
            name = f"ret-{arm}-{s}"
            add(
                RunSpec(
                    name,
                    _cfg(name, "text2sql", "bird_evolve", "bird_heldout", QWEN, mem, 400 + s, False),
                    source="sql-src-1",
                    pooled_sim_from=sources,
                    group="retrieval",
                )
            )
    return plan


def dependencies(spec: RunSpec) -> list[str]:
    deps = [spec.source] if spec.source else []
    return deps + [d for d in spec.pooled_sim_from if d not in deps]
