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
