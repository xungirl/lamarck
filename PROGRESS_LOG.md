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

## 2026-11-19 (Study 2 prepared, not run)

- Follow-up requested: does a strong search model help? First drafted with DeepSeek `deepseek-v4-pro` as the
  search model; the plan changed to Qwen only before any code was committed, so no DeepSeek upstream,
  off-peak guard or DeepSeek API check exists in the repository.
- PREREG.md, new section "Study 2: strong search model", committed before any Study 2 run: `qwen4-max`
  proposer and selector (thinking off, max_tokens 8,192, as in the pre-revision run), `qwen3.8-flash` policy,
  `s2-src-1..5` (seeds 11-15) with the protocol of `sql-src-1..7`; primary outcome held-out change per run vs
  0 and vs the 7 controls; cap RMB 9.00 in `results/study2/budget/spend_ledger.json`; gate bound RMB 2.10/run.
- Cost estimate from the ledgers (`python -m lamarck.experiments.study2`): RMB 1.29 / 1.72 / 2.05 per run
  (low / central / high), RMB 6.5 / 8.6 / 10.3 for five runs; n = 4 is possible under the high estimate.
- Code: qwen4-max price (RMB 10 / 30 per 1M, no cache discount); `--plan study2` in the CLI; `seed-cache`
  (copies the seed harness's 8 cache files from runs/rev3: evolve_s30 r0-2 = 83.3 / 86.7 / 80.0, held-out_s40
  r0 = 80.0, plus parent-split files); simulated DashScope upstream (`gateway.server --simulate`);
  `scripts/api_check.py --plan study2`; `lamarck.experiments.study2_report`; `scripts/run_study2.sh`.
- Offline only, zero real LLM calls: `make ci` green (105 tests); `scripts/run_study2.sh --dry-run`
  (`make dryrun-study2`) ran the launcher end to end in a temp root in ~70 s: 5/5 runs done, seed evaluations
  reused byte for byte, simulated spend under the cap, report written. `make test-docker` not run (no
  sandbox or container code changed); Docker/colima not started.
- To run it: put `DASHSCOPE_API_KEY=...` in ./.env (or point `$LAMARCK_KEYFILE` at a file), then
  `scripts/run_study2.sh`. Resume = the same command. Results: `results/study2/`. Nothing pushed.

## 2026-11-20 UTC (Study 2 extension to 10 runs, local macOS run)

- Earlier the same day (EC2 Ubuntu arm64 host): Study 2 runs `s2-src-1..5` all `done`; results committed in
  26c6984 (mean held-out change +2.5, t(4) = 2.24, p = 0.089; Welch vs controls p = 0.14; RMB 5.624).
- Extension requested after that result. PREREG "Deviations (Study 2)" item 1 committed (268c6f3) before any new
  run: `s2-src-6..10` (seeds 16-20), optional stopping, so n = 5 stays the primary analysis and n = 10 is a
  labelled secondary analysis vs the same 7 controls; cap RMB 9.00 -> 15.00 in the same ledger; host platform
  (EC2 Linux vs macOS/colima) stated as a confound.
- Code: plan of 10 runs with `PRIMARY_RUNS` / `EXTENSION_RUNS`; cap 15.00; report writes `analysis` (primary,
  exactly runs 1-5) and `analysis_extended` (runs 1-10) plus runs 6-10 alone (descriptive); test that the
  primary analysis uses exactly runs 1-5. `make ci` green (106 tests); `make dryrun-study2` green (10/10 runs).
- Before launching: `lamarck --plan study2 status` showed runs 1-5 `done` (never pending in the scheduler) and
  the gate admitting 6-9 at RMB 5.624 spent; after the run the ledger entries of runs 1-5 are byte-identical to
  26c6984 (not re-run, not re-billed); seed-cache copied nothing.
- 06:56 UTC (22:56 PST) `LAMARCK_KEYFILE=<file outside the repo> scripts/run_study2.sh` on macOS: colima started,
  gVisor `runsc` registered by the script, API check HTTP 200 for qwen3.8-flash and qwen4-max (ledger at RMB
  5.624 before it), gateway image `lamarck-runtime:37e3c499562e`. Runs 6, 7, 8 started at once, 9 and 10 after
  the gate saw real spend; all five `done` by 07:07 UTC, no failures, no provider errors.
- Held-out changes of runs 6-10: +7.5, 0, +2.5, 0, 0 (two runs accepted no edit). Primary (runs 1-5) unchanged.
  Extended (runs 1-10): mean +2.25, t(9) = 2.59, p = 0.029, 95% CI [0.28, 4.22], signed-rank p = 0.062; vs
  controls Welch p = 0.132, Mann-Whitney p = 0.121. Not confirmatory (optional stopping, host confound).
- Money (results/study2/budget/spend_ledger.json): RMB 5.545 this round (runs 6-10 RMB 5.5448 + API check
  RMB 0.0004), RMB 11.169 of RMB 15.00 in total, 1,944 calls (2 failed, both from the EC2 batch).
- Report regenerated, README Study 2 paragraph extended. Key-leak scan: key value in no repo file, no git
  history, no diff, not in the launcher log. Gateway container removed and colima stopped by the launcher's
  trap; `colima stop` confirmed; no leftover gateway/eval processes. Nothing pushed.

## 2026-11-20 UTC (Study 3: confirmatory replication, local macOS run, stopped by the provider at 5 of 10)

- PREREG.md "Study 3" committed (3eb9e66) before `runs/study3` existed: `s3-src-1..10`, seeds 21-30, config =
  Study 2's except run ids and seeds; fixed n (no runs added after results; no re-runs except under the outage
  rule); primary one-sided one-sample t vs 0, α = 0.05; verdict "replicated" / "not replicated"; own ledger
  `results/study3/budget/spend_ledger.json`, cap RMB 15.00, gate bound RMB 2.10/run.
- Code (separate commits): `study2.source_plan` shared plan builder; one-sided t and exact signed-rank in
  `stats`; `study2_report.collect` (Study 2 output byte-identical: regenerated `results/study2` diffs clean,
  and a test re-renders the committed RESULTS.md / analyses exactly); `study3.py`, `study3_report.py`,
  `--plan study3` in CLI and api_check; launcher `scripts/run_study.sh PLAN` with `run_study2.sh` /
  `run_study3.sh` wrappers; `tests/test_study3.py`. `make ci` green (115 tests); `make dryrun-study2` and
  `make dryrun-study3` green (10/10 simulated runs each).
- 11:24 UTC `LAMARCK_KEYFILE=<file outside the repo> scripts/run_study3.sh`: colima started, gVisor `runsc`
  registered by the script, API check HTTP 200 for qwen3.8-flash and qwen4-max (RMB 0.0004), seed-cache from
  runs/rev3. Runs 1-3 started, 4-7 as runs finished (gate held 8-10 at first: 7 bounds fit).
- 11:34:50 UTC first upstream HTTP 403 `AccessDenied.Unpurchased` (account credit, both models): `s3-src-5`
  (in its final held-out evaluation, discarded) and `s3-src-7` (round 1 proposer) ended `infra_error`; the
  scheduler stopped launching; `s3-src-8..10` never started. Per the instruction (stop on 403/billing, no
  retry), nothing was resumed. 18 failed calls, all 403, all in those two runs; the five finished runs had 0.
- Finished n = 5 (`s3-src-1,2,3,4,6`): held-out changes 0, +2.5, +5.0, +2.5, +7.5, mean +3.5; one-sided
  t(4) = 2.75, p = 0.026; two-sided p = 0.052, 95% CI [−0.04, 7.04]; signed-rank p 0.125 two-sided / 0.0625
  one-sided. Rule verdict on n = 5: **replicated**. Secondary: vs controls Welch p = 0.067, Mann-Whitney
  p = 0.068; evolve mean +7.3 (controls +7.1). RMB 7.862 of 15.00, 1,325 calls.
- Open decision for the user: top up and resume `s3-src-5`/`s3-src-7` (outage rule) and launch `s3-src-8..10`
  (pre-registered n = 10), or keep n = 5 as final. Either choice is now made after seeing the n = 5 result.
- Key-leak scan clean (repo files, runs/study3, launcher log, git history, diff). colima stopped by the
  launcher's trap, `colima stop` confirmed; no leftover lamarck processes. Nothing pushed.

## 2026-11-20 UTC (Study 3: completion to n = 10 after the provider top-up; disclosure before resuming)

- The stop at n = 5 was a provider credit outage, not a choice. The user topped up DashScope and asked to
  complete Study 3 to the pre-registered n = 10; that decision was made after the interim n = 5 result
  (one-sided p = 0.026, "replicated" on n = 5) was visible. No rule changed (PREREG "Notes (Study 3)" item 1).
- Before resuming: fixed a resume defect (665a281; `s3-src-5` was paused in its final held-out evaluation,
  which a resume would have skipped, reporting 80.0 → 80.0); 41 cached evaluations in runs/study3 checked,
  none provider-tainted; the interrupted held-out evaluation of `s3-src-5` was never cached; `s3-src-7`'s
  sealed round-1 proposal s2 is reused, only the failed s1 call is repeated (same seed). Ledger entries,
  state.json and heldout.jsonl of `s3-src-1,2,3,4,6` snapshotted to compare after the round.
