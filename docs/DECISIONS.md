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

### D16. Optional idempotency key on trigger_refund

- **Chose:** An optional idempotency_key; a retry with the same key and arguments returns the original receipt, a reused key with different arguments is refused. Kept in memory, newest 10,000 keys.
- **Instead of:** Leaving duplicate refunds to the client, or making the key required.
- **Why:** Agents retry after timeouts, and a duplicate refund is real money (OWASP ASI02, tool misuse). Optional keeps the brief's three inputs and behavior unchanged.

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

### T2-D13. Enforce the 2026-07-28 routing headers

- **Chose:** For 2026-07-28 requests, MCP-Protocol-Version, Mcp-Method and (for tools/call, prompts/get, resources/read) Mcp-Name must be present, plain visible ASCII or the base64 form, and equal to the body; otherwise 400 with -32020. Older versions: only headers that are present are checked.
- **Instead of:** Checking only headers that happen to be present.
- **Why:** The published Streamable HTTP spec lists a missing required header as a validation failure and says intermediaries MUST answer it with an HTTP error.

### T2-D14. Tool names limited to the MCP character set

- **Chose:** Refuse names outside ASCII letters, digits, _ - . / (SEP-986) with -32602, then a case-insensitive admin_ prefix check.
- **Instead of:** Unicode normalization (NFKC) before the prefix check.
- **Why:** NFKC does not turn a Cyrillic a into a Latin one, so look-alikes survived. One character-set rule closes them all and is easier to explain. Ignoring case over-blocks a tool named like Admin_report; accepted as defense in depth.

### T2-D15. Optional JWT mode with audience binding

- **Chose:** Off by default. When on, accept HS256 JWTs whose issuer and audience match config exactly, with exp, iat and sub required and the role taken from a claim; algorithm pinned; secret from an environment variable, at least 32 bytes.
- **Instead of:** Static tokens only; or trusting a role claim without checking the audience.
- **Why:** "Extract the user's role" can also mean reading it from the token. The audience check is how a resource server refuses tokens issued for other services (MCP authorization, RFC 8707). Production would use the identity provider's public keys rather than a shared secret.

### T2-D7. Missing token is HTTP 401 with code -31001

- **Chose:** 401 with `WWW-Authenticate: Bearer`, body code -31001, checked before the body is read.
- **Instead of:** Reusing -32001, or a code inside the JSON-RPC reserved range.
- **Why:** Authentication and authorization are different failures. MCP asks for new application codes outside -32768 to -32000; the 401 is what clients act on.

### T2-D8. A denied tool call is HTTP 200 with exactly the brief's error

- **Chose:** 200 and `{"code": -32001, "message": "Unauthorized Tool Call"}` with the request id, no data field.
- **Instead of:** HTTP 403, or extra detail in the error.
- **Why:** The brief names the error but not an HTTP status. Found by test: the official SDK client turns any non-2xx response into a generic transport error, so with 403 the client never sees -32001. Leaving out data means a test comparing the whole error object passes; the tool and role are in the log and audit record. MCP's OAuth section uses 403 for missing scopes, which this role check is not.

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

### T2-D12. Denied notifications are refused, not forwarded

- **Chose:** A tools/call notification for an admin tool gets 403 and a JSON-RPC error with no id.
- **Instead of:** Forwarding notifications unchecked, or answering 202 and dropping it.
- **Why:** Otherwise a caller could trigger an admin tool by leaving out the id. MCP says a notification the server cannot accept gets an HTTP error status, never 202.

## Task 3

### T3-D1. Holdback buffer with partial matching

- **Chose:** The regex module's partial matching, plus an anchored check for complete matches that could still grow; release everything that cannot be part of a match, hold at most 256 characters.
- **Instead of:** Buffering the whole response; fixed-size sliding windows.
- **Why:** Keeps time to first token low and memory flat. 256 covers the longest valid email (254). Property tests prove streaming output equals whole-text redaction for any split.

### T3-D2. What counts as PII

- **Chose:** Practical email pattern; SSNs in 3-2-4 form with one separator, including numbers the SSA never issues; cards of 13 to 19 digits that pass Luhn or are written 4-4-4-4; Unicode digits and letters via \d and \w.
- **Instead of:** SSA validity rules and Luhn-only cards (the original plan); NFKC shadow matching; ML detection.
- **Why:** A guardrail should not leak made-up test data such as 999-99-9999 or 1234 5678 9012 3456. Unicode-aware classes catch full-width digits without mapping offsets between a normalized copy and the original. Nine bare digits and Luhn-failing runs pass, to keep order and tracking numbers readable.

### T3-D3. Non-streamed answers are redacted too

- **Chose:** Redact message content and tool call arguments in the full JSON answer.
- **Instead of:** Streaming only.
- **Why:** Otherwise "stream": false would be a way around the guardrail.

### T3-D4. Redact per choice and per tool call

- **Chose:** One redactor per choice and one per tool call's arguments; held text released into the finishing chunk.
- **Instead of:** One redactor for the whole stream.
- **Why:** Choices and tool calls interleave; mixing them would join text that never belonged together and miss PII split within one of them.

### T3-D5. Provider errors never read

- **Chose:** Close a non-200 provider response unread; return a fixed error envelope, keeping Retry-After on 429.
- **Instead of:** Passing the provider's error through, or reading it to rewrite it.
- **Why:** The brief's Task 4 requires no leaked upstream details, and the mock 500 carries a stack trace, a file path and an internal IP to prove it.

## Task 4

### D25. SQLite with explicit transactions

- **Chose:** `sqlite3.connect(..., autocommit=True)` plus an explicit `BEGIN IMMEDIATE`, one connection per operation, WAL mode.
- **Instead of:** Python's default transaction handling, or one shared connection.
- **Why:** With the default, a manual BEGIN fails. `BEGIN IMMEDIATE` takes the write lock before the first read, so check-and-reserve is atomic whatever order the statements come in. One connection per call because the endpoint calls the limiter from a thread pool and sqlite3 connections must not cross threads; SQLite's own lock does the serializing, which also covers several processes on one file.
- **Proof:** 50 threads racing for 50,000 tokens at 2,000 each: exactly 25 approved, every run. A mutation run without the transaction approved up to 44 (88,000 tokens).

### T4-D1. Reserve then settle

- **Chose:** Reserve an upper-bound estimate (UTF-8 bytes / 4 of messages and tools, plus `max_tokens` or a default, times `n`) inside the transaction; settle to the provider's `usage.total_tokens` afterwards; refund when no provider answered.
- **Instead of:** Counting only after the response.
- **Why:** Counting after lets 50 parallel requests all pass a nearly full budget. Bytes rather than characters so non-English text is not undercounted. For streams the router asks the provider for usage and strips it again if the client did not ask.

### T4-D2. Fail closed

- **Chose:** 503 and no provider call if the store is missing, corrupt, tampered with or locked past the busy timeout.
- **Instead of:** Fail open.
- **Why:** A broken limiter must not become unlimited spend, or a way to switch the limit off.

### T4-D3. Failover on 429 and timeout only, by default

- **Chose:** Fail over on the two triggers the brief names. 5xx and connection errors return 502 unless `failover_on_errors = true`.
- **Instead of:** Failing over on every error.
- **Why:** The brief specifies 429 and timeout. A 5xx often means the request itself is bad, and a second paid provider would fail it too, at double the cost.

### T4-D4. One error envelope

- **Chose:** `{"error": {"type", "code", "message", "request_id"}}` with a fixed status map, fixed messages and `Retry-After`.
- **Instead of:** Passing upstream errors through.
- **Why:** The brief requires sanitized errors. Provider error bodies are never read at all, so nothing in them can leak.

### T4-D5. What "answered within 3000 ms" means

- **Chose:** Plain request: the whole body inside the deadline. Stream: headers and the first chunk. After that a stream stays with its provider, with an idle cut-off (`stream_idle_timeout_s`).
- **Instead of:** Headers only, or failing over mid-stream.
- **Why:** A provider that sends `200 OK` and then nothing would pass a headers-only check and hang. Switching mid-stream would send the client the start of two different answers.

### T4-D6. One clock decides failover

- **Chose:** `anyio.fail_after` around each attempt is the only failover clock; the HTTP client's own timeout is a backstop set above every deadline.
- **Instead of:** Letting the client's read timeout double as the deadline.
- **Why:** Found by the real-process test: the client's read timeout (the stream idle setting, 0.5 s in the test) fired first and failed over at 0.5 s. Two timers on one event is a timeout race; now there is one.

### T4-D7. A request bigger than the window is 400, not 429

- **Chose:** `request_too_large` (400) when the estimate alone exceeds the whole budget.
- **Instead of:** 429.
- **Why:** 429 tells the client to retry, and this request can never fit.

### T4-D8. Exact Retry-After

- **Chose:** Seconds until enough of the tenant's oldest rows expire for this request to fit.
- **Instead of:** Always the window length.
- **Why:** An honest Retry-After stops clients retrying too early (wasted calls) or waiting too long.
