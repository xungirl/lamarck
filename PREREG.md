# Pre-registration

Written and committed **before any experiment run** (2026-11-19, before the first H0 baseline
or calibration evaluation with a real model). Deviations are appended at the bottom with a
date and a reason; nothing above the "Deviations" line is edited after the first run.

## Research questions

- **RQ1 (primary, confirmatory).** On the text-to-SQL transition Spider → BIRD with the policy
  fixed (`qwen3.8-flash`), does continuing harness RSI **with Full SIM** end with a higher held-out
  BIRD execution accuracy than continuing **without SIM** (Harness Transfer) from the *same*
  source-evolved harness? Unit of analysis: the independent source run (the paper's Table 3 design).
- **RQ2 (descriptive).** How do the four conditions (Cold Start, Harness Transfer, Problem-Only
  Memory, Full SIM) compare on (a) Spider → BIRD, (b) support-ops standard policy → shifted policy,
  (c) cross-model: harness evolved with `qwen3.8-flash` on BIRD, continued with
  `deepseek-v4.1-flash` (DeepSeek family) on the same BIRD split?
- **RQ3 (descriptive).** Does the full record help beyond root causes only (Full SIM vs Problem-Only)?
- **RQ4 (exploratory extension).** With a memory pooled from many source runs, does top-k retrieved
  SIM under a token budget do as well as the full pooled SIM in the proposer/selector context?

## Data and splits (frozen)

| file | sha256 |
|---|---|
| `data/splits/text2sql.json` | `64f2311c9667cd0aa42f5d585a07f0b4924ff2e6f1dd205268223e9105571474` |
| `data/splits/supportops.json` | `84f61ecc9691e9ab4267d478e59aa12819e3514814ad4f658ba559626d1bd3a5` |
| `lamarck/domains/supportops/tasks_evolve.yaml` | `0085b77ec588f848a5ea3d07d8d67a593d9e887bba6181b1ca2603312c61faba` |
| `lamarck/domains/supportops/tasks_heldout.yaml` | `31a19a2803d2bdcaf855dbef704192dc8a2f18ec447639f41a59152c59d0a159` |
| `lamarck/domains/supportops/tasks_ood.yaml` | `b8415ab89adf9844701998ce57013e30c8aef0a7c1c8bef6eadbf21113b82904` |
| Spider 1.0 archive (`spider_data.zip`) | `00636695dabed6b5f4b8328a16b13e069a2f16591d5efcce57660669c85b121b` |
| BIRD mini-dev archive (`minidev.zip`) | `cc48ba16838204e4e214512030cb572eeb5f7bcdd999bae4b9b6ff12ec13b92f` |

- **Spider dev** (source): 20 databases shuffled with seed 20261118; 10 evolve-pool, 10 held-out-pool
  (database-disjoint). `spider_evolve` = 60 questions, `spider_heldout` = 120 questions, sampled
  uniformly from their pools.
- **BIRD mini-dev, SQLite** (target): `bird_evolve` = 60, `bird_heldout` = 120 questions, disjoint,
  uniform random (databases shared). One gold query (`bd0701`) exceeds 60 s on its own database and
  is excluded by the fixed rule "gold must execute within 60 s".
- **Support-ops** (from agent-eval-mcp `rrsi-wip@3b186b3`, gold replay re-validated in lamarck:
  110/110 tasks score 100%): `so_evolve` = 40 tasks (evolve store, 30-day window / $500 limit),
  `so_heldout` = 40 tasks (heldout store, same policy, different entities). Target = the `ood` store
  (14-day window / $250 limit, rewritten KB, new injection carriers, shifted request styles):
  `ood_evolve` = 15, `ood_heldout` = 15 (seeded uniform split of the 30 tasks).
- Only evolve-split scores and traces reach the proposer, selector and SIM. Held-out splits are
  evaluated outside the loop; their scores are written to files no prompt reads.

## Fixed method settings (all conditions, all domains)

- Rounds N = 4, candidates per round k = 3 (the paper uses N = 10, k = 5; scaled to the budget).
- Policy: temperature 0, seed 1234 (+ rollout index), max 2048 output tokens, one rollout per task;
  `enable_thinking=false` for `qwen3.8-flash`; DeepSeek with provider defaults.
  Text-to-SQL: at most 4 policy calls per task; support-ops: at most 16 (loop max_steps cap 12).
- Proposer: `qwen4-max`, temperature 0.8, one call per candidate, diversity hint per sample index.
  Selector (screen + select): `qwen4-max`, temperature 0. Both read SIM according to the condition.
- RRSI regularizers on: edit budget b_t annealed 3 → 1; noise band δ = max(2 · sd_null, 1/n) from
  3 evaluations of the run's starting harness (rollouts 0, 1, 2); noise floor S′ ≥ S* − δ; cost rule
  β0 = 0.10, β1 = 10, shaped rule w_s = 100, w_c = 15, w_n = 0.5; deterministic critic + LLM screen;
  prune set over the last 3 rounds; support-ops guard "unauthorized-write tasks must not increase".
- Memory conditions: Cold Start (seed harness, no SIM shown), Harness Transfer (source-evolved
  harness, no SIM shown), Problem-Only (same harness, root causes only), Full SIM (same harness,
  full source record, updated during target RSI). Every run records its own attempts to `sim.json`
  for analysis; what reaches the agents is controlled by the condition.
- Source runs start from the seed harness with an empty SIM that they build (Full SIM mode).
- Sandbox: Docker + gVisor, internal-only network to the gateway, read-only mounts of this split's
  data only.

## Experiments and endpoints

1. **Source RSI**: text-to-SQL on `spider_evolve` (10 independent source runs, seeds 1-10);
   support-ops on `so_evolve` (2 source runs, seeds 1-2); report held-out source score before/after.
2. **Paired study (RQ1)**: for each text-to-SQL source run i, two target runs on `bird_evolve`
   from its final harness: Full SIM (carrying source run i's SIM) and Harness Transfer.
   - Primary endpoint: `bird_heldout` EX of the harness retained after round N.
   - Statistic: mean paired difference (Full SIM − Harness Transfer), two-sided paired t-test,
     95% t confidence interval, exact two-sided Wilcoxon signed-rank (average ranks for ties,
     zero differences dropped, all sign assignments enumerated), paired effect size d_z.
     α = 0.05. n = 10 planned; if the policy budget cannot finish 10 pairs, as many complete pairs
     as possible in source-index order (minimum 5) are analysed and the shortfall is reported.
3. **Four conditions (RQ2, RQ3)**, one run per condition, held-out score after every round:
   (a) text-to-SQL Spider → BIRD from source run 1 (its paired arms serve as Harness Transfer and
   Full SIM); (b) support-ops `so_evolve` → `ood_evolve` from support-ops source run 1;
   (c) cross-model on BIRD: source = the qwen Cold Start run of (a); target policy
   `deepseek-v4.1-flash`, four conditions on `bird_evolve`, held-out `bird_heldout`.
   Reported descriptively (single runs), as in the paper.
4. **Retrieval ablation (RQ4)**: SIM pooled from all text-to-SQL source runs; continue from source
   run 1's harness on `bird_evolve` with (i) the full pooled SIM and (ii) top-4 retrieved entries
   (BM25 + embeddings, RRF) under a 1,200-token budget; 2 seeds each. Descriptive.
5. **Repeat-evaluation variance**: the seed harness and one evolved harness evaluated 3 times on
   `bird_heldout` and `spider_heldout`.

Budget caps (enforced in the gateway): ≤ 80,000 policy calls, ≤ 3,000 search calls in total.

## Analysis rules fixed in advance

- Scores are 0-100 points; differences are absolute points.
- No run is excluded after the fact. Runs stopped by a budget cap are reported as stopped.
- The paired study's endpoint is the final held-out score, not the best round.
- Everything else (curves, costs, SIM statistics) is descriptive.

---

## Deviations

1. **2026-11-19, before any result was used: search model settings and proposal format.** The first
   attempt of `sql-src-1` produced 0/3 usable proposals in round 0: `qwen4-max` returned only
   `reasoning_content` (thinking mode on by default) and hit the 8,192-token output cap with empty
   content (one diagnostic call: 198 s, 8,192 completion tokens). Fix: the search roles (proposer,
   selector) run `qwen4-max` with `enable_thinking=false`, and the proposer returns a JSON manifest plus
   verbatim `<file>` blocks instead of file contents escaped inside JSON (the JSON parser remains as a
   fallback). That aborted attempt (3 proposer calls, no evaluations of candidates) was deleted and
   `sql-src-1` restarted from scratch. No other setting changed.
2. **2026-11-19, after the first 1-2 rounds of 8 source runs, before any target run: acceptance rules
   and call cap.** The seed harness's repeated evaluations at temperature 0 differ by up to 4 of 60
   tasks (spider_evolve: 78.33 / 85.00 / 78.33), so the calibrated band was δ = 12.5 points. All 21
   evaluated candidates fell inside that band, where the shaped rule (w_c = 15) rejected every one of
   them because multi-call harnesses raised tokens by 59-517%: 0 of 21 admissible, so source runs
   could not produce any edit (and therefore any SIM). Changes, applied to every run and condition:
   δ = max(1 · sd_null, 1/n) (z = 1 instead of 2); above-band cost rule β1 = 40 (RRSI's published
   default) instead of 10; within-band shaped rule w_c = 5 instead of 15; text-to-SQL policy calls per
   task capped at 2 instead of 4 (keeps the policy budget within the 80,000-call cap). The screen now
   receives the full diff (it had rejected candidates whose diff the loop had truncated at 12,000
   characters). The 8 partial runs (runs/main/aborted/, 85 search calls and ~1,900 policy calls in the
   ledger) were discarded and all source runs restarted from scratch. This decision used only
   acceptance statistics of source rounds, no target or held-out result.
3. **2026-11-19, after a provider outage, before any run of the revised plan: budget revision.**
   At 08:34 UTC the provider account ran out of credit (`AccessDenied.Unpurchased`, HTTP 403) while
   sql-src-1..6 were finishing and the first paired target runs had started. 403s were then scored
   as failed tasks; every evaluation that saw one is set aside (`runs/main/aborted/outage-20261119`,
   `results/validity.json`) and the evaluator now discards provider failures instead of scoring them.
   The remaining money is about RMB 12, so the plan is scaled down. Changes, applied to every run:
   - **Money cap.** Every call is priced in RMB from the returned usage by the gateway's spend ledger
     (`results/budget/spend_ledger.json`), hard stop at RMB 9.50 cumulative (worst-case reservations
     before each call). The call caps above stay as a second line.
   - **One model for all roles:** `qwen3.8-flash` (thinking off) is policy, proposer and selector.
     `qwen4-max` has no known price and would fail closed in the spend ledger.
   - **Horizon:** N = 3 rounds, k = 2 candidates per round (edit budget annealed 3 → 1 over 3 rounds).
     All other method settings (deviation 2 included) unchanged.
   - **Sub-splits** (`data/splits/subsplits.json`, sha256
     `c90fdbb1ebc563a999b1fa5834c75efd95e6d8ad14574952c4c9d8ebc9b1ae75`, seed 20261119, uniform samples
     of the frozen splits): `spider_evolve_s30`, `spider_heldout_s40`, `bird_evolve_s30`,
     `bird_heldout_s40`, `so_evolve_s20`, `so_heldout_s20`; the support-ops target splits
     `ood_evolve` (15) and `ood_heldout` (15) are used whole. A sub-split's evaluation is taken from a
     cached evaluation of its parent split when one exists (same harness, model and rollout).
   - **Experiments, in priority order** (the scheduler launches a run only while the money left covers
     the conservative cost estimate of it and of the runs in flight; anything not launched is
     reported as not run):
     1. text-to-SQL source RSI: 3 runs (seeds 1-3) on `spider_evolve_s30`, held-out `spider_heldout_s40`;
     2. paired study (RQ1) with **n = 3 pairs** (was 10, minimum 5): Full SIM vs Harness Transfer on
        `bird_evolve_s30`, endpoint `bird_heldout_s40` EX after round 3; same statistics, which with
        n = 3 are descriptive only (the exact signed-rank test cannot reach p < 0.05 with 3 pairs);
     3. text-to-SQL Cold Start and Problem-Only (four conditions with the pair-1 arms);
     4. support-ops: 1 source run on `so_evolve_s20` (held-out `so_heldout_s20`), then the four
        conditions on `ood_evolve` / `ood_heldout` in the order Full SIM, Harness Transfer, Cold Start,
        Problem-Only;
     5. retrieval ablation with one seed per arm (pooled SIM of the 3 source runs).
   - **Cross-model (RQ2c) is not run** (no budget for a second model family). The four `xm-*` runs stay
     in the plan and are exercised end to end with fake models in the offline tests; the
     pre-revision `deepseek-v4.1-flash` repeat evaluations (in the ledger) are reported as they are.
   - **Pre-revision runs:** `sql-src-4` (qwen4-max search, full splits, finished before the outage) is
     reported separately as a pre-revision run; its numbers are not pooled with the revised runs.
   The revised runs live in a fresh experiment root (`runs/rev3`); only the cached evaluations of
   unchanged harnesses (none tainted) are copied over from `runs/main`.
4. **2026-11-19 03:38, during the revised run, before any paired statistic was computed: evaluation race.**
   The evaluation cache was locked per process only. sql-full-i and sql-transfer-i run in separate
   processes and start from the same harness, so both evaluated it at the same time (calibration on
   `bird_evolve_s30`, held-out start on `bird_heldout_s40`) and the later write replaced the earlier
   cache file. The arms therefore did not share one measurement of their common start, as the design
   requires (e.g. held-out start of pair 1: 60.0 in the Full SIM arm, 55.0 in the transfer arm; pair 2:
   67.5 vs 62.5; calibration scores of pairs 2 and 3 differ between arms). Fix: a cross-process file
   lock per cache key (commit "Evaluator: cross-process file lock ..."). Decision, taken after seeing
   the per-arm event logs of pairs 1-2 but before computing any paired difference or statistic: the six
   paired arms are re-run from scratch with the fix (same names, same seeds, same sources); the first
   execution is set aside in `runs/rev3/aborted/race/` and reported in REPORT.md as a secondary,
   flawed execution, never pooled. Cached evaluations stay: each cache file is one complete evaluation
   of its harness, and with the lock every arm now reads the same one. sql-cold, sql-problems-1 and the
   source runs did not race (no concurrent process shared their keys) and are kept. All support-ops
   condition runs started after the fix.
5. **2026-11-19 ~04:10, after the 3-pair result had been seen, before any data of new pairs existed:
   extension of the paired study.** The user asked to keep running with the remaining money
   (RMB 4.116 left under the unchanged RMB 9.50 cap). Adding pairs after seeing a result is optional
   stopping, so the extension is a **separate, secondary analysis**: the 3-pair analysis of deviation 3
   (mean Δ −1.67, p = 0.74) stays the primary result and is never replaced. Number of extra pairs, fixed
   now: per-pair cost estimated from the spend ledger as the most expensive observed pair (source RMB
   0.144 + both arms of its first execution RMB 0.708 = RMB 0.852; the first execution paid the arms'
   shared calibration twice, so this over-estimates a new pair); (4.116 − 0.50 headroom) / 0.852 → **4
   pairs, sources 4-7**, launched in order behind the scheduler's money gate with RMB 0.50 reserved and
   each source claiming its two arms. The extended analysis is pairs 1-7 (or all pairs that complete;
   a pair is used only if both arms finish). Protocol, configuration, splits, seeds (source i, arms
   100 + i) and code are those of pairs 1-3; the only code changes are the gate's headroom/bundling and
   the report's second table. The retrieval ablation keeps its pool of sources 1-3. No extra source
   seed beyond the pairs (estimated money after the pairs, RMB 0.71, is under headroom + RMB 1).

---

# Study 2: strong search model (separate follow-up study)

Written and committed **2026-11-19, before any Study 2 run**: no Study 2 call has been made and no Study 2
data exist (the code was exercised only offline, with a simulated upstream). Everything above, deviations 1-5
included, belongs to the first study and is unchanged; so are its results. Study 2 has its own experiment
root (`runs/study2`), its own spend ledger and cap, and its own result files (`results/study2/`). Deviations
from this section will be appended below it, dated, as for the first study.

## What had been seen when this was written

- Revised plan (deviation 3), 7 Spider source runs `sql-src-1..7` with `qwen3.8-flash` as policy, proposer
  and selector: held-out (`spider_heldout_s40`) 80.0 → 77.5, 77.5, 77.5, 82.5, 80.0, 80.0, 85.0, i.e. changes
  −2.5, −2.5, −2.5, +2.5, 0, 0, +5.0 (mean 0.0, sd 2.89); evolve changes +3.3 to +10.0.
- The one pre-revision source run with `qwen4-max` as proposer and selector (`sql-src-4` of `runs/main`:
  full splits 60 / 120 tasks, N = 4, k = 3) went 81.7 → 86.7 on held-out. One run, other splits and horizon.
- Hypothesis: the weak search model was the bottleneck.

## Question

With `qwen4-max` as proposer and selector and everything else identical to `sql-src-1..7`, does the Spider
held-out score of a source run improve?

## Design (fixed)

- **Runs:** `s2-src-1` … `s2-src-5`, seeds **11, 12, 13, 14, 15** (the first study used 1-7 and reserved
  1-10). Source RSI from the seed harness with an empty SIM that the run builds (memory `full`), as the controls.
- **Identical to `sql-src-i`:** domain text2sql; splits `spider_evolve_s30` / `spider_heldout_s40`
  (`data/splits/subsplits.json`, sha256 `c90fdbb1ebc563a999b1fa5834c75efd95e6d8ad14574952c4c9d8ebc9b1ae75`);
  N = 3 rounds, k = 2 candidates; the deviation-2 rules (δ = max(1 · sd_null, 1/n), β1 = 40, w_c = 5); at
  most 2 policy calls per task; per-run call caps (policy 9,000, proposer 40, selector 30, critic 10); one
  rollout, rollout index 0; same engine, evaluator and sandbox code. `tests/test_study2.py` checks that each
  configuration differs from `sql-src-1`'s only in run id, seed and search model.
- **Policy:** `qwen3.8-flash` (DashScope), `enable_thinking=false`, temperature 0, seed 1234 (+ rollout),
  max 2,048 output tokens: the controls' settings.
- **Proposer and selector:** `qwen4-max` (DashScope, same key mechanism). Thinking **off**
  (`enable_thinking=false`), as in the pre-revision `qwen4-max` run after deviation 1 and as the controls'
  search roles. Proposer temperature 0.8, selector 0, **max_tokens 8,192** (the engine default used by both the
  pre-revision run and the controls; 6 of 141 pre-revision `qwen4-max` proposer calls reached it).
- **Seed-harness evaluations reused:** the cached evaluations of the seed harness (tree `685dca477777`) on
  `spider_evolve_s30` (rollouts 0-2, the calibration) and `spider_heldout_s40` (rollout 0, the held-out start),
  and their parent-split files, are copied from `runs/rev3` by `lamarck seed-cache` (exact cache key: domain,
  split, policy model, harness tree, rollout; never overwriting). So every Study 2 run shares the controls'
  starting measurements (evolve 83.3, held-out 80.0) and noise band. Evolved candidates are evaluated anew.
- **Launch:** `scripts/run_study2.sh` (API check of both models, seed cache, gateway, scheduler with the money
  gate, report). At most 3 runs at a time, launched in the order `s2-src-1..5`.

## Endpoints and analysis (fixed)

- **Primary outcome:** per run, held-out execution accuracy on `spider_heldout_s40` (40 tasks) of the harness
  retained after round 3 minus that of the seed harness, in points.
  1. vs 0: two-sided one-sample t-test with a 95% t interval, and the exact two-sided Wilcoxon signed-rank test
     (zeros dropped, average ranks). With n = 5 the smallest attainable signed-rank p is 0.0625.
  2. vs the 7 flash-proposer controls (their changes listed above, read from `results/summary.json`, key
     `source`): two-sided Welch t-test with a 95% interval for the difference of means, and the exact two-sided
     Mann-Whitney test (every split of the pooled values enumerated, average ranks for ties).
  α = 0.05, no multiplicity correction. Reading rule: "held-out improved" only if the mean change is positive
  and test 1's t-test gives p < 0.05; "better than the flash proposer" only if the mean difference is positive
  and the Welch test gives p < 0.05. The rank tests are reported next to them as robustness checks.
- **Secondary (descriptive):** evolve change (`spider_evolve_s30`), accepted edits per run, proposals and
  proposal failures, RMB per run and in total, calls and tokens per role.
- **Limitations, stated before any data:** the controls ran earlier (2026-11-19) and were not randomized
  against or interleaved with Study 2's runs, so the comparison with them is not a randomized comparison (time,
  provider state or model versions may differ); n is small (at most 5 vs 7); scores move in steps of 2.5
  points on 40 tasks, and repeat evaluations of one harness at temperature 0 differ by about as much
  (`results/summary.json`, `eval_variance`), so only a large effect (a mean change of roughly 4-5 points or
  more, at the controls' sd) can reach significance; one model family, one benchmark, pilot-scale horizon.
- **Rules:** no run is excluded after the fact. A run that stops (budget cap, failure) is reported with its
  status; it has no endpoint and nothing is imputed. Provider failures pause a run (evaluations that saw one are
  discarded) and it resumes, as in the first study. **n is the number of runs that finish**; the analysis uses
  those runs, whatever their number (no test with fewer than 2).

## Money (fixed)

- **Cap:** RMB **9.00** for everything Study 2 calls (API checks included), in a separate ledger
  `results/study2/budget/spend_ledger.json`, enforced by the gateway with worst-case reservations before each
  call. The first study's ledger (`results/budget/`, RMB 9.50 cap, RMB 8.181 spent) is not opened.
- **Prices (RMB per 1M tokens):** `qwen3.8-flash` 0.80 in / 2.70 out / 0.10 cached input (as before);
  `qwen4-max` 10 in / 30 out, Alibaba Cloud Model Studio, China (Beijing),
  help.aliyun.com/zh/model-studio/model-pricing, retrieved 2026-11-19; output includes thinking tokens; no
  cached-input price was confirmed, so cached input is billed at the full input price; the free quota for new
  models is ignored. Unknown models are refused (fail closed).
- **Estimate** (`python -m lamarck.experiments.study2`, from `results/pre_revision/summary.json` and
  `results/budget/spend_ledger.json`): `qwen4-max` per successful pre-revision call: proposer 4,932 prompt /
  3,698 output tokens, selector 9,972 / 444; per control run 6-8 proposer and 3-5 selector calls (mean 6.9 and
  4.0), policy RMB 0.069-0.103, doubled here because the pre-revision `qwen4-max` harness roughly doubled policy
  tokens per task (477 → 936). Per run: **low RMB 1.29** (prompt sizes the flash search roles saw on these
  sub-splits), **central RMB 1.72**, **high RMB 2.05** (maximum calls and policy cost); five runs RMB 6.5 /
  8.6 / 10.3.
- **Money gate (priority if money runs short):** runs launch in the order `s2-src-1..5`. A run is launched only
  if cap − spent − in-flight reservations − Σ over running runs (RMB 2.10 − their spend so far) ≥ **RMB 2.10**,
  the per-run bound (the high estimate rounded up). A run the gate never admits is reported as not run. After
  any run ends in `budget_stop` no further run is launched. The bound is an estimate-based bound, not a hard
  per-run cap; the hard guarantee is the RMB 9.00 cap. (A hard per-run cap was rejected: the gateway's worst-case
  reservation, about RMB 0.4-0.6 per in-flight `qwen4-max` call because every character counts as a token and
  8,192 output tokens are assumed, would stop runs well below their real cost.) Consequence: the fifth run
  starts only if the first four spent at most RMB 6.90 (RMB 1.725 per run on average), so n = 5 is expected
  under the low and central estimates and n = 4 under the high one.

## Deviations (Study 2)

1. **2026-11-20 (UTC), after the n = 5 result had been seen, before any data of new runs existed: extension
   to 10 runs.** The user asked for five more Study 2 runs, `s2-src-6` … `s2-src-10` with seeds **16-20**.
   They are added *after* seeing the n = 5 result (`results/study2/RESULTS.md`, commit `26c6984`: held-out
   changes 0, 0, +5, +5, +2.5, mean +2.5; vs 0 t(4) = 2.24, p = 0.089; vs the controls Welch p = 0.14). This is
   **optional stopping**, so:
   - **The n = 5 analysis above (runs `s2-src-1..5`) stays the primary Study 2 result and is never replaced**,
     whatever the new runs show. `study2_report` keeps computing it from exactly runs 1-5 (a test checks this).
   - The **n = 10 analysis** (runs `s2-src-1..10`, all that finish) is a **separate, clearly labelled secondary
     analysis**, with the same endpoint and tests as the primary one (held-out change vs 0; vs the **same 7
     controls** `sql-src-1..7`). Its p-values are not corrected for the optional stopping and are not a
     confirmatory test: they cannot turn the primary reading into "held-out improved" or "better than the
     flash proposer".
   - **n of the extension is however many of the new runs finish under the money gate** (rules unchanged:
     launch order `s2-src-6..10`, at most 3 at a time, per-run bound RMB 2.10, no further launch after a
     `budget_stop`; a run the gate never admits is reported as not run; no run is excluded after the fact).
   - **Money:** the Study 2 cap rises from **RMB 9.00 to RMB 15.00**, in the same ledger file
     (`results/study2/budget/spend_ledger.json`, RMB 5.624 spent before the extension). Runs 1-5 cost RMB
     0.994-1.339 each (mean 1.124), so five more runs are expected to cost about RMB 5.6; RMB 9.38 are left
     under the new cap. The first study's ledger is still never opened.
   - **Host platform (a possible confound):** runs 1-5 ran on an AWS EC2 host (Ubuntu, Linux arm64, host Docker
     with gVisor `runsc`); runs 6-10 run on a macOS arm64 machine (Docker in a colima VM with gVisor `runsc`).
     The sandbox image is built from the same Dockerfile; protocol, configuration, splits, seed-harness cached
     evaluations, models and request settings are the same (the code changes are only the plan length, the
     cap and the report's second analysis). Policy calls go to the same provider at temperature 0, so the
     host should not change the scores, but the two batches also differ in time (a few hours apart) and provider
     state, and host and batch are confounded: a difference between runs 1-5 and 6-10 cannot be attributed to
     either.

---

# Study 3: confirmatory replication of Study 2 (fixed n)

Written and committed **2026-11-20 (UTC), before any Study 3 run**: no Study 3 call has been made, no Study 3
run directory (`runs/study3`) and no Study 3 result exist. Everything above (the first study, Study 2 and its
deviation 1) is unchanged, and so are their results. Study 3 has its own experiment root (`runs/study3`), its
own spend ledger and cap, and its own result files (`results/study3/`). Deviations from this section will be
appended below it, dated.

## What had been seen when this was written

- Study 2 (`results/study2/RESULTS.md`, `results/study2/summary.json`), `qwen4-max` proposer and selector:
  - primary, pre-registered n = 5 (`s2-src-1..5`, EC2 Linux): held-out changes 0, 0, +5, +5, +2.5, mean +2.5,
    t(4) = 2.24, two-sided p = 0.089, 95% CI [−0.60, +5.60]; vs the 7 flash controls Welch p = 0.142.
  - extended n = 10 (runs 6-10 added after seeing the n = 5 result, optional stopping, macOS/colima): changes
    of runs 6-10 +7.5, 0, +2.5, 0, 0; all ten: mean +2.25, t(9) = 2.59, two-sided p = 0.029, 95% CI
    [+0.28, +4.22], exact signed-rank p = 0.062; vs the controls Welch p = 0.132, Mann-Whitney p = 0.121.
    Not confirmatory.
  - cost per run RMB 0.778-1.663 (mean 1.117), `results/study2/budget/spend_ledger.json`.
- The 7 flash controls `sql-src-1..7` (`results/summary.json`, key `source`): held-out changes −2.5, −2.5,
  −2.5, +2.5, 0, 0, +5.0 (mean 0.0).
- Because the n = 10 p-value came from optional stopping, Study 3 is a fresh, fixed-n test of the same
  hypothesis with new seeds, pre-registered before any of its runs. Its direction is taken from Study 2.

## Hypothesis

With `qwen4-max` as proposer and selector and everything else identical to Study 2 (and so to
`sql-src-1..7` apart from the search model), the Spider held-out change of a source run after RSI is greater
than 0.

## Design (fixed)

- **Runs:** exactly 10 new runs, `s3-src-1` … `s3-src-10`, seeds **21, 22, …, 30** (Study 1 used 1-7,
  Study 2 11-20). Source RSI from the seed harness with an empty SIM that the run builds (memory `full`).
- **Identical to Study 2's runs except run id and seed:** domain text2sql; splits `spider_evolve_s30` /
  `spider_heldout_s40` (`data/splits/subsplits.json`, sha256
  `c90fdbb1ebc563a999b1fa5834c75efd95e6d8ad14574952c4c9d8ebc9b1ae75`); N = 3 rounds, k = 2 candidates; the
  deviation-2 acceptance rules; at most 2 policy calls per task; per-run call caps (policy 9,000, proposer
  40, selector 30, critic 10); one rollout, rollout index 0; same engine, evaluator and sandbox code. Policy
  `qwen3.8-flash` (thinking off, temperature 0, seed 1234 + rollout, max 2,048 output tokens); proposer and
  selector `qwen4-max` (thinking off, temperature 0.8 / 0, max_tokens 8,192). `lamarck/experiments/study3.py`
  builds the plan from Study 2's plan builder; `tests/test_study3.py` checks that each Study 3 configuration
  equals the corresponding Study 2 configuration except `run_id` and `seed`.
- **Seed-harness evaluations reused:** as in Study 2, `lamarck seed-cache --from runs/rev3` copies the cached
  evaluations of the seed harness (tree `685dca477777`) on `spider_evolve_s30` (rollouts 0-2, the
  calibration), `spider_heldout_s40` (rollout 0, the held-out start) and their parent-split files (exact
  cache key: domain, split, policy model, harness tree, rollout; never overwriting). Every Study 3 run
  therefore starts from the same measurements as Study 2 and the controls (evolve 83.3, held-out 80.0).
  Evolved candidates are evaluated anew.
- **Host:** all Study 3 runs on one macOS arm64 machine, Docker in a colima VM with gVisor `runsc`.
- **Launch:** `scripts/run_study3.sh` (API check of both models, seed cache, gateway, scheduler with the
  money gate, report). At most 3 runs at a time, launched in the order `s3-src-1..10`.

## Fixed n

- **No further runs are added after seeing any Study 3 result, whatever it is.** There is no extension.
- If money stops runs (the gate never admits a run, or a run ends in `budget_stop`), **n is the number of runs
  that finished**, and that is reported together with the runs that did not; nothing is imputed and the
  missing runs are not replaced by other seeds.
- **No re-runs of failed runs**, except after a provider or gateway error, and then only under the existing
  outage rule (first study deviation 3, Study 2 "Rules"): provider failures pause a run, evaluations that saw
  one are discarded, and the same run (same id, same seed) resumes from its last completed round. A run that
  fails for any other reason is reported with its status and has no endpoint.
- No run is excluded after the fact.

## Endpoints and analysis (fixed)

- **Primary outcome:** per run, held-out execution accuracy on `spider_heldout_s40` (40 tasks) of the harness
  retained after round 3 minus that of the seed harness, in points (as in Study 2).
- **Primary test:** one-sample t-test of the held-out changes of the finished runs among `s3-src-1..10`
  (exactly these runs, no other study's runs) against 0, **one-sided (mean > 0), α = 0.05**. No test with
  fewer than 2 finished runs.
- **Also reported for the primary outcome:** the two-sided t-test p, the 95% two-sided t interval, and the
  exact Wilcoxon signed-rank test (zeros dropped, average ranks, every sign assignment enumerated), two-sided
  and one-sided (greater). These do not change the verdict.
- **Interpretation rule (fixed in advance):** one-sided t-test p < 0.05 → **"replicated"**; otherwise
  **"not replicated"**. README and `results/study3/RESULTS.md` use exactly that word for the verdict.
- **Secondary (descriptive, no verdict):** held-out changes vs the 7 flash controls `sql-src-1..7` (two-sided
  Welch t with a 95% interval for the difference of means, exact two-sided Mann-Whitney), evolve-split change
  (`spider_evolve_s30`), accepted edits per run, RMB per run and in total.
- **Limitations, stated before any data:**
  - the controls are not randomized: they ran earlier (2026-11-19), on another date and not interleaved with
    Study 3, so the two-sample comparison is not a randomized comparison (time, provider state, model
    versions);
  - held-out scores move in steps of 2.5 points on 40 tasks;
  - evaluation noise: repeat evaluations of one harness at temperature 0 differ by about as much as the
    effect (`results/summary.json`, `eval_variance`); every run shares one cached measurement of the seed
    harness's held-out start (80.0), so the noise of that one measurement is common to all runs;
  - host: all Study 3 runs on macOS/colima (Study 2's runs 1-5 ran on EC2 Linux);
  - power: at Study 2's n = 10 mean (+2.25) and sd (2.75) the one-sided test with n = 10 has power about
    0.77; at half that effect about 0.33. Study 2's estimate may be inflated by optional stopping, so a
    "not replicated" verdict does not show that the effect is 0.
  - one model family, one benchmark, pilot-scale horizon (3 rounds).

## Money (fixed)

- **Cap:** RMB **15.00** for everything Study 3 calls (API check included), in a separate ledger
  `results/study3/budget/spend_ledger.json`, enforced by the gateway with worst-case reservations before each
  call. The first study's and Study 2's ledgers are not written.
- **Prices:** as in Study 2 (`qwen3.8-flash` 0.80 / 2.70 / 0.10 cached; `qwen4-max` 10 / 30, cached input at
  the full price; unknown models refused).
- **Estimate** (`python -m lamarck.experiments.study3`, from Study 2's ledger): RMB 1.117 per run (Study 2
  mean, range 0.778-1.663), about **RMB 11.2** for ten runs.
- **Money gate:** as in Study 2: runs launch in the order `s3-src-1..10`; a run is launched only if
  cap − spent − in-flight reservations − Σ over running runs (RMB 2.10 − their spend so far) ≥ **RMB 2.10**,
  the per-run bound (Study 2's bound, above Study 2's most expensive run, RMB 1.663). A run the gate never
  admits is reported as not run; after any run ends in `budget_stop` no further run is launched. The hard
  guarantee is the RMB 15.00 cap. Consequence: the tenth run starts only if the runs before it leave room
  for its bound (at most RMB 12.90 spent and bound by running runs); at Study 2's mean cost n = 10 is
  expected.
