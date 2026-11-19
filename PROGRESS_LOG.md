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
