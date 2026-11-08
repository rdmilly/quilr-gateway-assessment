# Task 1: customer-service MCP server

An MCP server on the official Python SDK (`mcp` 2.3.0), served over stdio, with two tools:

| Tool | Arguments | Returns |
| --- | --- | --- |
| `get_customer_record` | `customer_id`: `CUST-` and five digits | The customer's profile and refundable balance |
| `trigger_refund` | `customer_id`, `amount` (positive, at most 2 decimals), `reason` (10 to 500 characters) | A refund receipt |

## Run it

```bash
uv sync
uv run python -m task1_mcp_server        # or: uv run task1-mcp-server
```

Try it by hand (the 2026-07-28 protocol carries its version in `_meta` on every request):

```bash
M='"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{"name":"cli","version":"1"}}'
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"CUST-00042"},'"$M"'}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"CUST-42"},'"$M"'}}' \
  | uv run python -m task1_mcp_server 2>/dev/null
```

Request 1 returns the record. Request 2 returns `-32602 Invalid params` naming the `customer_id` field. Logs are on stderr; drop the `2>/dev/null` to see them.

Seed customers: `CUST-00042` (balance 250.00), `CUST-10001` (120.00), `CUST-12345` (75.50).

## How errors map

| Situation | Response | Why |
| --- | --- | --- |
| Argument fails validation | JSON-RPC error `-32602 Invalid params`, one entry per bad field in `error.data` | The brief asks for standard JSON-RPC codes |
| Unknown tool name | JSON-RPC error `-32602` | What the MCP spec specifies |
| No such customer, refund larger than the balance | Normal result with `isError: true` | A business outcome the agent should read and act on |
| Line is not JSON | JSON-RPC error `-32700 Parse error`, `id: null` | JSON-RPC 2.0 |
| JSON but not a JSON-RPC message (including batches) | JSON-RPC error `-32600 Invalid Request` | JSON-RPC 2.0; MCP does not allow batches |
| Bug in a tool | JSON-RPC error `-32603 Internal error`, fixed message | Details go to the stderr log, never to the caller |

Error responses name the fields that failed, never the values sent, so a secret pasted into the wrong field is not echoed back or logged.

**One deliberate difference from the current spec.** MCP 2026-07-28 (SEP-1303) says input validation failures should come back as tool results with `isError: true`, so a model can read the message and retry. The brief asks for JSON-RPC error codes, so this server sends `-32602`. Switching would change one place, `tools.call_tool`.

## Validation rules

All in `schemas.py`, one strict Pydantic model per tool. The same models produce the JSON Schema shown in `tools/list`, so the advertised schema and the enforced rules cannot drift apart.

- **Strict types, no coercion.** `"10.00"` is not a number and `true` is not a number.
- **Unknown fields are rejected.** A caller cannot slip in something like `approved_by`.
- **`customer_id`** must be exactly `CUST-` plus five ASCII digits. It stays a string, so `CUST-00042` keeps its zeros. Rejected: a trailing newline, full-width or Arabic-Indic digits, and wrong case.
- **`amount`** must be finite and greater than 0, with at most two decimal places. Money is handled as `Decimal` internally, so three refunds of 0.10 leave exactly 0.30 less.
- **`reason`** must be 10 to 500 characters, with at least 10 left after trimming spaces. Control characters, zero-width characters, bidi overrides, private-use characters and lone surrogates are rejected, so ten invisible characters cannot pass a ten-character rule.

## Keeping stdout clean

Stdout carries JSON-RPC and nothing else. Four layers make sure of it:

1. Logging is configured to stderr, as one JSON object per line.
2. While the server runs, `sys.stdout` points at stderr. Python buffers `print()`, and a buffer flushed at exit would otherwise reach the real stdout.
3. The SDK points file descriptor 1 at stderr while serving. That catches writes that skip Python entirely.
4. Tests run the real server as a subprocess, including a tool that calls `print()` and writes to descriptor 1, and parse every byte of stdout as JSON-RPC.

## Two fixes on top of the SDK

`transport.py` sits between the SDK's stdio transport and the server. Both gaps it closes were found by testing the real process:

- **Responses lost at end of input.** When stdin closed, the SDK stopped right away and dropped requests still in flight. So `printf ... | server` often printed nothing. The server now answers everything it has received, then exits, with a 10-second limit so a stuck request cannot keep it alive.
- **Unreadable lines got no reply.** The SDK dropped lines it could not parse, and the client waited forever. They now get `-32700` or `-32600`, as in the table above.

## Both protocol versions

The server speaks MCP 2026-07-28 (stateless, `_meta` on each request) and 2025-11-25 (`initialize` handshake), which is what most clients still use. Every client-facing test runs on both.

## Tests

`tests/task1/`: the argument models (including property-based tests), the dispatcher, the in-memory SDK client on both protocols, the transport layer, and real-process stdio tests.

```bash
uv run pytest tests/task1
```

## Known limits

- An `id` that is an object or an array (not allowed by JSON-RPC) is read by the SDK as a notification, so it gets no reply.
- Duplicate keys in a request resolve to the last value, which is standard JSON parser behavior. The Task 2 gateway handles this, because a gateway and a server reading the same message differently is a real attack.
- Customer data lives in memory and resets on restart.
