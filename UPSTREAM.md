# What could be offered back upstream

Notes for the RRSI authors (Xia et al., arXiv 2609.24972, github.com/google-research/rrsi) and the SIM
authors (Yu et al., arXiv 2611.18856). Nobody has been contacted; this is a list of things that came out
of building lamarck that might be useful to them, with pointers to the code here.

## To the SIM authors

1. **A runnable reference for the D.3 record and the D.1 merge rule.** `lamarck/sim/memory.py` implements
   the entry/attempt schema field for field, the update U(M_t, e_t), identical-text merging with
   `aliases`, `carried_from`, and the Problem-Only projection. `tests/test_sim.py` has property tests
   (Hypothesis) for the invariants a SIM should keep: one attempt appended per update, merges lossless,
   deterministic and idempotent, aliases always resolving to a live entry, lossless JSON round trip.
   The paper does not define `backfilled`; we used "reconstructed after the fact rather than written live"
   and would welcome the intended meaning.
2. **Sealed predictions.** The paper says predictions are recorded before evaluation. lamarck enforces it
   structurally: the proposal manifest (root cause, predicted tasks) is written to disk before the
   candidate's evaluation starts, and a test checks the manifest timestamp against the evaluation file.
3. **Statistics check.** `lamarck/experiments/stats.py` reproduces Table 3's statistics from the per-run
   values (mean +2.36, t(9) = 4.06, p = 0.003, CI [1.05, 3.68], exact signed-rank p = 0.006, d_z = 1.28;
   `tests/test_stats.py`). One small note: App. C.4 says the signed-rank check "enumerates the 210 sign
   assignments"; with ten non-zero differences there are 2^10 = 1,024 assignments, and enumerating all
   1,024 gives the reported p = 0.006.
4. **A small-model, small-budget data point.** Our CRSI results with one cheap model (`qwen3.8-flash`) in
   every role, N = 3, k = 2 and 15-40-task splits (PREREG.md deviation 3: the whole revised study had to fit
   in RMB 9.50) are in results/RESULTS.md and REPORT.md, including what did not replicate. Read them as a
   pilot-scale, budget-limited attempt with a different model family, not as a replication.
5. **Retrieval over SIM.** The paper's proposer reads the complete SIM. We implemented top-k retrieval
   (BM25 + embeddings, reciprocal-rank fusion, token budget) over a memory pooled from several source runs;
   see REPORT.md for whether the budget allowed running it.

## To the RRSI authors

1. **Noise band at temperature 0.** Repeated evaluations of an unchanged harness at temperature 0 still
   differed by up to 4/60 tasks on our provider, so the calibrated 2σ band (12.5 points) pushed every gain
   into the within-band shaped rule, which then rejected all multi-call candidates (0/21 admissible; see
   PREREG.md deviation 2). A short note in the docs on choosing z, w_c and the number of trials k when the
   evaluation set is small might save others the same detour.
2. **Critic sees truncated diffs.** In our first rounds the LLM screen rejected candidates because the
   diff it received had been truncated by the loop (we had copied a 12,000-character cap). RRSI's critic
   passes `diff[:120_000]`; marking truncation explicitly in the payload avoids the reviewer treating the
   cut as a defect.
3. **Sandboxed evaluation.** lamarck runs each candidate evaluation in a fresh gVisor container whose only
   network peer is an LLM gateway that holds the key and enforces budgets; escape probes are in
   `lamarck/sandbox/probes.py`. Under gVisor, Docker's embedded DNS is not reachable, so the gateway name is
   injected with `--add-host`; this may be useful if RRSI's coding domain ever moves to runsc.
4. **A minimal Domain adapter shape.** lamarck's `Domain` (tasks with public/private fields, data mounts per
   split, host-side scoring, guards, critic patterns) is a slightly narrower version of RRSI's interface that
   makes "gold never enters the sandbox" a property of the type rather than of each adapter.
