# Working in this repo

Rules for anyone changing this code, person or coding agent.

## The rule that comes first

Every behavior the assignment describes is built exactly as the brief says. Anything stricter or
extra stays off by default and is listed in the README's deviations table with the reason.

## Layout

- `shared/`: errors, logging, configuration and auth used by every task. Imports nothing from a task.
- `taskN_*/`: one folder per task, each with its own README. Tasks never import each other
  (enforced by import-linter).
- `tests/taskN/`: that task's tests. `tests/shared/` covers `shared/`.
- `mocks/`: stand-ins for tests and demos, never product code (enforced by import-linter).
- `gateway/main.py`: wiring only, no logic.

## Code

- One idea per file, named for what it does.
- A short comment on why at every decision point. The code should read without this file.
- Never write anything but MCP messages to stdout in Task 1. Logs go to stderr.
- No secrets in code, config or tests. Test tokens are obviously fake.
- Errors leaving the system never carry stack traces, file paths or upstream bodies.

## Tests

- Name each test after what it proves, with the requirement or attack ID it covers:
  `test_T2c_viewer_admin_call_never_reaches_downstream`.
- Timing behavior is tested with a fake clock. Benchmarks report numbers and never fail the build.

## The gate

`./check.sh` must pass before anything is committed. It checks, in order: the lock file matches
`pyproject.toml`; lint, including security rules (Bandit's set, blocking calls in async code,
catch-all excepts); formatting; strict type checks; tests with a 90% branch-coverage floor; the CI
workflows for security problems (zizmor); and the working tree for secrets (Betterleaks).

CI runs the same gate on Python 3.12 to 3.14 and adds: a full-history secret scan, a dependency
audit against two advisory databases (OSV via `uv audit`, PyPI via pip-audit), a malware check
on every install, workflow linting, and CodeQL code scanning. Every action is pinned to a commit,
and dependency resolution ignores versions published in the last week.
