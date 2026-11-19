#!/usr/bin/env bash
# Reproduce the budget-revised experiments (PREREG.md deviation 3) on one machine.
#
# Needs: colima + Docker, gVisor installed in the colima VM (see PROGRESS_LOG.md), the data under
# data/ (scripts/prepare_*), DASHSCOPE_API_KEY in the key file named in lamarck/cli.py.
# Every call goes through the gateway, which stops at the RMB cap in lamarck/cli.py (SPEND_CAP_RMB).
# Safe to re-run: finished runs are skipped, interrupted runs resume from their last round.
set -euo pipefail
cd "$(dirname "$0")/.."
L="uv run python -m lamarck.cli --root runs/rev3"

colima status >/dev/null 2>&1 || colima start
# colima regenerates /etc/docker/daemon.json on start, which drops the runsc runtime
if ! docker info 2>/dev/null | grep -q runsc; then
  colima ssh -- sudo runsc install
  colima ssh -- sudo systemctl restart docker
fi

# the API check owns the spend ledger while it runs: no gateway container may be up at the same time
docker rm -f lamarck-gateway >/dev/null 2>&1 || true
uv run python scripts/api_check.py          # one call; stop here on 403 / billing errors
$L gateway-up
$L schedule --parallel 4 | tee -a runs/rev3/schedule.out
make report

# leave nothing running
docker rm -f lamarck-gateway >/dev/null 2>&1 || true
colima stop
