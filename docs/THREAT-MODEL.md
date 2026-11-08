# Threat model

Threats tracked per component and trust boundary (B1 to B9). Status: **defended + tested**, **partial** (defended, not fully tested), **planned** (designed, not built), **open** (known gap, accepted or deferred).

| Boundary | Crossing |
| --- | --- |
| B1 | Caller to gateway |
| B2 | Gateway to downstream MCP server |
| B3 | Gateway to primary model |
| B4 | Gateway to backup model |
| B5 | Gateway to SQLite |
| B6 | MCP client host to Task 1 server (skips the gateway) |
| B7 | Gateway to logs |
| B8 | Config to gateway |
| B9 | Build and supply chain |

## Foundation

| Threat | Where | Class | Defense | Proof | Status |
| --- | --- | --- | --- | --- | --- |
| Token guessed by timing the comparison | identity, B1 | ASI03 identity and privilege abuse | Tokens stored as SHA-256 hashes and compared with hmac.compare_digest against every entry, so timing does not reveal which entry matched or how much. | tests/shared/test_auth.py | defended + tested |
| Oversized or malformed Authorization header | identity, B1 | Input validation, DoS | 4096-character cap and a strict Bearer pattern before any lookup. | tests/shared/test_auth.py | defended + tested |
| Secrets end up in logs | logs, B7 | LLM02 sensitive information disclosure | Message and fields are scrubbed for bearer and basic credentials, JWTs, API keys and GitHub tokens; keys like authorization or password are always redacted. | tests/shared/test_logs.py | defended + tested |
| Forged log lines | logs, B7 | Log injection | Every record is one JSON object with ASCII escaping, so a newline in input cannot start a fake line, and extra fields cannot overwrite level, time or logger. | tests/shared/test_logs.py | defended + tested |
| Audit records edited or deleted after the fact | audit | Repudiation | Each record carries the hash of the previous one; verify_chain finds edits, insertions and deletions in the middle. Cutting off the tail is not detectable without an external anchor; documented. | tests/shared/test_audit.py | partial |
| Bad config, or config errors that print secrets | config, B8 | Misconfiguration | Frozen, typed sections with unknown keys rejected; error messages name the field and never include the value. | tests/shared/test_config.py | defended + tested |
| Malicious or vulnerable dependency | build, B9 | ASI04 supply chain, LLM03 | Locked dependencies, a one-week cooldown on new versions, uv malware check, uv audit and pip-audit in CI, Dependabot alerts. | CI supply-chain job; Dependabot shows 0 open alerts | defended + tested |
| Secret committed to the repo | build, B9 | Credential exposure | Betterleaks scans the working tree locally and the full git history in CI; GitHub secret scanning and push protection are on. | CI secrets job scanned about 377 KB with no leaks; GitHub shows 0 open alerts | defended + tested |
| Hijacked or over-privileged CI workflow | build, B9 | CI/CD compromise | Actions pinned to commit SHAs, credentials not persisted after checkout, read-only default permissions, zizmor and CodeQL on the workflows. | CI workflows job; CodeQL 0 results | defended + tested |
| The build agent's GitHub token can do too much | build | LLM06 excessive agency | Alert permissions granted read-only, so the agent can read findings but not dismiss them. The token can still change repo settings; to be tightened after submission. | GitHub API: alerts readable, dismiss not granted | partial |

## Task 1

| Threat | Where | Class | Defense | Proof | Status |
| --- | --- | --- | --- | --- | --- |
| Malformed or extra arguments reach the refund code | task1, B6 | LLM05 improper output handling, ASI02 tool misuse | One strict Pydantic model per tool: no type coercion, unknown fields rejected, ASCII-only CUST-[0-9]{5}, finite positive amount with at most two decimals, reason 10 to 500 visible characters. A failure is -32602 and the tool never runs. | tests/task1/test_schemas.py, test_tools.py::test_bad_arguments_never_reach_the_tool | defended + tested |
| Lookalike input passes the ID check | task1, B6 | Input validation | Pattern runs in Rust regex where $ is the true end and [0-9] is ASCII only, so a trailing newline, full-width or Arabic-Indic digits are rejected. | test_schemas.py::test_malformed_customer_ids_are_rejected | defended + tested |
| Invisible or reordered text hides what a refund reason says | task1 | Prompt injection carrier, reviewer deception | reason rejects control, format (zero-width, bidi override), surrogate, private-use and unassigned characters, and counts length after trimming. | test_schemas.py::test_invalid_reasons | defended + tested |
| Something writes to stdout and corrupts the protocol | task1, B6 | Availability, protocol integrity | Four layers: logging to stderr; sys.stdout pointed at stderr while serving (catches buffered print); SDK diverts descriptor 1 (catches C code and os.write); a real-process test parses every byte of stdout. | test_stdio.py::test_stray_print_cannot_corrupt_stdout, ::test_importing_the_server_writes_nothing_to_stdout | defended + tested |
| Client waits forever: dropped replies | task1, B6 | Availability | transport.py answers garbage with -32700, non-messages and batches with -32600, requests with a null or object id with -32600, and drains in-flight requests when stdin closes (10 s cap). | test_transport.py, test_stdio.py::test_unreadable_lines_get_errors_and_the_server_keeps_going, ::test_request_with_invalid_id_gets_invalid_request, ::test_full_session_over_pipes | defended + tested |
| Error replies echo secrets back | task1 | LLM02 sensitive information disclosure | error.data lists field names and error types only, never the rejected values; capped at 20 errors and 100-character field names. | test_tools.py::test_error_data_names_fields_but_never_echoes_values, ::test_flood_of_unknown_fields_gets_a_short_answer | defended + tested |
| A crash leaks internals | task1 | Information disclosure | Any unexpected exception becomes -32603 Internal error with a fixed message; the detail goes to the stderr log only. | test_tools.py::test_unexpected_failure_is_a_generic_internal_error | defended + tested |
| Two refunds spend the same balance | task1 | Race condition | Balance check and update happen under one lock; money is Decimal so repeated small refunds are exact. | test_tools.py::test_concurrent_refunds_cannot_overspend (50 threads, exactly 12 of 10.00 succeed from 120.00), ::test_refund_reduces_the_balance_exactly | defended + tested |
| An agent retries trigger_refund and pays twice | task1 | ASI02 tool misuse, unsafe tool execution | Optional idempotency key: a retry with the same key returns the original receipt; a reused key with different arguments is refused; checked under the balance lock. Tool also annotated destructive. Keys are in memory (restart forgets them). | test_tools.py::test_retry_with_the_same_key_refunds_once, ::test_concurrent_retries_with_one_key_pay_once, ::test_same_key_for_a_different_refund_is_refused | defended + tested |
| Anyone who can start the server can call it | task1, B6 | Authentication | By design. Over stdio only the parent process can talk to the server, and the spec says stdio servers take credentials from the environment, not the HTTP auth flow. Authentication lives in the Task 2 gateway. | MCP 2026-07-28, Auth section | open |

## Task 2

| Threat | Where | Class | Defense | Proof | Status |
| --- | --- | --- | --- | --- | --- |
| Admin tool reached by name tricks | policy, B1 | ASI03 privilege abuse | Names outside the MCP character set (SEP-986) are refused with -32602, so look-alike letters, full-width letters and invisible characters never reach the admin check; the admin_ check ignores case. | test_gateway.py::test_names_outside_the_mcp_character_set_are_refused, ::test_admin_tool_check_ignores_case; test_rules.py::test_tool_name_character_set | defended + tested |
| Gateway and server read the same message differently | parser, B1 | Parser differential | Reject duplicate keys and batches, then forward the message re-serialized from what the gateway parsed, never the raw bytes. | test_gateway.py::test_unreadable_bodies_are_refused_before_policy (duplicate keys, batches, NaN), test_rules.py::test_invalid_messages | defended + tested |
| Admin call smuggled as a notification | policy | Authorization bypass | Authorize tools/call notifications too, and drop them if denied. | test_gateway.py::test_denied_tool_call_notification_is_refused_not_forwarded | defended + tested |
| Caller's token passed through to the downstream server | proxy, B2 | MCP token passthrough, confused deputy | Strip the caller's Authorization header; send the verified subject and role in gateway headers instead. | test_gateway.py::test_caller_token_is_not_passed_downstream | defended + tested |
| Caller steers the gateway to another host | proxy, B2 | SSRF | One fixed downstream URL from config; nothing in the request can change it. | Downstream URL only from config (settings.py); test_gateway.py::test_downstream_url_must_be_http_with_a_host | defended + tested |
| Headers say one tool, body says another | http, B1 | Request smuggling | On 2026-07-28 requests the routing headers must be present, well-formed and equal to the body, otherwise HTTP 400 and -32020. | test_gateway.py::test_header_that_disagrees_with_body_is_refused, ::test_method_header_that_disagrees_is_refused | defended + tested |
| Huge or slow request ties up the gateway | http, B1 | LLM10 unbounded consumption | Body size limit (Content-Length checked first, then counted while reading) and an upstream timeout. | test_gateway.py::test_oversized_body_is_413, ::test_chunked_body_over_the_limit_is_cut_off, ::test_downstream_failure_is_a_clean_error | defended + tested |
| Caller without a valid token reaches the server | http, identity, B1 | Authentication | Bearer token checked before the body is read; 401 otherwise. | test_gateway.py::test_missing_or_bad_token_is_401_and_nothing_is_forwarded | defended + tested |
| Token issued for another service, or forged, accepted (JWT mode) | identity, B1 | MCP token passthrough, ASI03 | Optional JWT mode accepts only HS256 tokens whose issuer and audience match config, unexpired, with required claims; algorithm pinned; secret from the environment. | tests/shared/test_auth_jwt.py; test_gateway.py::test_gateway_accepts_jwts_issued_for_it_and_refuses_others | defended + tested |
| Browser page drives a local gateway (DNS rebinding) | http, B1 | Cross-origin abuse | Requests carrying an Origin not on the allow list get 403; Origin is not forwarded. | test_gateway.py::test_unknown_origin_is_403_and_allowed_origin_passes | defended + tested |
| Upstream failure leaks internals | proxy, B2 | Information disclosure | Fixed messages with 502 or 504 and the caller's id; exception text never returned. | test_gateway.py::test_downstream_failure_is_a_clean_error | defended + tested |
| Tool call decisions cannot be reviewed later | audit | Repudiation | One hash-chained OCSF record per allowed or denied tools/call, with subject, role and tool, never arguments. | test_gateway.py::test_every_tool_call_decision_is_audited | defended + tested |
| Viewer learns admin tool names from tools/list | proxy | Information disclosure | Accepted: the brief asks for transparent forwarding. Filtering is a production option (DECISIONS.md T2-D9). | test_gateway.py::test_tools_list_is_forwarded_transparently | open |
| Gateway identity headers forged by someone who can reach the server directly | B2 | Spoofing | The server must only be reachable from the gateway (private network or mTLS). Not enforceable from the gateway; documented. | README, Decisions worth knowing | open |

## Task 3 (planned)

| Threat | Where | Class | Defense | Proof | Status |
| --- | --- | --- | --- | --- | --- |
| PII split across chunks slips through | pii, B3 | LLM02 sensitive information disclosure | Holdback buffer with partial regex matching: release text as soon as it cannot be the start of a match, hold the rest (cap 256 characters). | planned | planned |
| Obfuscated PII (full-width digits, spaced card numbers) | pii | Evasion | Match on an NFKC-normalized shadow copy; cards checked with Luhn; spelled-out numbers documented as out of scope. | planned | planned |
| Buffering kills time to first token | pii, B3 | Latency | Only hold back what could still become a match; benchmark TTFT overhead. | planned | planned |

## Task 4 (planned)

| Threat | Where | Class | Defense | Proof | Status |
| --- | --- | --- | --- | --- | --- |
| Concurrent requests overshoot the token budget | limit, sqlite, B5 | LLM10 unbounded consumption | Check and reserve inside one BEGIN IMMEDIATE transaction, then settle to the real usage. | planned: 50 concurrent requests test | planned |
| Limiter store broken or tampered | limit, sqlite, B5 | Fail-open | Fail closed with 503 if the store cannot be read or written. | planned | planned |
| Failover charges twice or answers twice | router, B3, B4 | Race condition | Cancel the primary before calling the backup; charge the tenant once. | planned | planned |
| Upstream error leaks stack traces or internals | router | Information disclosure | One error envelope {error:{type,code,message,request_id}} with fixed messages and Retry-After. | planned | planned |
