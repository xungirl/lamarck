# Local equivalent of .github/workflows/ci.yml (offline: fake models, no secrets).
.PHONY: ci lint type test test-docker flaky dashboard report dryrun-study2 dryrun-study3

ci: lint type test

lint:
	uv run ruff check lamarck tests scripts
	uv run ruff format --check lamarck tests scripts

type:
	uv run mypy

test:
	uv run pytest -m "not docker and not live"

test-docker:  ## needs colima/Docker (gVisor optional)
	uv run pytest -m docker

flaky:  ## run the offline suite 10 times; any failure fails the target
	for i in 1 2 3 4 5 6 7 8 9 10; do uv run pytest -q -m "not docker and not live" || exit 1; done

report:
	uv run python -m lamarck.experiments.cli_report

dashboard:
	uv run python -m lamarck.dashboard.build

dryrun-study2:  ## Study 2 launcher end to end, offline (simulated upstream, temp root; needs data/raw)
	scripts/run_study2.sh --dry-run

dryrun-study3:  ## Study 3 launcher end to end, offline (simulated upstream, temp root; needs data/raw)
	scripts/run_study3.sh --dry-run
