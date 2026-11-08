#!/usr/bin/env bash
# The gate: every change has to pass this before it is committed.
#   ./check.sh        everything, including a secret scan of the working tree (needs Docker)
#   ./check.sh --ci   as CI runs it; CI scans secrets, dependencies and workflows in their own jobs
set -euo pipefail
cd "$(dirname "$0")"

step() { printf '\n== %s\n' "$1"; }

step "lock file matches pyproject"; uv lock --check
step "lint (incl. security rules)"; uv run ruff check .
step "format";                      uv run ruff format --check .
step "types";                       uv run mypy
step "tests + branch coverage";     uv run pytest -q --cov --cov-report=term

if [[ "${1:-}" != "--ci" ]]; then
  step "workflow security (zizmor)"
  uvx -q zizmor==1.31.1 --offline .github/workflows
  step "secrets in the working tree (Betterleaks)"
  if command -v docker >/dev/null 2>&1; then
    docker run --rm -v "$PWD:/repo" ghcr.io/betterleaks/betterleaks:v1.9.0 dir /repo --redact
  else
    echo "skipped: Docker not available (CI scans the full history)"
  fi
fi

printf '\nGATE PASSED\n'
