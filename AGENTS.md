# Working in this repo

Rules for anyone changing this code, person or coding agent.

## The rule that comes first

Every behavior the assignment describes is built exactly as the brief says. Anything stricter or
extra stays off by default and is listed in the README's deviations table with the reason.

## Layout

- `shared/`: errors, logging, configuration and auth used by every task. Imports nothing from a task.
- `taskN_*/`: one folder per task, each with its own README, code and `tests/`. Tasks never import
  each other (enforced by import-linter once the task folders exist).
- `mocks/`: stand-ins for tests and demos, never product code.
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

`./check.sh` runs lint, format, type checks and tests. Nothing is committed unless it passes.
CI runs the same gate on Python 3.12 to 3.15, plus a full-history secret scan and a dependency audit.
