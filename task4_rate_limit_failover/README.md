# Task 4: rate limiting and model failover

An LLM gateway endpoint, `POST /v1/chat/completions` (OpenAI-compatible), that gives each tenant a token budget per sliding window (50,000 tokens per minute by default), sends the request to a primary model provider, and fails over to a backup when the primary answers `429` or has not answered within 3000 ms. Budgets live in an on-disk SQLite database.

```
client ──▶ router :8082 ──▶ primary provider (mock :9100)
             │   │      └─▶ backup provider  (mock :9101)  on 429 or 3000 ms
             │   └─ one error shape, nothing from a provider's error body
             └─ SQLite: reserve estimate ▶ settle to real usage ▶ or refund
```

## Run it

```bash
uv sync
cp config.example.toml config.toml
uv run python -m mocks.llm_provider                       # terminal 1: primary on :9100
MOCK_LLM_PORT=9101 MOCK_LLM_NAME=backup MOCK_LLM_BEHAVIOR=mock-echo \
  uv run python -m mocks.llm_provider                     # terminal 2: backup on :9101
uv run python -m task4_rate_limit_failover                # terminal 3: the router on :8082
```

The primary obeys the `model` field (`mock-429`, `mock-stall`, `mock-500`, anything else echoes). The backup always answers, so each primary failure can be seen failing over. From a fourth terminal, with the fake test token from `config.example.toml` in a variable:

```bash
TOKEN=test-viewer-token
ask() { curl -s -D - localhost:8082/v1/chat/completions -H "authorization: Bearer $TOKEN" \
  -H content-type:application/json -d "{\"model\": \"$1\", \"messages\": [{\"role\": \"user\", \"content\": \"hello\"}]}"; }

ask mock-echo    # 200, x-gateway-provider: primary
ask mock-429     # 200, x-gateway-provider: backup, at once
ask mock-stall   # 200, x-gateway-provider: backup, after 3 seconds
ask mock-500     # 502 {"error": {...}}: no failover on 5xx unless failover_on_errors = true
```

To see the budget refuse a request, set `tokens_per_window = 1500` and restart the router. Each request reserves its prompt plus `default_max_tokens` (about 1,034 tokens here) until the provider reports what it really used (4), so the way to fill the budget is two requests at once:

```bash
ask mock-stall & sleep 0.5; ask mock-echo; wait
# the second gets 429 rate_limit_exceeded with Retry-After while the first holds its
# reservation; the first then fails over, settles to 4 tokens, and the next request passes
```

## The rate limiter

`limiter.py`. One row per request: tenant, time, tokens. A tenant's usage is the sum of its rows newer than `now - 60 s`, so the window slides continuously. There is no reset at the top of the minute and no double burst across a boundary.

**Why it can't be raced.** The check and the charge are one `BEGIN IMMEDIATE` transaction: drop expired rows, sum the tenant's live rows, insert the new row only if it fits. `BEGIN IMMEDIATE` takes SQLite's write lock before the first read, so two requests can never both read "45,000 used" and both decide they fit. That holds across threads and across processes sharing the file. The test races 50 threads (each with its own connection) for a 50,000 budget at 2,000 tokens each: exactly 25 are approved, every run. With the transaction removed in a mutation run, the same race approved up to 44 (88,000 tokens).

**Reserve, then settle.** The provider's real usage is only known afterwards, but the decision has to be made before. So the router reserves an upper bound first and corrects it:

| Step | Charged |
| --- | --- |
| Request arrives | prompt bytes / 4 + `max_tokens` (or the default) per choice |
| Provider answers | corrected to the provider's `usage.total_tokens` |
| No provider answered | refunded to zero |
| Provider answered but reported no usage | the estimate stands |

Bytes rather than characters keep the guess on the safe side for non-English text. For streams the router asks the provider for usage (`stream_options.include_usage`) and, if the client didn't ask for it, removes the usage chunk again, so the client sees the stream it asked for.

**Eviction.** Every reservation deletes every tenant's rows older than the window, in the same transaction, so the table only ever holds about one window of traffic. A refused request still commits its eviction.

**`Retry-After` is exact**, not "60". It is the time until enough of the tenant's oldest rows expire for this request to fit.

**A request bigger than the whole budget is a `400`**, not a `429`: retrying could never help, and a 429 tells the client to retry.

**Fail closed.** If the database is missing, corrupt, tampered with or locked past the 5 s busy timeout, the router answers `503` and calls no provider. A rate limiter that fails open is one an attacker can switch off by breaking it.

## Failover

`router.py`. The brief's rule is 429 or 3000 ms. Three timing decisions sit inside that:

- **What "answered" means.** For a plain request, the whole body must arrive within 3000 ms. For a stream, the headers *and the first chunk*: a provider that sends `200 OK` and then nothing would otherwise pass the check and hang.
- **No switching mid-stream.** Once a streamed answer has started reaching the client it stays with that provider, because switching would send the start of two different answers. A stream that goes silent afterwards is cut after `stream_idle_timeout_s`.
- **One clock decides.** The HTTP client's own read timeout is set above every deadline. Testing against real processes found it had been shorter, so it fired first and failed over at 0.5 s instead of 3 s.

Measured over real sockets (`tests/task4/test_processes.py`): a stalled primary hands over at 3.05 to 3.08 s; a 429 hands over at once.

Race conditions, and how each is closed:

| Race | What could go wrong | What happens |
| --- | --- | --- |
| Timeout vs. response | Both providers answer the client, or the primary's connection is left open | `anyio.fail_after` cancels whatever step the primary was on and the half-open response is closed before the backup is called (tested: "primary closed" is logged before "backup called") |
| Client disconnects mid-attempt | Leaked connection, tokens charged for nothing | The response is closed on every exit path, cancellation included, and the reservation is refunded |
| Failover and billing | Charged once per provider tried | The router never touches the limiter; the endpoint reserves once and settles once |

**Which failures fail over.** By default only the two the brief names: 429 and timeouts. A 5xx or a refused connection returns `502` at once. `failover_on_errors = true` adds them. Off by default because a 5xx often means the request itself is bad, and sending it to a second paid provider doubles the cost of a request that will fail there too.

## Errors

Every error, from this router or a provider, is one shape with a fixed message:

```json
{"error": {"type": "rate_limit_exceeded", "code": 429, "message": "Token budget exceeded. Retry later.", "request_id": "req_..."}}
```

| Status | `type` | When |
| --- | --- | --- |
| 400 | `invalid_request` | Not JSON, no messages, bad `max_tokens` or `n`, body too large |
| 400 | `request_too_large` | Estimate alone exceeds a whole window |
| 401 | `authentication_error` | Missing or unknown bearer token (with `WWW-Authenticate`) |
| 429 | `rate_limit_exceeded` | Tenant budget spent, or every provider rate limited (with `Retry-After`) |
| 502 | `upstream_unavailable` | Provider error, refused connection, or a garbled answer |
| 503 | `service_unavailable` | The rate limit store cannot be used |
| 504 | `upstream_timeout` | No provider answered in time |

Provider error bodies are never read: only the status code and `Retry-After`. The mock's 500 carries a stack trace, a file path, an internal IP and an account id, and the tests check none of them reaches the client. The provider keys come from the gateway's environment and the caller's own token is never forwarded. The request id ties the client's error to the server's log lines and the audit record, which holds counts and the route taken (`primary:rate_limited,backup:ok`), never prompt text.

## Known limits

- **One database file.** SQLite serializes writers, which is plenty for one host (each reservation is one short transaction) and wrong for a fleet. For several hosts the same reserve-and-settle logic would move to Redis (a Lua script for the atomic check) or Postgres (`SELECT ... FOR UPDATE`).
- **Wall-clock time.** The window uses wall-clock seconds so it survives a restart. If the clock is stepped backwards, recent rows count for longer than a minute, which errs toward refusing rather than allowing.
- **The estimate is an estimate.** Bytes / 4 is not a tokenizer. It only decides admission; the charge is corrected to real usage, so the budget itself stays accurate.
- **A timed-out primary may still bill.** Cancelling our side does not stop the provider finishing the request.
- **Tenant = token subject.** Each bearer token's subject is one tenant. Per-key budgets, tiers and per-model limits would be a settings change, not a design change.
