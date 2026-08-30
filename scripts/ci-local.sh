#!/usr/bin/env bash
# Run the CI lint and test jobs locally with the same commands as
# .github/workflows/ci.yml. The image build is not run here.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> sync"
uv sync --frozen --all-packages

echo "==> lint: uv lock --check"
uv lock --check
echo "==> lint: ruff check"
uv run ruff check .
echo "==> lint: ruff format --check"
uv run ruff format --check .

for member in shared channels gateway; do
  echo "==> test: $member"
  uv run --package "joshua-$member" pytest "$member/tests" \
    --cov="joshua_$member" --cov-report=term --cov-report=xml --cov-fail-under=80
done

# core's store layer only runs against Postgres, so the integration suite counts
# toward coverage. Run the unit suite, append the integration suite, then gate on
# the combined total. Needs DATABASE_URL set to a live Postgres.
echo "==> test: core (unit + integration, combined coverage)"
uv run --package joshua-core pytest core/tests --cov=joshua_core --cov-report= --cov-fail-under=0
rc=0
uv run --package joshua-core pytest core/tests -m integration \
  --cov=joshua_core --cov-append --cov-report= --cov-fail-under=0 || rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 5 ]; then exit "$rc"; fi
uv run --package joshua-core coverage report --show-missing --fail-under=80
uv run --package joshua-core coverage xml -o coverage.xml

echo "==> test-policy"
python3 scripts/check_test_policy.py

echo "==> ci-local: all stages passed"
