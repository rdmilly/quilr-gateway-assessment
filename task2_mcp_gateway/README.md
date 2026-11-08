# Task 2: MCP security gateway

An HTTP reverse proxy between an AI agent and a downstream MCP server. It reads the caller's bearer token to find their role, forwards `tools/list` (and everything else) unchanged, and refuses `tools/call` on any tool whose name starts with `admin_` unless the caller is an admin, answering `-32001 Unauthorized Tool Call` without contacting the server.

```
agent ──HTTP──▶ gateway :8080 /mcp ──HTTP──▶ mock MCP server :9000 /mcp
                 │ who are you (bearer token → role)
                 │ parse the JSON-RPC message once, strictly
                 │ admin_ tool and not admin? → -32001, stop here
                 └ forward the parsed message, stream the reply back
```

## Run it

```bash
uv sync
cp config.example.toml config.toml
uv run python -m mocks.mcp_server        # terminal 1: the downstream server on 127.0.0.1:9000
uv run python -m task2_mcp_gateway       # terminal 2: the gateway on 127.0.0.1:8080
```

Then, from a third terminal. The two tokens are the fake test values documented in `config.example.toml`; keeping them in variables keeps real tokens out of shell history and docs too.

```bash
VIEWER_TOKEN=test-viewer-token
ADMIN_TOKEN=test-admin-token
H='-H content-type:application/json -H accept:application/json,text/event-stream'
CALL='{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"key_id":"k1"}}}'

# viewer: refused by the gateway, the server never sees it
curl -s localhost:8080/mcp $H -H "authorization: Bearer $VIEWER_TOKEN" -d "$CALL"

# admin: forwarded, the server runs the tool
curl -s localhost:8080/mcp $H -H "authorization: Bearer $ADMIN_TOKEN" -d "$CALL"

# no token: 401
curl -si localhost:8080/mcp $H -d "$CALL" | head -1
```

The viewer gets:

```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"Unauthorized Tool Call","data":{"tool":"admin_reset_key","required_role":"admin"}}}
```

Any MCP client works too: point it at `http://127.0.0.1:8080/mcp` with an `Authorization: Bearer ...` header. The tests drive the official Python SDK client through the gateway on both protocol versions.

## What happens to each request

Every request takes the same path through `app.py`, in this order. Each check runs before anything more expensive.

| Step | Fails with | Why it's here |
| --- | --- | --- |
| Origin header not on the allow list | HTTP 403 | DNS rebinding protection, required by the MCP spec. Agents send no Origin, so they pass. |
| No valid bearer token | HTTP 401, `WWW-Authenticate: Bearer` | Checked before the body is read, so an anonymous caller costs almost nothing. |
| Not `application/json`, or body over 1 MiB | HTTP 415 / 413 | Bounded work per request. |
| Not exactly one JSON-RPC message | HTTP 400, `-32700` or `-32600` | See "One message, read strictly" below. |
| `Mcp-Method` / `Mcp-Name` headers disagree with the body | HTTP 400, `-32020` | Required by MCP 2026-07-28. |
| `tools/call` on an `admin_` tool, role is not admin | HTTP 200, `-32001` | The brief's rule. The server is never contacted. |
| Downstream unreachable or too slow | HTTP 502 / 504, `-32603` | Fixed message; no upstream details leak. |

Otherwise the parsed message is forwarded and the server's reply, JSON or a server-sent event stream, is streamed back as it arrives.

## Decisions worth knowing

**`-32001` comes back with HTTP 200.** That is how an MCP server answers a request it refuses. The official SDK client reports any non-2xx status as a generic transport error, so with a 403 the client would never see `-32001`. A test drives the SDK client through the gateway and checks the code arrives.

**`-32001` is in a range the current spec discourages.** MCP 2026-07-28 says new implementations SHOULD NOT use `-32000` to `-32019`. The brief names `-32001` exactly, so the gateway uses it. In production I would use a code outside the JSON-RPC reserved range.

**Missing token is HTTP 401 with code `-31001`.** The 401 is what clients act on. For the body, MCP asks for new application error codes outside the reserved range (`-32768` to `-32000`), so `-31001` sits just outside it, and it is not confused with the authorization error `-32001`.

**The admin check runs on a canonical name.** NFKC normalization, invisible (format) characters removed, trimmed, case-folded. So `Admin_reset_key`, a full-width `a`, a soft hyphen or a zero-width space cannot slip past. `administrator_report` and `my_admin_tool` are not blocked: the rule is "starts with `admin_`".

**One message, read strictly, forwarded as parsed.** The gateway decides on what it parsed and forwards that, re-serialized, never the original bytes. Anything two JSON parsers might read differently is refused, so the gateway and the server cannot disagree about which tool is being called:

- duplicate keys (`{"name": "lookup_order", "name": "admin_reset_key"}`),
- batches,
- `NaN`, `Infinity` and out-of-range numbers,
- lone surrogates,
- invalid UTF-8,
- a `null` id.

**`tools/list` is not filtered.** The brief says to forward it transparently, so a viewer sees `admin_reset_key` listed but cannot call it. Hiding admin tools from viewers would be a reasonable production choice; it is not done here because the brief asks for transparency.

**No token passthrough.** The caller's `Authorization` (and cookies) never reach the server, which did not issue that token. The gateway sends `X-Gateway-Subject` and `X-Gateway-Role` instead. The server should trust those only on a network where nothing but the gateway can reach it.

**One fixed downstream.** The target URL comes from config, never from the request, so a caller cannot point the gateway at another host.

**Denied notifications are dropped.** A `tools/call` sent as a notification (no id) gets HTTP 202 like any notification, but is not forwarded.

**Every tool call decision is audited.** One OCSF 1.9.0 API Activity record per allowed or denied `tools/call`, with subject, role, tool and latency, hash-chained so edits are detectable. The records hold decisions, never arguments.

## Tests

`tests/task2/`:

- `test_rules.py`: the parser, the policy and the header check on their own, including property-based tests.
- `test_gateway.py`: the gateway over HTTP against the real mock server, in memory, for both JSON and SSE responses.
- `test_sdk_client.py`: the official MCP client through the gateway on 2025-11-25 and 2026-07-28.
- `test_processes.py`: the gateway and the mock as real processes on real ports, configured from a file.

```bash
uv run pytest tests/task2
```

## Known limits

- Tokens are a static, hashed list in config. Production would verify OAuth or JWT tokens from an identity provider; the `Authenticator` is the one place that would change.
- The role comes from the token alone. Per-tool policies (read-only, needs approval, redact arguments) would extend `policy.py`.
- Only `POST` is served. The 2025-11-25 `GET` event stream and session `DELETE` return 405; the mock server is stateless and does not need them.
- Numbers are forwarded as Python reads them, so `1.10` is sent as `1.1`. The value is the same; the text is not.
