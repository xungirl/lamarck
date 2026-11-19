# Progress log

Resume from `git status` + this file. Newest entries at the bottom. Every number in this
repository comes from a run on this machine (Apple M2, 8 cores, 24 GB, macOS; colima VM 6 CPU / 10 GB).

## 2026-11-18

- Read the SIM paper (arXiv 2611.18856) in full, the RRSI reference code (read-only clone, never executed),
  the sibling projects rrsi-sql (data, ledger idea) and agent-eval-mcp (support-ops env).
- Prior-art search (GitHub code search + web, 2026-11-18): SIM-inspired code already exists:
  `crimeacs/auto-improve` (`--sim` flag for a text-artifact improver, 4-pair study, MIT) and
  `JosephOIbrahim/Synapse` (`harness/sim`, schema/store "not wired yet"). So lamarck is NOT the first
  open-source SIM implementation; position it as an independent implementation of the full CRSI protocol
  (four conditions, paired study) on a regularized code-harness RSI engine with a sandboxed eval layer.
- Models on the DashScope endpoint (listed via /models): policy `qwen3.8-flash`, search `qwen4-max`;
  non-Qwen families offered: DeepSeek (`deepseek-v4.1-flash`, `deepseek-v4-pro`), Kimi, GLM, MiniMax.
- stage-1 engine core, stage-2 SIM, stage-3 gateway (tags). Then the loop + toy domain (offline e2e).
- stage-4 sandbox: gVisor `runsc` installed inside the colima VM (apt repo, release-20261116.0);
  gVisor cannot reach Docker's embedded DNS, so the gateway name is injected with --add-host.
  Escape probes: results/sandbox_probes.json (all escapes fail, gateway reachable).
- Note: agent-eval-mcp's stopped RRSI stage left stores/hooks/tasks on branch `rrsi-wip` (read-only for us).

## 2026-11-19 (provider outage, budget revision)

- 00:34-00:36: the provider account ran out of credit (403 `AccessDenied.Unpurchased`) while
  sql-src-1..6 were finishing; 2,138 calls got 403 and were scored as failures. Only sql-src-4 had
  finished before the first 403 (last call 08:32:27 UTC, first 403 08:34:14 UTC). Evaluator fixed to
  discard provider failures (commit 9cbc0c2); tainted runs set aside in runs/main/aborted/outage-20261119.
- Budget revision (PREREG deviation 3): RMB spend ledger with a hard cap of 9.50 (results/budget/),
  qwen3.8-flash for every role, N=3 k=2, sub-splits (20-40 tasks), 3 sql sources / 3 pairs,
  1 support-ops source, priority order + scheduler money gate, cross-model unfunded (fake-model test).
- 03:20 API check: one call, HTTP 200 (scripts/api_check.py). New experiment root runs/rev3 with the
  untainted cached evaluations of runs/main copied in (sub-splits are projected from them for free).
- colima restart drops gVisor from /etc/docker/daemon.json: re-register with
  `colima ssh -- sudo runsc install && colima ssh -- sudo systemctl restart docker`.
  The gateway container has restart policy unless-stopped: `docker rm -f lamarck-gateway` before
  `colima stop`, or it comes back on the next `colima start`.
- 03:23 first launch failed (relative --root -> git add of ignored path); fixed, RMB 0.035 lost.
- 03:25 relaunched: `uv run python -m lamarck.cli --root runs/rev3 schedule --parallel 4`
  (log runs/rev3/schedule.out, per-run logs runs/rev3/logs/). Resume = run the same command again.
- 03:38 evaluation race found (paired arms in separate processes evaluated their shared start at
  once; held-out start 60.0 vs 55.0). Cross-process flock per cache key + regression test;
  PREREG deviation 4; the six paired arms moved to runs/rev3/aborted/race/ (spend/calls snapshots
  there, subtracted in the report) and re-run by the same scheduler.
- 03:49 schedule done: all 18 funded runs `done`, none left unrun. Spend ledger: RMB 5.384 of 9.50,
  5,184 calls, 0 failed. xm-* (cross-model) unfunded by design.
- `make report` -> results/ (RESULTS.md, summary.json, cost.json, runs/), dashboard rebuilt.
  `make test-docker` (fake models) passed after the run; gateway container removed; colima stopped.
- Docs: README.md, REPORT.md, docs/linkedin/ (3 draft series, not posted), INTERVIEW.md Q&A (36).
- Key-leak check: the DashScope key string occurs in no tracked/untracked file and nowhere in git history.
- Next (needs money): the pre-registered full plan (10 pairs, N=4, k=3, full splits) and the xm-* runs.
  Resume/reproduce: scripts/reproduce.sh. Nothing was pushed; no remote exists.
- ~04:10 extension requested ("继续跑"): PREREG deviation 5 committed before launch (4 extra pairs,
  sources 4-7, from the most expensive observed pair RMB 0.852 and RMB 0.50 headroom; secondary
  analysis, primary 3-pair analysis unchanged). Gate: `--reserve-rmb 0.5`, a source claims its arms.
- 04:36 colima start, gVisor re-registered, API check HTTP 200; 04:37-04:51 pairs 4-7 all `done`.
  Spend: RMB 8.181 total (RMB 2.797 this round), 7,563 calls, 0 failed. Extra source seed skipped
  (RMB 1.32 left, below headroom + RMB 1). Gateway removed, colima stopped.
