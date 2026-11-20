"""A simulated DashScope upstream for offline dry runs of real experiment plans (no network, no key).

It answers for the REAL model names a plan uses (`qwen3.8-flash`, `qwen4-max`), in DashScope's response
shape (usage with `prompt_tokens_details.cached_tokens`), so a dry run exercises the gateway's routing, the RMB
spend ledger with real prices and the hard cap, the scheduler's money gate and the report exactly as a real run
would, except that no request leaves the process. It is selected with `lamarck.gateway.server --simulate` and
refuses to run next to a real key.

  text2sql  policy: returns the gold query of the task (looked up on the host side from the question) when a
            hash of (system prompt, question, seed) falls under a threshold that rises a little with every
            "Tip" line in the system prompt, otherwise a wrong query; so different harnesses score differently,
            repeated evaluations vary, and an edit can "help".
            search: proposer appends one generic tip line to system_prompt.md; screen accepts; selector picks
            the highest evolve score (lamarck.gateway.fakes).
  toy       policy = fake-policy, search = fake-search.

Token usage is a deterministic function of the text (about 4 characters per token, never more tokens than
characters), with a cached prefix reported for long prompts. Numbers say nothing about real models.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from . import fakes
from .providers import UpstreamError

Responder = Callable[[dict[str, Any]], str | dict[str, Any]]
POLICY_MODEL, SEARCH_MODEL = "qwen3.8-flash", "qwen4-max"


def _h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


@lru_cache(maxsize=1)
def _gold_by_question() -> dict[tuple[str, str], str]:
    from ..domains.text2sql import data

    return {(t["db_id"], t["question"]): t["gold_sql"] for t in data.tasks().values()}


def sim_sql_policy(body: dict[str, Any]) -> str:
    system, user = fakes._text(body, "system"), fakes._text(body, "user")
    db = re.search(r"^Database: (\S+)", user, re.M)
    q = re.search(r"^Question: (.*)$", user, re.M)
    gold = _gold_by_question().get((db.group(1), q.group(1).strip())) if db and q else None
    tips = len(re.findall(r"^- Tip ", system, re.M))
    threshold = min(95, 78 + 3 * tips)
    if gold and _h(f"{system}|{q.group(1) if q else ''}|{body.get('seed')}") % 100 < threshold:
        return f"```sql\n{gold}\n```"
    return "```sql\nSELECT 1\n```"


def _sim_sql_propose(body: dict[str, Any]) -> str:
    user = fakes._text(body, "user")
    seed = int(body.get("seed") or 0)
    failing = re.findall(r"^- (\w+) \| db=", user, re.M)
    prompt = re.search(r"=== file: system_prompt.md ===\n(.*?)(?=\n\n=== file:|\n\n### |\Z)", user, re.S)
    text = (prompt.group(1) if prompt else "").rstrip("\n")
    manifest = {
        "root_cause": {"cite": None, "name": f"generic SQL mistake {seed % 7}"},
        "what": f"add a generic checking tip ({seed})",
        "n_independent_edits": 1,
        "predicted": failing[:3],
    }
    tip = f"- Tip {seed}: re-read the question and check the selected columns, joins and aggregation."
    return f'<manifest>\n{json.dumps(manifest)}\n</manifest>\n<file path="system_prompt.md">\n{text}\n{tip}\n</file>\n'


def sim_sql_search(body: dict[str, Any]) -> str:
    system = fakes._text(body, "system")
    if "harness engineer" in system:
        return _sim_sql_propose(body)
    return fakes.fake_search(body)


RESPONDERS: dict[str, dict[str, Responder]] = {
    "text2sql": {POLICY_MODEL: sim_sql_policy, SEARCH_MODEL: sim_sql_search},
    "toy": {POLICY_MODEL: fakes.fake_policy, SEARCH_MODEL: fakes.fake_search},
}


class SimulatedDashScope:
    """Stands in for DashScopeProvider: same request/response shape, no network."""

    name = "simulated-dashscope"

    def __init__(self, domain: str) -> None:
        if domain not in RESPONDERS:
            raise ValueError(f"no simulation for domain {domain!r}")
        self.responders = RESPONDERS[domain]
        self.calls = 0

    def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        model = str(body.get("model", ""))
        if model not in self.responders:
            raise UpstreamError(f"upstream 404: model {model!r} not simulated", 404, transient=False)
        self.calls += 1
        out = self.responders[model](body)
        message: dict[str, Any] = {"role": "assistant", "content": out} if isinstance(out, str) else out
        chars = len(json.dumps(body.get("messages", []), ensure_ascii=False))
        prompt = max(1, chars // 4)
        completion = min(
            int(body.get("max_tokens") or 16_384), 1 + len(str(message.get("content") or "")) // 4
        )
        cached = (prompt // 2) // 256 * 256  # a shared prefix served from cache, in 256-token blocks
        return {
            "id": "sim",
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "prompt_tokens_details": {"cached_tokens": cached},
            },
        }
