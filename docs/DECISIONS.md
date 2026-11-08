# Decisions

Every choice where a reasonable person could have gone another way: what we chose, what we rejected, and why. Decisions for tasks not yet built are marked planned.

## Foundation

### D1. Follow the brief exactly on anything a test could see

- **Chose:** Match every behavior the brief describes; put extra effort into proof (tests, scans), reasoning (decisions, threat model) and presentation.
- **Instead of:** Building our own preferred behavior where the brief and current practice differ.
- **Why:** Graders check what the brief says. Disagreements go in the README with the reason, so the judgment still shows.

### D2. Python and the official MCP SDK (mcp 2.3.0)

- **Chose:** Python throughout, official SDK, latest release.
- **Instead of:** TypeScript.
- **Why:** One language across all four tasks; Pydantic is named in the brief; 2.3.0 is the newest release (checked on PyPI).

### D3. Target MCP 2026-07-28, still accept 2025-11-25

- **Chose:** Serve both protocol versions; every client test runs on both.
- **Instead of:** Only the newest version.
- **Why:** 2026-07-28 is current, but most clients still speak 2025-11-25.

### D17. Python 3.12 to 3.14

- **Chose:** requires-python >=3.12,<3.15.
- **Instead of:** Including 3.15.
- **Why:** pytest and mypy did not run on 3.15 in November 2026 (CI showed it).

### D18. CI on Linux, macOS and Windows

- **Chose:** Gate on all three; Windows fixes capped at 30 minutes for Task 1.
- **Instead of:** Linux only; or adding Windows at the end.
- **Why:** Graders run it on their own laptops. Adding Windows late would land four tasks' worth of platform fixes on deadline day.

### D20. The gate

- **Chose:** uv lock check, ruff with security rules, ruff format, mypy strict, pytest with 90% branch-coverage floor (subprocesses measured), zizmor, Betterleaks; CI adds malware check, uv audit, pip-audit, full-history secret scan and CodeQL.
- **Instead of:** Semgrep and gitleaks from the original plan.
- **Why:** Ruff's S rules cover the Bandit checks, CodeQL covers deeper analysis, and Betterleaks is the maintained successor to gitleaks.

### D21. Repo public early; alert access read-only

- **Chose:** Public now so GitHub's scanners run while we build; token reads alerts but cannot dismiss them.
- **Instead of:** Private until submission; write access.
- **Why:** GitHub's free security scanning needs a public repo. An agent that can dismiss findings could hide them.

### D22. Cards never import each other

- **Chose:** Shared kit plus one folder per task; tasks do not import each other.
- **Instead of:** One big package.
- **Why:** Graders can review each task alone; deleting one lane leaves the others working. Enforced by import-linter once a second task exists.

### D23. Static hashed token map, constant-time compare

- **Chose:** Config holds SHA-256 hashes of tokens with subject and role.
- **Instead of:** JWT as the default; plain tokens in config.
- **Why:** Simplest thing that meets 'read a bearer token, extract the role' without storing secrets. JWT mode is an optional extra.

### D24. OCSF audit events, hash-chained

- **Chose:** OCSF 1.9.0 API Activity (class 6003) per decision.
- **Instead of:** Ad hoc log lines.
- **Why:** OCSF is the open schema SIEM tools ingest, so every allow, deny and redact decision drops straight into a SOC without custom parsing.

## Task 1

### D4. Bad arguments are JSON-RPC -32602, not isError

- **Chose:** -32602 Invalid params with one entry per bad field.
- **Instead of:** isError tool results, which SEP-1303 (Final) and the current spec prefer.
- **Why:** The brief explicitly asks for standard JSON-RPC error codes. README explains the difference; switching is one function.

### D6. Low-level Server instead of the decorator API

- **Chose:** mcp.server.Server with our own dispatcher.
- **Instead of:** MCPServer (decorators).
- **Why:** The decorator API turns validation failures into isError results, the opposite of what the brief asks.

### D7. Validation stricter than the brief's minimum

- **Chose:** Two decimal places, reason 10 to 500 characters counted after trimming, invisible characters rejected, ASCII-only IDs.
- **Instead of:** Only 'positive float' and 'min length 10'.
- **Why:** A refund cannot be a fraction of a cent, unbounded input is a risk, and ten invisible characters should not count as a reason. Graders' likely tests (zero, negative, string, missing) behave exactly per the brief.

### D8. Business failures are isError results

- **Chose:** Unknown customer and refund over balance return a normal result with isError: true.
- **Instead of:** JSON-RPC errors.
- **Why:** The call was valid; the agent should read the outcome and decide. Matches the spec's split between protocol and execution errors.

### D9. Errors never echo the rejected value

- **Chose:** Field names and error types only; generic -32603 on crashes.
- **Instead of:** Pydantic's default messages, which include the input.
- **Why:** A secret pasted into the wrong field should not come back or reach the log.

### D10. Drain in-flight requests when stdin closes

- **Chose:** Hold the server open until every received request is answered, 10 s cap.
- **Instead of:** SDK default (stop immediately) or waiting forever.
- **Why:** Known SDK bug python-sdk #2678; printf | server printed nothing. The cap stops a stuck request keeping the process alive.

### D11. Answer unreadable lines

- **Chose:** -32700 for non-JSON, -32600 for non-messages and batches.
- **Instead of:** SDK default: silent drop.
- **Why:** JSON-RPC 2.0 requires a reply; silence leaves the client waiting.

### D12. Parse errors carry "id": null

- **Chose:** JSON-RPC 2.0 rule: null when the id cannot be read.
- **Instead of:** Omitting id, which MCP's schema suggests.
- **Why:** The two specs disagree. MCP says every message follows JSON-RPC 2.0, and the official Python SDK's error type allows null.

### D13. Requests with a null id get -32600

- **Chose:** Screen raw lines before the SDK and replace such requests with an invalid message.
- **Instead of:** Documenting it as a gap.
- **Why:** MCP: request ids MUST NOT be null. Trade-off: passing our own input stream means the SDK no longer redirects descriptor 0; only matters to tools that spawn child processes reading stdin, and ours spawn none.

### D14. Point sys.stdout at stderr while serving

- **Chose:** Swap inside the stdio context, flush first, restore after.
- **Instead of:** Relying on the SDK's descriptor diversion alone.
- **Why:** Python buffers print(); the buffer flushed at exit after the SDK restored stdout, so text hit the wire. Found by test.

### D15. Money as Decimal

- **Chose:** Validate as float (the brief's type), compute in Decimal.
- **Instead of:** Float arithmetic.
- **Why:** Three refunds of 0.10 must take off exactly 0.30.

### D16. Idempotency key deferred

- **Chose:** Mention as a production addition; build at the end if time allows (30 to 45 min, no debt).
- **Instead of:** Building it now.
- **Why:** Not in the brief, and three tasks remain. It touches only Task 1 files, so later costs the same as now.

## Task 2

### D5. Unauthorized tool call is -32001

- **Chose:** Use -32001 exactly as the brief says.
- **Instead of:** A code outside the legacy range.
- **Why:** The spec says new implementations SHOULD NOT use -32000 to -32019 (verified on the official spec page). The brief names the code, so we follow it and document the conflict; we raised it by email.

### T2-D3. Normalize tool names before the admin_ check

- **Chose:** NFKC, trim, lower case, then prefix check.
- **Instead of:** Raw string startswith.
- **Why:** Stops Admin_, full-width letters and zero-width characters bypassing the rule.

### T2-D4. Strict single parse, re-serialize to forward

- **Chose:** Reject batches and duplicate keys; forward what we parsed.
- **Instead of:** Forwarding raw bytes.
- **Why:** If gateway and server read the same message differently, policy can be bypassed.

### T2-D5. No token passthrough; fixed downstream

- **Chose:** Strip the caller's Authorization; send verified subject and role; one configured downstream URL.
- **Instead of:** Forwarding the caller's headers; routing by request.
- **Why:** MCP security guidance forbids token passthrough; a fixed target removes SSRF.

### T2-D6. Check Mcp-Method and Mcp-Name against the body

- **Chose:** Mismatch is HTTP 400 with -32020 on the 2026-07-28 path.
- **Instead of:** Trusting headers or ignoring them.
- **Why:** Required by the current spec so gateways can route on headers safely.

### T2-D7. Missing token is HTTP 401 with code -31001

- **Chose:** 401 with `WWW-Authenticate: Bearer`, body code -31001, checked before the body is read.
- **Instead of:** Reusing -32001, or a code inside the JSON-RPC reserved range.
- **Why:** Authentication and authorization are different failures. MCP asks for new application codes outside -32768 to -32000; the 401 is what clients act on.

### T2-D8. A denied tool call is HTTP 200 with a JSON-RPC error

- **Chose:** 200 and `{"error":{"code":-32001,...}}`.
- **Instead of:** HTTP 403.
- **Why:** Found by test: the official SDK client turns any non-2xx response into a generic transport error, so with 403 the client never sees -32001. 200 is how an MCP server answers a request it refuses.

### T2-D9. tools/list forwarded unfiltered

- **Chose:** Forward exactly as the brief says; viewers see admin tools but cannot call them.
- **Instead of:** Hiding admin tools from viewers.
- **Why:** The brief asks for transparency. Filtering is a reasonable production option and is noted in the README.

### T2-D10. httpx2 and Starlette, not httpx and FastAPI

- **Chose:** httpx2 (the HTTP client the MCP SDK itself uses) and plain Starlette.
- **Instead of:** httpx plus FastAPI.
- **Why:** One HTTP client library instead of two, and no auto-generated docs endpoints adding surface. The gateway has one route.

### T2-D11. The mock is a real MCP server

- **Chose:** The official SDK's Streamable HTTP server with one ordinary and one admin tool, answering over SSE or JSON.
- **Instead of:** A hand-written fake that returns canned JSON.
- **Why:** Tests then prove the gateway works with real MCP traffic on both protocol versions, including streamed replies.

### T2-D12. Denied notifications are dropped

- **Chose:** A tools/call notification for an admin tool gets 202 like any notification, and is not forwarded.
- **Instead of:** Forwarding notifications unchecked.
- **Why:** Otherwise a caller could trigger an admin tool by leaving out the id.

## Task 3 (planned)

### T3-D1. Holdback buffer with partial matching

- **Chose:** regex module partial matches; hold at most 256 characters.
- **Instead of:** Buffer the whole response; fixed-size windows.
- **Why:** Keeps TTFT low and memory flat. 256 covers the longest valid email (254).

### T3-D2. What counts as PII

- **Chose:** Practical email pattern, SSN with SSA rules, cards 12 to 19 digits with Luhn, NFKC shadow matching.
- **Instead of:** Loose digit runs; ML detection.
- **Why:** Low false positives while catching common evasion. Spelled-out numbers are out of scope and documented.

## Task 4 (planned)

### D25. SQLite with explicit transactions

- **Chose:** sqlite3.connect(..., autocommit=True) plus explicit BEGIN IMMEDIATE.
- **Instead of:** Python's default transaction handling.
- **Why:** With the default, a manual BEGIN fails. BEGIN IMMEDIATE takes the write lock up front so check-and-reserve is atomic.

### T4-D1. Reserve then settle

- **Chose:** Reserve an estimate (chars/4 + max_tokens) inside the transaction, settle to reported usage after.
- **Instead of:** Count only after the response.
- **Why:** Counting after lets 50 parallel requests all pass a nearly full budget.

### T4-D2. Fail closed

- **Chose:** 503 if the limiter store is unavailable.
- **Instead of:** Fail open.
- **Why:** A broken limiter should not become unlimited spend.

### T4-D3. Failover on 429 and timeout only

- **Chose:** Cancel primary, then call backup; charge once.
- **Instead of:** Also on 5xx and connection errors by default.
- **Why:** That is what the brief specifies; the wider set is a flag.

### T4-D4. One error envelope

- **Chose:** {error:{type,code,message,request_id}} with a fixed status map and Retry-After.
- **Instead of:** Passing upstream errors through.
- **Why:** The brief requires sanitized errors; one shape is easy for clients.
