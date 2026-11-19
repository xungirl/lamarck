<!-- DRAFT. Not posted. Every number is from results/ in this repository (see REPORT.md). -->

# Series 3: Does a self-improving agent learn from its own history? A pilot on RMB 5

## Post 1/3: The question

Self-Improvement Memory (Yu et al., 2611.18856) records every accepted harness edit by root cause, with
the tasks it was predicted to fix written down before evaluation. When the benchmark changes, you carry
that memory along. The paper reports that this beats carrying only the improved harness.

I implemented the full protocol (four starting conditions and a paired study) and pre-registered the
experiment before running it.

## Post 2/3: The result: nothing detectable, and why that is the honest answer

Budget forced a pilot: one cheap model in every role, 3 rounds, 2 candidates, 30-40-task splits, 3 pairs.

Full memory minus harness-only transfer on BIRD held-out: 0.0, −10.0, +5.0 points. Mean −1.67, 95% CI
[−20.6, +17.3]. With 3 pairs an exact signed-rank test cannot go below p = 0.25, and one task is 2.5
points, while the same harness re-evaluated at temperature 0 moved by up to 5 points.

So: no evidence for the effect, and no power to see it. I am reporting it as exactly that.

## Post 3/3: Things that went wrong and how they show up in the report

- A provider outage scored 403s as wrong answers: set aside, documented, evaluator fixed.
- Two parallel experiment arms evaluated their shared starting harness at the same time and each kept a
  different score (60.0 vs 55.0). Cross-process lock, regression test, both arms re-run, and the flawed
  first execution still appears in the report, never pooled.
- The support-ops target split was at ceiling (100 for every condition): uninformative, and said so.

What would make it a real test: ten or more pairs, bigger evolve sets, a stronger proposer. The code is
ready for that; the budget was not.
