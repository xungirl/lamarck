<!-- DRAFT. Not posted. Every number is from results/ in this repository (see REPORT.md). -->

# Series 1: Building an engine that rewrites its own agent harness

## Post 1/3: Candidates are git branches

I spent the last weeks building lamarck, an open-source engine where one LLM edits another agent's
harness (prompts, config, control code) and keeps only the edits that survive evaluation.

The design choice I would make again: every candidate edit is a git branch in its own worktree.
- The tree hash is a free, content-addressed cache key: an unchanged harness is never evaluated twice.
- Accepting an edit is a fast-forward; the lineage is just `git log`.
- When the task changes, I clone the whole repository, so the history travels with the harness.

It is an independent implementation of the Self-Improvement Memory paper (Yu et al., 2611.18856), which
ships no code, on top of the regularizers from RRSI (Xia et al., 2609.24972).

## Post 2/3: Temperature 0 is not deterministic

The regularizers decide whether a gain is real by measuring noise first: evaluate the starting harness
three times and set a noise band from the spread.

At temperature 0 the same harness on the same 60 Spider tasks scored 78.3, 85.0 and 78.3. On 30 BIRD
tasks one harness scored 46.7, 36.7 and 46.7. With the band at 2 sd, every one of 21 candidates fell
inside it and then lost on token cost: zero admissible edits. I documented the change to 1 sd in the
pre-registration before running anything that mattered.

Lesson: measure your evaluator's noise before you trust any "improvement" it reports.

## Post 3/3: The optimizer overfits, even with regularizers

Three independent source runs on Spider all discovered the same edit: execute the drafted SQL and ask
for a repair when it returns no rows. Evolve accuracy went 83.3 → 90.0 / 93.3 / 90.0.

Held-out accuracy went 80.0 → 77.5 in all three runs.

And moved to BIRD, every Spider-evolved harness started below the untouched seed harness (55.0 / 62.5 /
65.0 vs 67.5). Small evolve sets make self-improvement look better than it is. Keep a held-out split the
optimizer never sees, and look at it.
