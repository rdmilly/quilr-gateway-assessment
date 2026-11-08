# Task 3: streaming PII guardrail

An LLM gateway endpoint, `POST /v1/chat/completions` (OpenAI-compatible), that passes the request to a model provider and streams the answer back with emails, Social Security numbers and payment card numbers replaced by `[REDACTED]` as they go by. It never holds the whole answer, and it only delays text that might still turn out to be PII.

```
client ──▶ gateway :8081 ──▶ provider (mock :9100)
       ◀── redacted SSE ◀── SSE chunks
              │ per choice, per tool call: StreamRedactor
              │ release what can't be PII, hold only a possible match (≤ 256 chars)
```

## Run it

```bash
uv sync
cp config.example.toml config.toml
uv run python -m mocks.llm_provider        # terminal 1: a fake provider on 127.0.0.1:9100
uv run python -m task3_pii_guardrail       # terminal 2: the gateway on 127.0.0.1:8081
```

The mock provider echoes the last user message, so you choose what the "model" says. `"mock": {"chunk_chars": 2}` makes it stream two characters at a time, splitting the PII across many chunks:

```bash
curl -sN localhost:8081/v1/chat/completions -H content-type:application/json -d '{
  "model": "mock-echo", "stream": true, "mock": {"chunk_chars": 2},
  "messages": [{"role": "user", "content": "Email john.doe@example.com, SSN 123-45-6789, card 4111 1111 1111 1111."}]
}'
```

Joined back together, the `content` deltas read `Email [REDACTED], SSN [REDACTED], card [REDACTED].`

## How the streaming works

The hard part is that PII arrives in pieces: one chunk ends in `john.d`, the next starts `oe@exa`. `redactor.py` handles that with one rule per piece of text:

- **Text that can't be part of a match is sent at once.** Most text, most of the time.
- **A complete match followed by more text is final.** It is replaced and sent.
- **A match that reaches the end of what has arrived might still grow**, complete or not (`john@ex.c` may become `john@ex.com`). It is held until the next chunk decides it.
- **Nothing is held longer than 256 characters**, the longest anything could match (an email address is at most 254). Past that it cannot be PII, so it is released.

Two details make this exact rather than approximate:

- **Partial matching.** The `regex` library can report "this could still match if more text came" (`partial=True`), which the standard `re` cannot.
- **An anchored check for complete matches.** Partial matching alone reports `a@mail.example` as complete when the text so far is `a@mail.example.`, missing that the dot may continue it. So before treating a match as final, the redactor asks whether the text from its start could still grow. The property tests found this.

Each choice (`n` > 1) and each tool call's arguments get their own redactor, so PII split across chunks of one stream is caught and different streams never mix. When a choice finishes, anything still held for it is released into that same chunk, before `finish_reason`.

**Non-streamed answers are redacted too.** Otherwise `"stream": false` would be a way around the guardrail.

## What counts as PII

All in `patterns.py`.

| Kind | Matched | Deliberately not matched |
| --- | --- | --- |
| Email | `local@domain.tld`, practical pattern, Unicode letters allowed | `user@localhost`, `john@` alone, "john at example dot com" |
| SSN | `123-45-6789`, `123 45 6789`, including numbers the SSA never issues (`999-99-9999`), so made-up test data does not leak | 9 bare digits (too many order numbers look like that), mixed separators, a 3-2-4 shape inside a longer number |
| Card | 13 to 19 digits with optional single spaces or dashes, if the Luhn checksum passes, or if written in 4-4-4-4 groups even with a bad checksum | Order and tracking numbers that fail Luhn, a card-shaped window inside a longer digit run |

`\d` and `\w` match Unicode digits and letters, so full-width digits are caught without a normalization pass.

Out of scope, and said here rather than claimed: PII spelled out in words, deliberately obfuscated addresses, and kinds the brief does not list (names, phone numbers, street addresses).

## Latency and memory

Measured with `uv run python -m task3_pii_guardrail.bench`: a long answer with PII in every sentence, fed in 4-character chunks (about one model token each), on a 4-core x86 VPS with Python 3.14:

| | |
| --- | --- |
| Median time added per chunk | 5 µs |
| 99th percentile per chunk | 29 µs |
| Throughput | about 620,000 characters per second |
| Most text ever held back | 19 characters |

Over real sockets (`tests/task3/test_processes.py`), the first text reaches the client about 20 ms into an 840 ms stream: the gateway does not wait for the provider to finish. Memory per stream is bounded by 256 held characters per choice plus one incoming chunk; the SSE reader refuses any single event over 1 MiB.

## Errors

Provider failures come back in one shape, `{"error": {"type", "code", "message", "request_id"}}`, with fixed messages. The provider's error body is never read, let alone forwarded, so stack traces, internal hosts and account ids cannot leak. A provider 429 becomes a 429 with its `Retry-After` kept.

The provider gets the gateway's own key (from the environment variable named in `provider_api_key_env`), never the caller's.

## Tests

`tests/task3/`:

- `test_patterns.py`: what counts as PII.
- `test_redactor.py`: property tests that streaming output equals whole-text redaction for any split, that PII never survives any split, and that held text stays under the cap. Also checked offline on 139,000 random cases with zero mismatches.
- `test_sse.py`: the SSE reader split at every byte, including inside multi-byte characters.
- `test_completions.py`: per-choice and per-tool-call redaction and flushing.
- `test_endpoint.py`: the endpoint against the mock provider: chunk sizes 1 to 50, `n` > 1, tool calls, non-streamed answers, error cleaning, audit records.
- `test_processes.py`: real processes and sockets, including time to first token.

## Known limits

- Only the response is redacted, as the brief asks. Redacting PII in the prompt before it reaches the provider would be a natural next step.
- Text is held only while it could still be PII, so a word at the very end of a chunk waits for the next chunk (it could be the start of an email). In practice that is a few characters.
- Caller authentication is not part of this task; Task 4 adds a per-tenant API key in front of the same lane.
