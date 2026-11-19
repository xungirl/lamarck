"""Deterministic fake models for offline tests and CI.

  fake-policy   the toy domain's policy: obeys `RULE <kind>: <op>` lines in the system prompt
  fake-policy-alt  a simulated second model family for the cross-model (CRSI) plumbing: it upper-
                case items when no rule applies and misreads the op `sort` as `reverse`, so a
                harness evolved for fake-policy transfers only partly
  fake-search   proposer + selector for the toy domain: diagnoses a failing kind, proposes a rule,
                avoids operations already recorded as missed in SIM or tried in the run history,
                and selects the admissible candidate with the highest evolve score
  fake-sql      text-to-SQL policy stub: answers with a trivial query over the first table
  fake-support  support-ops policy stub: looks up the first order id it sees, then answers

They exist to exercise the plumbing (gateway, sandbox, engine, SIM) without a
network; they say nothing about real model behaviour.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..domains.toy.ops import OPS


def _text(body: dict[str, Any], role: str) -> str:
    return "\n".join(str(m.get("content") or "") for m in body.get("messages", []) if m.get("role") == role)


def _h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


# ----------------------------------------------------------------- toy policy
def fake_policy(body: dict[str, Any]) -> str:
    system, user = _text(body, "system"), _text(body, "user")
    m = re.search(r"kind=(\w+) item=(\w+)", user)
    if not m:
        return ""
    kind, item = m.groups()
    rule = re.search(rf"^RULE {kind}: (\w+)\s*$", system, re.M)
    if rule and rule.group(1) in OPS:
        return str(OPS[rule.group(1)](item))
    return item


def fake_policy_alt(body: dict[str, Any]) -> str:
    system, user = _text(body, "system"), _text(body, "user")
    m = re.search(r"kind=(\w+) item=(\w+)", user)
    if not m:
        return ""
    kind, item = m.groups()
    rule = re.search(rf"^RULE {kind}: (\w+)\s*$", system, re.M)
    if rule and rule.group(1) in OPS:
        op = "reverse" if rule.group(1) == "sort" else rule.group(1)
        return str(OPS[op](item))
    return item.upper()


# ----------------------------------------------------------------- toy search
def _sim_json(user: str) -> dict[str, Any]:
    m = re.search(r"SIM JSON:\n(\{.*?\n\})", user, re.S)
    if not m:
        return {}
    try:
        out: dict[str, Any] = json.loads(m.group(1))
        return out
    except json.JSONDecodeError:
        return {}


def _propose(body: dict[str, Any]) -> str:
    user = _text(body, "user")
    seed = int(body.get("seed") or 0)
    run_seed, sample = seed // 1000, seed % 10
    failing = re.findall(r"^- (\S+) \| kind=(\w+) item=(\w+)", user, re.M)
    rules_file = re.search(r"=== file: rules.md ===\n(.*?)(?=\n=== file:|\n### |\Z)", user, re.S)
    rules = rules_file.group(1).rstrip("\n") if rules_file else ""
    kinds = sorted({k for _, k, _ in failing})
    if not kinds:
        return json.dumps({"root_cause": {"cite": None, "name": "no failures"}, "what": "noop", "edits": []})
    kind = kinds[(run_seed + sample) % len(kinds)]
    name = f"no rule for kind {kind}"
    sim = _sim_json(user)
    tried = set(re.findall(rf"kind={kind} op=(\w+)", user))  # run history + SIM text
    for p in sim.get("problems", []):
        if p.get("name") == name:
            for a in p.get("attempts", []):
                if a.get("missed"):
                    tried.update(re.findall(r"op=(\w+)", a.get("what", "")))
    order = sorted(OPS, key=lambda op: _h(f"{run_seed}-{kind}-{op}"))
    op = next((o for o in order if o not in tried), order[0])
    cite = next((p["id"] for p in sim.get("problems", []) if p.get("name") == name), None)
    new_rules = (
        re.sub(rf"^RULE {kind}: \w+\n?", "", rules, flags=re.M).rstrip("\n") + f"\nRULE {kind}: {op}\n"
    )
    manifest = {
        "root_cause": {"cite": cite, "name": name},
        "what": f"add rule kind={kind} op={op}",
        "n_independent_edits": 1,
        "predicted": [tid for tid, k, _ in failing if k == kind],
    }
    return f'<manifest>\n{json.dumps(manifest)}\n</manifest>\n<file path="rules.md">\n{new_rules}</file>\n'


def _screen(body: dict[str, Any]) -> str:
    user = _text(body, "user")
    m = re.search(r"### CANDIDATES\n(\[.*?\n\])", user, re.S)
    cands = json.loads(m.group(1)) if m else []
    verdicts = [
        {
            "sample": c["sample"],
            "verdict": "reject" if "LEAK" in c.get("diff", "") else "accept",
            "reason": "fake",
        }
        for c in cands
    ]
    return json.dumps({"verdicts": verdicts})


def _select(body: dict[str, Any]) -> str:
    user = _text(body, "user")
    m = re.search(r"### ADMISSIBLE CANDIDATES\n(\[.*?\n\])", user, re.S)
    cands = json.loads(m.group(1)) if m else []
    if not cands:
        return json.dumps({"choice": "none", "reason": "nothing to choose"})
    best = max(cands, key=lambda c: (c["evolve_score"], -c["mean_tokens_per_task"]))
    return json.dumps({"choice": best["sample"], "reason": "highest evolve score"})


def fake_search(body: dict[str, Any]) -> str:
    system = _text(body, "system")
    if "harness engineer" in system:
        return _propose(body)
    if "strict reviewer" in system:
        return _screen(body)
    if "selector of an automated" in system:
        return _select(body)
    return "{}"


# ------------------------------------------------------------ domain stubs
def fake_sql(body: dict[str, Any]) -> str:
    text = _text(body, "system") + "\n" + _text(body, "user")
    m = re.search(r"CREATE TABLE\s+[\"`\[]?(\w+)", text, re.I)
    table = m.group(1) if m else "sqlite_master"
    return f"```sql\nSELECT count(*) FROM {table}\n```"


def fake_support(body: dict[str, Any]) -> str | dict[str, Any]:
    msgs = body.get("messages", [])
    if any(m.get("role") == "tool" for m in msgs):
        last = next(m for m in reversed(msgs) if m.get("role") == "tool")
        status = re.search(r'"status":\s*"(\w+)"', str(last.get("content")))
        return f"The order status is {status.group(1) if status else 'unknown'}."
    user = _text(body, "user")
    m = re.search(r"ORD-\d{5}", user)
    if m and body.get("tools"):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_order", "arguments": json.dumps({"order_id": m.group(0)})},
                }
            ],
        }
    return "I could not find an order id."


RESPONDERS: dict[str, Any] = {
    "fake-policy": fake_policy,
    "fake-policy-alt": fake_policy_alt,
    "fake-search": fake_search,
    "fake-sql": fake_sql,
    "fake-support": fake_support,
}
