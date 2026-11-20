#!/usr/bin/env bash
# Study 2 (PREREG.md, "Study 2: strong search model"): 10 Spider source runs (s2-src-1..5 pre-registered, 6..10
# the extension of Study 2 deviation 1). See scripts/run_study.sh.
#
#   scripts/run_study2.sh                    real run (cap RMB 15.00, own ledger results/study2/budget/)
#   scripts/run_study2.sh --dry-run [DIR]    the same steps offline (simulated upstream, no key, no network)
exec "$(dirname "$0")/run_study.sh" study2 "$@"
