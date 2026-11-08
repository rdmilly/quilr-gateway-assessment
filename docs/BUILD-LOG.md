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

CI now also runs the gate on macOS and Windows (Python 3.14); all 226 tests, including the real-process stdio tests, pass on both on the first run.

## 3. Task 2: MCP gateway (`task2_mcp_gateway/`, `mocks/mcp_server.py`)

Bearer token to role, strict single JSON-RPC parse forwarded as parsed, `admin_` tools refused with `-32001` for non-admins without contacting the server, routing headers checked against the body, Origin check, size limits, no token passthrough, one fixed downstream, OCSF audit per tool call decision. The mock downstream is a real SDK server over Streamable HTTP. Tested with raw HTTP, with the official SDK client on both protocol versions over SSE and JSON, and as real processes on real ports.

Found while building:

- The SDK client turns any non-2xx reply into a generic error, so `-32001` must travel with HTTP 200 for clients to see it.
- The SDK's HTTP server checks the Host header (DNS rebinding protection); the mock allows localhost names explicitly.
- The SDK app's lifespan must start and stop in one async task; the test fixture holds it in a background task.
- Escapes such as `\u200b` typed into remote commands arrived as the literal invisible characters. All test files were scanned and invisible or look-alike characters restored to escapes.

`import-linter` now enforces that tasks never import each other, the shared kit imports no task, and product code never imports the mocks.

## 4. Task 2 hardening and two extras

Checked against the published 2026-07-28 spec, the gateway now enforces the routing headers, answers refused notifications with 403 instead of 202, sends errors that answer no single request without an id, and limits tool names to the MCP character set (closing look-alike letters). `-32001` comes back exactly as the brief names it. Extras: an optional idempotency key on `trigger_refund`, and an optional JWT mode with audience binding.

## 5. Task 3: streaming PII guardrail (`task3_pii_guardrail/`, `mocks/llm_provider.py`)

A streaming redactor with bounded holdback, an incremental SSE reader, per-choice and per-tool-call redaction, and an OpenAI-compatible endpoint. Found while building:

- Partial matching alone reported `a@mail.example` as complete while `.org` was still coming; property tests found it, and an anchored "could it still grow" check fixed it.
- A card-shaped window inside a long tracking number was redacted; digits continuing past a separator now count as one longer number.
- The in-memory test transport collects whole responses, hiding streaming; time to first token is measured with real processes instead.
- Mock controls sent as headers never reached the mock, because the gateway rightly forwards only what the provider needs; they moved into the body.
