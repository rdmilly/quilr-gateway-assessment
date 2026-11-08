"""``POST /v1/chat/completions``: pass a chat request to the provider, redact the answer.

The request body goes to the provider unchanged, with the gateway's own
provider key (if configured) instead of anything the caller sent. The answer
comes back through the redactor:

* **Streamed** (``"stream": true``): each upstream chunk is parsed, redacted
  and sent on at once. Only text that might still be the start of an email,
  SSN or card number is held back, so the first token is not delayed, and the
  full response is never held in memory.
* **Not streamed**: the whole JSON answer is redacted before it is returned.
  Leaving it unredacted would make ``stream: false`` a way around the guardrail.

Upstream failures come back as one standard error shape,
``{"error": {"type", "code", "message", "request_id"}}``, with fixed messages:
nothing from the provider's error body (stack traces, internal hosts, account
ids) ever reaches the caller. Every request leaves one audit record with the
number of redactions, never the text.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from shared.audit import AuditLog, Decision, DecisionEvent
from shared.errors import GatewayError, GatewayErrorType, new_request_id
from shared.logs import log_event
from shared.sse import EventTooLarge, SSEReader

from .completions import CompletionStreamRedactor, redact_completion
from .redactor import redact
from .settings import LlmGatewaySettings

LANE = "llm"
POLICY_ID = "llm.pii-redaction.v1"
PATH = "/v1/chat/completions"

logger = logging.getLogger(__name__)


def _frame(data: str) -> bytes:
    return f"data: {data}\n\n".encode()


async def _read_limited(stream: AsyncIterator[bytes], limit: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    async for chunk in stream:
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(
    settings: LlmGatewaySettings,
    *,
    client: httpx2.AsyncClient | None = None,
    audit: AuditLog | None = None,
    environ: Mapping[str, str] | None = None,
) -> Starlette:
    audit_log = audit if audit is not None else AuditLog()
    env = os.environ if environ is None else environ
    provider_key = env.get(settings.provider_api_key_env) if settings.provider_api_key_env else None
    state: dict[str, httpx2.AsyncClient] = {}
    if client is not None:
        state["client"] = client

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if client is not None:
            yield
            return
        timeout = httpx2.Timeout(settings.upstream_timeout_s, connect=5.0)
        async with httpx2.AsyncClient(timeout=timeout, follow_redirects=False) as own:
            state["client"] = own
            yield

    def fail(error: GatewayError, request_id: str) -> JSONResponse:
        return JSONResponse(error.body(request_id), error.status, headers=error.headers())

    def record(request_id: str, redactions: int, started: float, outcome: str) -> None:
        audit_log.record(
            DecisionEvent(
                request_id=request_id,
                lane=LANE,
                operation="chat.completions",
                decision=Decision.REDACTED if redactions else Decision.ALLOWED,
                reason=outcome,
                policy_id=POLICY_ID,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
                counts={"redactions": redactions},
            )
        )

    async def completions(request: Request) -> Response:
        started = time.perf_counter()
        request_id = new_request_id()

        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return fail(
                GatewayError(
                    GatewayErrorType.INVALID_REQUEST, "Content-Type must be application/json."
                ),
                request_id,
            )
        raw = await _read_limited(request.stream(), settings.max_body_bytes)
        if raw is None:
            return fail(
                GatewayError(GatewayErrorType.INVALID_REQUEST, "The request body is too large."),
                request_id,
            )
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return fail(
                GatewayError(GatewayErrorType.INVALID_REQUEST, "The body must be a JSON object."),
                request_id,
            )
        streaming = body.get("stream") is True

        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream" if streaming else "application/json",
        }
        if provider_key:
            headers["authorization"] = f"Bearer {provider_key}"
        upstream_request = state["client"].build_request(
            "POST", settings.provider_url, content=raw, headers=headers
        )
        try:
            upstream = await state["client"].send(upstream_request, stream=True)
        except httpx2.TimeoutException:
            return fail(GatewayError(GatewayErrorType.UPSTREAM_TIMEOUT), request_id)
        except httpx2.HTTPError:
            return fail(GatewayError(GatewayErrorType.UPSTREAM_UNAVAILABLE), request_id)

        if upstream.status_code != 200:
            status = upstream.status_code
            retry_after = upstream.headers.get("retry-after", "")
            await upstream.aclose()  # the provider's error body is never read, let alone sent on
            log_event(
                logger, logging.WARNING, "provider error", request_id=request_id, status=status
            )
            if status == 429:
                return fail(
                    GatewayError(
                        GatewayErrorType.RATE_LIMIT_EXCEEDED,
                        "The model provider is rate limiting requests. Retry later.",
                        int(retry_after) if retry_after.isdigit() else None,
                    ),
                    request_id,
                )
            return fail(GatewayError(GatewayErrorType.UPSTREAM_UNAVAILABLE), request_id)

        if not streaming:
            try:
                data = await _read_limited(upstream.aiter_bytes(), settings.max_response_bytes)
            finally:
                await upstream.aclose()
            try:
                answer = json.loads(data) if data is not None else None
            except ValueError:
                answer = None
            if not isinstance(answer, dict):
                return fail(GatewayError(GatewayErrorType.UPSTREAM_UNAVAILABLE), request_id)
            redactions = redact_completion(answer)
            record(request_id, redactions, started, "complete")
            return JSONResponse(answer, headers={"x-request-id": request_id})

        async def redacted_stream() -> AsyncIterator[bytes]:
            reader = SSEReader()
            redactor = CompletionStreamRedactor()
            outcome = "complete"
            done = False
            first_byte_ms: float | None = None

            def handle(event: str) -> list[bytes]:
                nonlocal done
                if event.strip() == "[DONE]":
                    out = []
                    rest = redactor.leftovers()
                    if rest is not None:
                        out.append(_frame(json.dumps(rest)))
                    out.append(_frame("[DONE]"))
                    done = True
                    return out
                try:
                    chunk = json.loads(event)
                except ValueError:
                    chunk = None
                if isinstance(chunk, dict):
                    return [_frame(json.dumps(redactor.process(chunk)))]
                return [_frame(redact(event))]  # not JSON: redact it as plain text

            try:
                async for raw_chunk in upstream.aiter_raw():
                    for event in reader.feed(raw_chunk):
                        for frame in handle(event):
                            if first_byte_ms is None:
                                first_byte_ms = (time.perf_counter() - started) * 1000
                            yield frame
                for event in reader.finish():
                    for frame in handle(event):
                        yield frame
                if not done:
                    rest = redactor.leftovers()
                    if rest is not None:
                        yield _frame(json.dumps(rest))
                    outcome = "upstream ended without [DONE]"
            except EventTooLarge:
                outcome = "upstream event too large; stream cut"
            except httpx2.HTTPError:
                outcome = "upstream stream broke"
            finally:
                await upstream.aclose()
                record(request_id, redactor.redactions, started, outcome)
                log_event(
                    logger,
                    logging.INFO,
                    "stream done",
                    request_id=request_id,
                    redactions=redactor.redactions,
                    outcome=outcome,
                    first_byte_ms=round(first_byte_ms or 0.0, 3),
                )

        return StreamingResponse(
            redacted_stream(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
                "x-request-id": request_id,
            },
        )

    return Starlette(routes=[Route(PATH, completions, methods=["POST"])], lifespan=lifespan)
