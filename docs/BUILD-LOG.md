# Build log

One entry per shipped component, newest last.

## 1. Foundation (`shared/`, gate, CI)

Errors and the gateway error envelope, JSON logging to stderr with secret redaction, a hash-chained OCSF audit log, typed config loading, and bearer-token auth with constant-time comparison. The gate (`check.sh`) and CI run lint with security rules, strict types, tests with a 90% branch-coverage floor, a dependency audit, a malware check, a workflow scan, a secret scan of the full history, and CodeQL.

## 2. Task 1: MCP server (`task1_mcp_server/`)

Two tools behind strict Pydantic models; validation failures are JSON-RPC `-32602`, business failures are `isError` results. Serves MCP 2026-07-28 and 2025-11-25.

Found by testing the real process, and fixed in `transport.py`:

- The SDK dropped in-flight requests when stdin closed, so `printf ... | server` often printed nothing.
- The SDK gave no reply to unparsable lines or batches. They now get `-32700` or `-32600`.
- An unflushed `print()` reached the real stdout at exit, after the SDK restored it. `sys.stdout` now points at stderr while serving.
- A request with `"id": null` (or an object, array, boolean or fraction) was read as a notification and got no reply. Raw lines are now screened first, and such requests get `-32600`.

CI now also runs the gate on macOS and Windows (Python 3.14).
