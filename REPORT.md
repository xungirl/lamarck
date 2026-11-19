# lamarck: a pilot-scale, budget-limited test of Self-Improvement Memory on a regularized harness-RSI engine

Technical report, 2026-11-19. Every number below is produced by `make report` and can be traced to
`results/summary.json` (key given in brackets), `results/RESULTS.md`, `results/budget/spend_ledger.json`,
`results/validity.json`, `results/pre_revision/` or the exported run files in `results/runs/`. Scores
are execution accuracy / task success in points (0-100); one rollout per task; the policy runs at
temperature 0.

## 1. Summary

lamarck implements SIM (Yu et al., arXiv 2611.18856) and the CRSI protocol (Cold Start, Harness
Transfer, Problem-Only Memory, Full SIM; a paired Full-SIM-vs-Transfer study over independent source
runs) on an RRSI-style engine (Xia et al., arXiv 2609.24972), with gVisor-sandboxed evaluation and a
gateway that enforces an RMB budget per call. After the provider account ran out of credit during the
first, full-size attempt, the study was re-planned to fit RMB 9.50 (PREREG deviation 3) and run with one
model, `qwen3.8-flash`, as policy, proposer and selector; 3 rounds × 2 candidates; 15-40-task splits.
With the money left, four more pairs were added afterwards (deviation 5); because that decision was made
after the 3-pair result was known, the 7-pair analysis is reported separately as secondary and the 3-pair
analysis remains the primary one.

Findings at this scale:

1. **RQ1 (primary): no detectable effect.** Full SIM − Harness Transfer on BIRD held-out, three pairs:
   0.0, −10.0, +5.0 points; mean −1.67, 95% CI [−20.6, +17.3], paired t(2) = −0.38, p = 0.74; exact
   signed-rank p = 1.0 [`paired.stats`]. With three pairs the exact test cannot go below p = 0.25 and one
   task is 2.5 points, so this is a pilot measurement, not a test of the paper's claim.
   **Extended, secondary (n = 7):** Δ = 0.0, −10.0, +5.0, 0.0, −2.5, 0.0, 0.0; mean −1.07, 95% CI
   [−5.26, +3.12], t(6) = −0.63, p = 0.555; exact signed-rank p = 0.750 [`paired_extended.stats`].
   Still no detectable effect; the interval contains both zero and the paper's mean gain (+2.36).
2. **Spider-evolved harnesses mostly transferred negatively to BIRD.** Six of seven source-evolved
   harnesses started below the untouched seed harness on BIRD held-out (55.0-65.0 vs 67.5; source 7:
   72.5) [`paired_extended.pairs[].start`, `conditions.sql`]. On Spider itself every source run raised
   evolve accuracy (83.3 → 86.7-93.3) while held-out went 80.0 → 77.5, 77.5, 77.5, 82.5, 80.0, 80.0, 85.0
   (mean 80.0) [`source`]: on average the edits fit the 30 evolve tasks without generalizing.
3. **Four conditions, Spider → BIRD (single runs):** Cold Start 67.5 → 65.0, Harness Transfer 55.0 →
   60.0, Problem-Only 55.0 → 65.0, Full SIM 55.0 → 60.0 [`conditions.sql`]. No condition ended above the
   seed harness's starting 67.5.
4. **Support-ops was at ceiling.** All four conditions scored 100.0 on the 15-task shifted-policy
   held-out split before and after RSI [`conditions.supportops`]. The source run itself improved (evolve
   75.0 → 95.0, held-out 80.0 → 95.0 on 20 tasks [`source`]).
5. **Measurement noise is as large as the effects.** The same harness on the same 40 held-out tasks at
   temperature 0 scored 60.0 and 55.0 (also 67.5 / 62.5 and 65.0 / 70.0) in two evaluations
   [`paired_raced.start_by_arm`]; three evaluations of one harness on 30 BIRD tasks gave 46.7, 36.7,
   46.7 (sd 5.8) [`eval_variance`].
6. **Engineering:** RMB 8.181 in total (7,563 calls): RMB 5.384 for the revised plan, RMB 2.797 for the
   four extra pairs [spend ledger]. A provider outage
   and a cross-process evaluation race were both caught, fixed with regression tests and documented,
   rather than absorbed into the results.

## 2. System

**Engine** (`lamarck/engine/`). Each run owns a git repository containing only `harness/`. The
incumbent is the tip of `evolve`; candidate j of round t is drafted on branch `cand/r<t>s<j>` in its own
worktree and accepted by fast-forward. Per round: evaluate the incumbent; k proposals in parallel, each
sealed to disk with its root cause and predicted fixes before evaluation; a deterministic critic
(leakage patterns in added lines, compilation, import allow-list, no `open/exec/eval`, declared edits ≤
b_t) and an LLM screen; sandboxed evaluation; RRSI judgement (noise floor S′ ≥ S* − δ, cost rule above
the band, shaped rule inside it, domain guards); LLM selection restricted to admissible candidates;
history and SIM update. δ = max(z·sd, 1/n) is calibrated from three evaluations of the run's starting
harness.

**SIM** (`lamarck/sim/`). The App. D.3 record: problems keyed by root cause; attempts with what changed,
files, predicted / fixed / missed tasks, score and provenance (`split/model`). The proposer cites an
existing problem id or opens a new one; identical root-cause texts merge deterministically with aliases
(App. D.1). Property tests check that each update appends exactly one attempt and that merges are
lossless, idempotent and deterministic. The memory condition controls what the proposer and selector
see; every run records its own attempts regardless. Retrieval extension: BM25 + embeddings fused by
reciprocal rank, top-k under a token budget.

**Gateway** (`lamarck/gateway/`). The only holder of the API key. Per-run grants (roles, models,
budgets), call caps, token buckets, retries that count against budgets, an audit log of request hashes
(no content). The **spend ledger** reserves a worst-case price before each call (one input token per
request character plus `max_tokens` output tokens, an upper bound), refuses the call if spent + in
flight + worst case would exceed the cap, then settles at the price of the returned usage (cached prompt
tokens at the cached rate; failed calls unbilled; timeouts charged the worst case). Unknown models fail
closed; one process owns the ledger file (flock). Price used: `qwen3.8-flash` RMB 0.80 / 2.70 / 0.10 per
1M input / output / cached input tokens.

**Sandbox** (`lamarck/sandbox/`). One fresh container per evaluation job under gVisor `runsc`:
read-only root and mounts (harness, job, this split's data only), one writable `/out`, user nobody, no
capabilities, CPU / memory / pid limits, a host-side timeout, and an internal-only network whose only
peer is the gateway (injected with `--add-host`, since gVisor cannot reach Docker's embedded DNS).
Escape probes (external IPs, DNS, cloud metadata, the host, read-only mounts, the Docker socket, raw
sockets) all fail and the gateway is reachable [`results/sandbox_probes.json`]. Gold answers never enter
the sandbox; scoring happens on the host.

**Domains.** Text-to-SQL: Spider 1.0 dev (source) → BIRD mini-dev SQLite (target), execution accuracy
on read-only databases, at most 2 policy calls per task. Support-ops: an MCP tool environment from the
author's agent-eval-mcp project, 110 tasks (gold replay 110/110); source = standard refund policy,
target = shifted policy (14-day window, lower limit, rewritten knowledge base, new prompt-injection
carriers, shifted request styles).

**Experiment layer.** The plan is code (`lamarck/experiments/plan.py`). A scheduler runs it in
dependency and priority order with a **money gate**: a run starts only if the money left, minus
in-flight reservations, minus the remaining estimate of running runs, minus the estimates of every
higher-priority pending run, still covers its own estimate. `stats.py` reproduces the paper's Table 3
statistics from its per-run data (tests).

## 3. Pre-registration, deviations and the outage

`PREREG.md` fixed research questions, splits (with hashes), settings and analysis rules before any
experiment. Five deviations were appended:

1. Search roles with thinking off and a manifest + verbatim-file proposal format (the first proposer call
   spent its whole output budget on reasoning).
2. Noise band z = 1 instead of 2, RRSI's default β1, a softer within-band cost weight, 2 calls per SQL
   task: with the original settings 0 of 21 candidates were admissible, so no run could produce an edit.
3. **Budget revision** after the outage: RMB cap 9.50, one model for all roles, N = 3, k = 2, sub-splits,
   3 pairs instead of 10, a priority order, cross-model unfunded.
4. **Evaluation race.** The two arms of a pair run in separate processes; the cache lock was per
   process, so both arms evaluated their common start at once and the later write replaced the earlier.
   The arms did not share one measurement of their start (60.0 vs 55.0 in pair 1). Fixed with a
   cross-process file lock; the six paired arms were re-run; the first execution is reported in §5.6 and
   never pooled. Decided after the per-arm logs of pairs 1-2 had been seen, before any paired difference
   was computed.
5. **Extension to 7 pairs** (requested with the remaining money). Decided after the 3-pair result and
   before any new-pair data: four pairs (sources 4-7), the number fixed from the most expensive observed
   pair (RMB 0.852) and RMB 0.50 of headroom; same protocol, configuration, splits and code; analysed
   separately as secondary. The four pairs cost RMB 0.701, 0.886, 0.554 and 0.654.

**The outage.** At 08:34:14 UTC on 2026-11-19 the provider began returning HTTP 403
`AccessDenied.Unpurchased` (account out of credit). 2,138 calls got a 403; the evaluator of that time
scored them as failed tasks, producing held-out scores such as 0/120. Only `sql-src-4` had finished
before the first 403 (last call 08:32:27 UTC) [`results/validity.json`]. The evaluator now raises on
provider and gateway failures, never caches such an evaluation and pauses the run; stale tainted cache
files are set aside.

## 4. Revised design (what actually ran)

| block | runs | evolve / held-out split (tasks) |
|---|---|---|
| Source RSI, text-to-SQL | sql-src-1..7 (seeds 1-7), building SIM; 4-7 added by deviation 5 | spider_evolve_s30 (30) / spider_heldout_s40 (40) |
| Paired study (RQ1) | sql-full-i vs sql-transfer-i from sql-src-i: i = 1..3 primary, i = 1..7 extended | bird_evolve_s30 (30) / bird_heldout_s40 (40) |
| Four conditions, text-to-SQL | sql-cold, sql-transfer-1, sql-problems-1, sql-full-1 | same |
| Source RSI, support-ops | so-src-1 | so_evolve_s20 (20) / so_heldout_s20 (20) |
| Four conditions, support-ops | so-cold, so-transfer, so-problems, so-full | ood_evolve (15) / ood_heldout (15) |
| Retrieval (RQ4) | ret-full-1, ret-top-1 (pooled SIM of sql-src-1..3, from sql-src-1's harness) | bird_evolve_s30 / bird_heldout_s40 |

Sub-splits are seeded uniform samples of the frozen splits; an evaluation on a sub-split is taken from a
cached evaluation of its parent split when one exists (tasks are solved independently at temperature 0
with a fixed seed). All 30 runs finished; the money gate delayed some launches but left none unrun, and the cap was never reached.

## 5. Results

### 5.1 Source RSI [`source`]

| run | evolve before → after | held-out before → after | accepted | RMB |
|---|---|---|---|---|
| sql-src-1 | 83.33 → 90.00 | 80.00 → 77.50 | 1 | 0.144 |
| sql-src-2 | 83.33 → 93.33 | 80.00 → 77.50 | 2 | 0.134 |
| sql-src-3 | 83.33 → 90.00 | 80.00 → 77.50 | 1 | 0.103 |
| sql-src-4 | 83.33 → 90.00 | 80.00 → 82.50 | 1 | 0.136 |
| sql-src-5 | 83.33 → 90.00 | 80.00 → 80.00 | 1 | 0.128 |
| sql-src-6 | 83.33 → 93.33 | 80.00 → 80.00 | 1 | 0.108 |
| sql-src-7 | 83.33 → 86.67 | 80.00 → 85.00 | 1 | 0.123 |
| so-src-1 | 75.00 → 95.00 | 80.00 → 95.00 | 1 | 0.177 |

Five of seven Spider sources (1, 2, 3, 5, 6) independently added the same kind of edit: execute the
drafted SQL and, if it returns no rows or fails, ask the model to repair it (sql-src-2 later added sample
rows to the repair prompt) [`results/runs/sql-src-*/history.jsonl`]; held-out went 77.5-80.0 in those
runs. Sources 4 and 7 made prompt-only edits (case-insensitive text matching, selecting full entity
records, NULL handling) and ended at 82.5 and 85.0 held-out; with one run each this is a description,
not a comparison. The support-ops edit (write tools only when the request unambiguously asks for them) generalized:
+3 of 20 held-out tasks. The pre-revision run with `qwen4-max` as proposer on the full splits (60 / 120
tasks) went 78.33 → 88.33 evolve and 81.67 → 86.67 held-out [`results/pre_revision/summary.json`]; a
stronger proposer or a larger evolve set may matter, but one run cannot show it.

### 5.2 Paired study, RQ1 [`paired`]

| source run | start (H_S on BIRD held-out) | No SIM | Full SIM | Δ |
|---|---|---|---|---|
| 1 | 55.00 | 60.00 | 60.00 | +0.00 |
| 2 | 62.50 | 72.50 | 62.50 | −10.00 |
| 3 | 65.00 | 62.50 | 67.50 | +5.00 |

Mean Δ −1.67 points, 95% CI [−20.64, +17.31], t(2) = −0.38, p = 0.742, exact signed-rank p = 1.000,
d_z = −0.22; SIM wins 1, loses 1, ties 1. On the evolve split, which the search optimizes, the mean Δ is
0.00 (p = 1.000) [`paired.evolve_stats`]. Pair 2's −10 comes from one accepted edit in the transfer arm
that moved held-out 62.5 → 72.5 while its evolve score stayed at 46.7; the Full SIM arm accepted
nothing. A held-out change of this size with no evolve change is what evaluation noise looks like
(§5.5), not a memory effect.

**Extended analysis, pairs 1-7 (deviation 5; secondary)** [`paired_extended`]:

| source run | start | No SIM | Full SIM | Δ |
|---|---|---|---|---|
| 1 | 55.00 | 60.00 | 60.00 | +0.00 |
| 2 | 62.50 | 72.50 | 62.50 | −10.00 |
| 3 | 65.00 | 62.50 | 67.50 | +5.00 |
| 4 | 62.50 | 65.00 | 65.00 | +0.00 |
| 5 | 65.00 | 67.50 | 65.00 | −2.50 |
| 6 | 60.00 | 60.00 | 60.00 | +0.00 |
| 7 | 72.50 | 60.00 | 60.00 | +0.00 |

Mean Δ −1.07 points, 95% CI [−5.26, +3.12], t(6) = −0.63, p = 0.555, exact signed-rank p = 0.750,
d_z = −0.24; SIM wins 1, loses 2, ties 4. Evolve-split mean Δ −0.48 (p = 0.788). Four of the seven pairs
end tied: in three or fewer rounds the two arms often accept no edit, or edits that do not move the 40
held-out tasks. Because pairs 4-7 were added after the primary result was seen, this analysis carries an
optional-stopping caveat; it is consistent with the primary one and does not replace it.

### 5.3 Four conditions [`conditions`]

Text-to-SQL, Spider → BIRD (held-out after rounds 0, 1, 2; held-out is re-evaluated only when the
retained harness changes):

| condition | start | r0 | r1 | r2 | evolve | accepted |
|---|---|---|---|---|---|---|
| Cold Start | 67.5 | 67.5 | 65.0 | 65.0 | 43.3 → 43.3 | 1 |
| Harness Transfer | 55.0 | 60.0 | 60.0 | 60.0 | 43.3 → 46.7 | 1 |
| Problem-Only Memory | 55.0 | 55.0 | 65.0 | 65.0 | 43.3 → 50.0 | 2 |
| Full SIM | 55.0 | 55.0 | 55.0 | 60.0 | 43.3 → 50.0 | 1 |

Support-ops, standard → shifted policy: every condition scored 100.0 at the start and after every round;
evolve (15 tasks) Cold Start 93.3 → 100.0, Transfer 86.7 → 100.0, Problem-Only 86.7 → 100.0, Full SIM
86.7 → 93.3. The 15-task shifted held-out split is saturated by the seed harness, so it cannot separate
the conditions at this size.

### 5.4 Retrieval ablation, RQ4 [`retrieval`]

Pooled SIM of the three Spider sources, continued from sql-src-1's harness: full pooled SIM 55.0 → 65.0
(evolve 46.7); top-4 retrieved under a 1,200-token budget 55.0 → 62.5 (evolve 53.3). Search tokens were
nearly equal (57,417 vs 57,585) because three source runs produced only three SIM entries: the pool is
far smaller than the budget at which retrieval would change what the proposer reads. One seed each;
descriptive.

### 5.5 Measurement noise [`eval_variance`]

Repeat evaluations of one harness at temperature 0: the seed harness on spider_evolve (60 tasks) 78.3 /
85.0 / 78.3; on bird_evolve_s30 43.3 / 46.7 / 43.3; sql-src-2's harness on bird_evolve_s30 46.7 / 36.7
/ 46.7 (sd 5.8); the support-ops seed harness on ood_evolve 93.3 / 73.3 / 80.0 (sd 10.2). On the 40
held-out tasks, two evaluations of one unchanged harness differed by up to 5 points (§5.6), half the size
of pair 2's Δ.

### 5.6 Set-aside first execution of the paired arms (deviation 4) [`paired_raced`]

| source run | start (SIM arm / No-SIM arm) | No SIM | Full SIM | Δ |
|---|---|---|---|---|
| 1 | 60.00 / 55.00 | 62.50 | 62.50 | +0.00 |
| 2 | 67.50 / 62.50 | 60.00 | 62.50 | +2.50 |
| 3 | 65.00 / 70.00 | 70.00 | 65.00 | −5.00 |

Mean Δ −0.83 (95% CI [−10.32, +8.65]): the same picture as the re-run. The start column measures
held-out noise for an unchanged harness.

### 5.7 Search with a flash model [`search`, `runs.*`]

The 30 runs had 180 candidate slots: 155 usable proposals (unusable: 16 manifests without file blocks, 4
unterminated JSON objects, 3 without a root cause, 2 without edits), 62 dropped by the critic or the LLM
screen, 93 evaluated, 37 accepted. For the attempts each run recorded itself, proposers predicted 170 task
fixes and 49 were realized (29%); 67 proposals cited an existing SIM problem. Using one flash model for
proposing and selecting is the main design difference from the paper and plausibly limits what a memory
can add.

### 5.8 Cost [spend ledger, `results/cost.json`]

RMB 8.181 of the RMB 9.50 cap for 7,563 calls: policy RMB 6.846 (7,185 calls), proposer RMB 1.003 (226),
selector RMB 0.332 (152); RMB 5.384 of it for the revised plan and RMB 2.797 for the four extra pairs.
6.16M of 11.11M policy prompt tokens (55%) were billed at the cached rate. The revised schedule ran from
03:25 to 03:49 local time, including the re-run of the paired arms, and the extension from 04:37 to
04:51 (first and last lines of `results/runs/*/events.log`), four runs in parallel.
Pre-revision usage (the full plan, outage included) is summarized in
`results/pre_revision/summary.json`; it cannot be priced exactly because no price is known for
`qwen4-max` or `deepseek-v4.1-flash`.

## 6. Discussion

The paper reports a consistent gain of Full SIM over Harness Transfer across ten paired source runs.
This pilot neither supports nor contradicts it: the primary interval spans about ±20 points, and the
secondary 7-pair interval [−5.3, +3.1] still contains both zero and the paper's mean gain of +2.36. What it
does show is where a small study breaks. (a) With 30 evolve tasks an accepted edit moves 1-3 tasks, the
same size as evaluation noise at temperature 0. (b) With three rounds and two candidates a run accepts
0-3 edits, so a SIM holds one to three problems and the transferred history is short. (c) The
transferred harness itself was a liability on BIRD (a Spider-tuned repair loop), so every non-cold
condition started 2.5-12.5 points behind Cold Start and spent its few rounds recovering. (d) The
support-ops target split was too easy. A credible test needs larger evolve splits (or repeated rollouts
per task), more rounds, a stronger proposer and at least ten pairs, as in the paper's design. Seven
pairs did not change the picture: most pairs tie because each arm accepts at most a few small edits.

## 7. Threats to validity

- **Scale and power:** n = 3 pairs (primary), 7 (secondary); single runs per condition; 15-40-task splits.
- **Optional stopping:** pairs 4-7 were added after the primary result was known (deviation 5); hence the
  separate, secondary analysis.
- **One model, one family:** `qwen3.8-flash` played every role; results may not transfer to stronger
  proposers or other families. The cross-model transition (RQ2c) was not run.
- **Noise at temperature 0:** the provider is not deterministic; one rollout per task.
- **Deviations after data:** deviations 2-5 were decided after some data had been seen (stated in
  PREREG.md for each); deviation 5 followed the 3-pair paired result, none of the others used a paired difference or a
  held-out comparison between conditions.
- **Raced first execution:** set aside and reported (§5.6), not pooled. Cached evaluations made during it
  were kept as the shared measurement for the re-run (each is a complete evaluation of its harness).
- **Support-ops ceiling:** that transition is uninformative at this size.
- **Implementation fidelity:** the paper ships no code; prompts and the SIM-usage guidelines follow the
  paper's appendix as written but cannot be checked against the authors' implementation.

## 8. Future work

- Fund the pre-registered full plan (10 pairs, N = 4, k = 3, full splits, with pairs fixed in advance) and the cross-model transition
  (`xm-*` runs: in the plan, exercised offline with simulated model families in
  `tests/test_engine_toy.py`).
- Repeated rollouts per task to shrink δ, or larger evolve sets.
- A harder shifted-policy support-ops split.
- Retrieval over SIM pools large enough for the token budget to bind (tens of entries).

## 9. Reproducibility

`scripts/reproduce.sh` runs the revised study end to end (gVisor re-registration, a one-call API check,
gateway, scheduler with the money gate, report). `make ci` runs 91 offline tests with fake models.
`PROGRESS_LOG.md` records the operational history.
