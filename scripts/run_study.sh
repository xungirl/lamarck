#!/usr/bin/env bash
# Study 2 (PREREG.md, "Study 2: strong search model"): 10 Spider source runs (s2-src-1..5 pre-registered, 6..10
# the extension of Study 2 deviation 1) with qwen4-max as proposer and selector, policy qwen3.8-flash, everything
# else as sql-src-1..7. One command:
#
#   scripts/run_study2.sh              real run: API checks, seed-cache reuse, gateway, 10 runs behind the
#                                      money gate (cap RMB 15.00, own ledger results/study2/budget/), report
#   scripts/run_study2.sh --dry-run [DIR]
#                                      the same steps offline in a temp root (or DIR): simulated upstream
#                                      answering qwen3.8-flash / qwen4-max at real prices, subprocess sandbox,
#                                      no Docker, no key, no network
#
# Real run needs: colima + Docker with gVisor (see PROGRESS_LOG.md), data under data/, DASHSCOPE_API_KEY in
# ./.env (or the file named by $LAMARCK_KEYFILE). The key is read by the gateway only.
# Safe to re-run: finished runs are skipped, interrupted runs resume from their last round, the seed cache is
# never overwritten. The first study's ledger (results/budget/) is never opened.
set -euo pipefail
cd "$(dirname "$0")/.."
PARALLEL="${STUDY2_PARALLEL:-3}"
PY="$(uv run python -c 'import sys; print(sys.executable)')"

if [[ "${1:-}" == "--dry-run" ]]; then
  # ------------------------------------------------------------------ offline dry run
  TMP="${2:-$(mktemp -d "${TMPDIR:-/tmp}/lamarck-study2-dryrun.XXXXXX")}"
  mkdir -p "$TMP"
  TMP="$(cd "$TMP" && pwd)"
  export LAMARCK_BACKEND=local
  LAMARCK_GATEWAY_PORT="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
  export LAMARCK_GATEWAY_PORT
  PREV="$TMP/prev"            # stands in for runs/rev3 (the first study's root)
  ROOT="$TMP/study2"          # stands in for runs/study2
  SPEND="$TMP/budget/spend_ledger.json"   # stands in for results/study2/budget/spend_ledger.json
  CAP="$("$PY" -c 'from lamarck.cli import plan_money; print(plan_money("study2")[1])')"
  GW_PID=""
  stop_gw() { if [[ -n "$GW_PID" ]]; then kill "$GW_PID" 2>/dev/null || true; wait "$GW_PID" 2>/dev/null || true; GW_PID=""; fi; }
  trap stop_gw EXIT
  start_gw() {  # $1 state dir, $2 spend file, $3 cap
    "$PY" -m lamarck.gateway.server --state "$1" --host 127.0.0.1 --port "$LAMARCK_GATEWAY_PORT" \
      --simulate text2sql --spend-file "$2" --spend-cap "$3" > "$1.log" 2>&1 &
    GW_PID=$!
    for _ in $(seq 100); do
      curl -fs "http://127.0.0.1:$LAMARCK_GATEWAY_PORT/healthz" >/dev/null 2>&1 && return 0
      sleep 0.1
    done
    echo "simulated gateway did not start (see $1.log)"; exit 1
  }
  echo "dry run in $TMP (simulated upstream, port $LAMARCK_GATEWAY_PORT)"

  # 0. a previous study's cache of the seed harness (in a real run: runs/rev3, made with the real policy)
  mkdir -p "$PREV"
  start_gw "$PREV/gateway-state" "$TMP/prev-budget/spend_ledger.json" 9.50
  "$PY" -m lamarck.cli --root "$PREV" eval --domain text2sql --split spider_evolve_s30 --rollouts 0 1 2
  "$PY" -m lamarck.cli --root "$PREV" eval --domain text2sql --split spider_heldout_s40 --rollouts 0
  stop_gw

  # 1-5: the launcher's steps
  "$PY" scripts/api_check.py --plan study2 --simulate text2sql --spend-file "$SPEND"
  "$PY" -m lamarck.cli --plan study2 --root "$ROOT" seed-cache --from "$PREV"
  start_gw "$ROOT/gateway-state" "$SPEND" "$CAP"
  "$PY" -m lamarck.cli --plan study2 --root "$ROOT" schedule --parallel "$PARALLEL" | tee -a "$TMP/schedule.out"
  stop_gw
  "$PY" -m lamarck.experiments.study2_report --root "$ROOT" --out "$TMP/results" --spend-file "$SPEND"

  # checks: every run done, seed evaluations reused (not re-run), money within the cap, report written
  "$PY" - "$TMP" <<'EOF'
import json, sys
from pathlib import Path
tmp = Path(sys.argv[1])
root, prev = tmp / "study2", tmp / "prev"
from lamarck.experiments import study2
plan = study2.build_plan()
states = {n: json.loads((root / "runs" / n / "state.json").read_text())["status"] for n in plan}
assert all(s == "done" for s in states.values()), states
prov = [json.loads(x) for x in (root / "cache" / "seed_cache_provenance.jsonl").read_text().splitlines()]
copied = {c["file"]: c for p in prov for c in p["copied"]}
tree = prov[0]["seed_trees"]["text2sql"]
for n in plan:
    cal = json.loads((root / "runs" / n / "calibration.json").read_text())
    assert cal["harness_tree"] == tree
    prev_scores = [round(json.loads((prev / "cache/evals/text2sql/spider_evolve_s30/qwen3.8-flash" / f"{tree}__r{r}.json").read_text())["score"], 6) for r in range(3)]
    assert cal["scores"] == prev_scores, (cal["scores"], prev_scores)
    start = json.loads((root / "runs" / n / "heldout.jsonl").read_text().splitlines()[0])
    assert start["after_round"] == -1 and start["tree"] == tree
for f in copied:
    a, b = (root / f).read_bytes(), (prev / f).read_bytes()
    assert a == b, f
spend = json.loads((tmp / "budget" / "spend_ledger.json").read_text())
assert spend["spent_rmb"] <= spend["cap_rmb"] == study2.SPEND_CAP_RMB
by = spend["by"]
assert by["model:qwen4-max"]["calls"] > 0 and by["model:qwen3.8-flash"]["calls"] > 0
summ = json.loads((tmp / "results" / "summary.json").read_text())
print(json.dumps({
    "dry_run_ok": True, "states": states, "seed_cache_files": len(copied),
    "spent_rmb_simulated": round(spend["spent_rmb"], 4), "calls": spend["calls"],
    "calls_by_model": {k: v["calls"] for k, v in by.items() if k.startswith("model:")},
    "rmb_by_run": {k: round(v["rmb"], 4) for k, v in by.items() if k.startswith("run:")},
    "n_analysed_primary": summ["analysis"]["n"],
    "n_analysed_extended": summ["analysis_extended"]["n"],
}, indent=1))
EOF
  exit 0
fi

# ---------------------------------------------------------------------- real run
ROOT=runs/study2
L="uv run python -m lamarck.cli --plan study2 --root $ROOT"
KEYFILE="${LAMARCK_KEYFILE:-.env}"
[[ -f "$KEYFILE" ]] || { echo "no key file $KEYFILE (DASHSCOPE_API_KEY=...); see README"; exit 1; }
[[ -d runs/rev3/cache/evals/text2sql ]] || echo "note: runs/rev3 cache missing; the seed harness will be evaluated anew"
mkdir -p "$ROOT"
git rev-parse HEAD >> "$ROOT/code_commit.txt"

STARTED_COLIMA=0
cleanup() {
  docker rm -f lamarck-gateway >/dev/null 2>&1 || true
  if [[ "$STARTED_COLIMA" == 1 ]]; then colima stop || true; fi
}
trap cleanup EXIT
if [[ "$(uname -s)" == Darwin ]]; then
  if ! colima status >/dev/null 2>&1; then colima start; STARTED_COLIMA=1; fi
  # colima regenerates /etc/docker/daemon.json on start, which drops the runsc runtime
  if ! docker info 2>/dev/null | grep -q runsc; then
    colima ssh -- sudo runsc install
    colima ssh -- sudo systemctl restart docker
  fi
else
  # native Linux (e.g. an EC2 host): Docker runs directly; register gVisor once if missing
  if ! docker info 2>/dev/null | grep -q runsc; then
    sudo runsc install
    sudo systemctl restart docker
  fi
fi

# the API check owns the Study 2 spend ledger while it runs: no gateway container may be up at the same time
docker rm -f lamarck-gateway >/dev/null 2>&1 || true
uv run python scripts/api_check.py --plan study2   # one tiny call per model; stops here on 403 / billing errors
[[ -d runs/rev3/cache/evals/text2sql ]] && $L seed-cache --from runs/rev3
$L gateway-up
$L schedule --parallel "$PARALLEL" | tee -a "$ROOT/schedule.out"
uv run python -m lamarck.experiments.study2_report
