#!/usr/bin/env bash
# Study 3 (PREREG.md, "Study 3"): fixed-n replication of Study 2, 10 Spider source runs s3-src-1..10 (seeds
# 21-30) with qwen4-max as proposer and selector; no run is added after results. See scripts/run_study.sh.
#
#   scripts/run_study3.sh                    real run (cap RMB 15.00, own ledger results/study3/budget/)
#   scripts/run_study3.sh --dry-run [DIR]    the same steps offline (simulated upstream, no key, no network)
exec "$(dirname "$0")/run_study.sh" study3 "$@"
