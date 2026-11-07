#!/usr/bin/env bash
# The gate: every change has to pass this before it is committed.
# CI runs the same script, plus a secret scan and a dependency audit.
set -euo pipefail
cd "$(dirname "$0")"

step() { printf '\n== %s\n' "$1"; }

step "lint";   uv run ruff check .
step "format"; uv run ruff format --check .
step "types";  uv run mypy
step "tests";  uv run pytest -q

printf '\nGATE PASSED\n'
