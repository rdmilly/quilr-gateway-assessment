"""``POST /v1/chat/completions``: rate limit per tenant, then route with failover.

Order of checks, cheapest first:

1. **Who.** A valid bearer token is required (401 otherwise). The token's subject is the
   tenant whose budget is charged.
2. **Is it a request.** JSON object, under the size limit, with messages (400 otherwise).
3. **Can they afford it.** The estimate is reserved in SQLite (see limiter.py). Over budget:
   429 with ``Retry-After``. Bigger than a whole window: 400, since retrying cannot help.
   Store broken: 503, never an uncounted pass.
4. **Route.** Primary, then backup on 429 or timeout (see router.py).
5. **Settle.** The reservation is corrected to the provider's reported usage, or refunded
   if no provider answered. One reservation per request, so a failover never charges twice.

To get real usage for streams, the endpoint asks the provider for it
(``stream_options.include_usage``). If the client did not ask for usage itself, the extra
usage chunk and fields are removed before the stream reaches it, so the client sees what
it asked for.

Every error leaves as ``{"error": {"type", "code", "message", "request_id"}}`` with a fixed
message. Provider error bodies are never read, so their stack traces, hosts and account ids
cannot reach a client.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import anyio.to_thread
import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from shared.audit import AuditLog, Decision, DecisionEvent
from shared.auth import Authenticator
from shared.errors import GatewayError, GatewayErrorType, new_request_id
from shared.logs import log_event
from shared.sse import EventTooLarge, SSEReader

from .estimate import InvalidRequest, estimate_tokens
from .limiter import (
    RateLimited,
    RequestTooLarge,
    Reservation,
    SlidingWindowLimiter,
    StoreUnavailable,
)
from .router import AllProvidersFailed, Failure, Opened, Provider, Router
from .settings import RouterSettings

LANE = "llm"
POLICY_ID = "llm.token-budget.v1"
PATH = "/v1/chat/completions"

logger = logging.getLogger(__name__)

_FAILURE_ERRORS = {
    Failure.RATE_LIMITED: (
        GatewayErrorType.RATE_LIMIT_EXCEEDED,
        "The model providers are rate limiting requests. Retry later.",
    ),
    Failure.TIMEOUT: (GatewayErrorType.UPSTREAM_TIMEOUT, None),
    Failure.ERROR: (GatewayErrorType.UPSTREAM_UNAVAILABLE, None),
}


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


def _total_tokens(usage: object) -> int | None:
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            return total
    return None


def create_app(
    settings: RouterSettings,
    authenticator: Authenticator,
    limiter: SlidingWindowLimiter,
    providers: list[Provider],
    *,
    client: httpx2.AsyncClient | None = None,
    audit: AuditLog | None = None,
) -> Starlette:
    audit_log = audit if audit is not None else AuditLog()
    state: dict[str, Router] = {}

    def make_router(http: httpx2.AsyncClient) -> Router:
        return Router(
            http,
            providers,
            timeout_s=settings.failover_timeout_ms / 1000,
            failover_on_errors=settings.failover_on_errors,
            max_response_bytes=settings.max_response_bytes,
        )

    if client is not None:
        state["router"] = make_router(client)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if client is not None:
            yield
            return
        # The router and the relay enforce the real deadlines (failover, stream idle). The
        # client's own timeout is only a backstop and must sit above both: if it were shorter,
        # it would fire first and decide failover on the wrong clock.
        backstop = max(settings.failover_timeout_ms / 1000, settings.stream_idle_timeout_s) + 5
        timeout = httpx2.Timeout(backstop, connect=min(5.0, backstop))
        async with httpx2.AsyncClient(timeout=timeout, follow_redirects=False) as own:
            state["router"] = make_router(own)
            yield

    def fail(error: GatewayError, request_id: str) -> JSONResponse:
        headers = {**error.headers(), "x-request-id": request_id}
        if error.kind is GatewayErrorType.AUTHENTICATION_ERROR:
            headers["WWW-Authenticate"] = 'Bearer realm="llm-gateway"'
        return JSONResponse(error.body(request_id), error.status, headers=headers)

    def record(
        request_id: str,
        tenant: str | None,
        decision: Decision,
        reason: str,
        started: float,
        counts: dict[str, int] | None = None,
        target: str | None = None,
    ) -> None:
        audit_log.record(
            DecisionEvent(
                request_id=request_id,
                lane=LANE,
                operation="chat.completions",
                decision=decision,
                reason=reason,
                policy_id=POLICY_ID,
                subject=tenant,
                target=target,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
                counts=counts or {},
            )
        )

    async def settle(reservation: Reservation, actual: int | None) -> int:
        """Correct the charge. Returns the tokens finally charged."""
        if actual is None:
            return reservation.tokens  # no usage reported: the estimate stands
        try:
            await anyio.to_thread.run_sync(limiter.settle, reservation, actual)
        except StoreUnavailable:
            log_event(logger, logging.ERROR, "settle failed; estimate kept")
            return reservation.tokens
        return actual

    async def completions(request: Request) -> Response:
        started = time.perf_counter()
        request_id = new_request_id()

        identity = authenticator.identify(request.headers.get("authorization"))
        if identity is None:
            record(request_id, None, Decision.DENIED, "unauthenticated", started)
            return fail(GatewayError(GatewayErrorType.AUTHENTICATION_ERROR), request_id)
        tenant = identity.subject

        def invalid(message: str) -> JSONResponse:
            record(request_id, tenant, Decision.DENIED, "invalid request", started)
            return fail(GatewayError(GatewayErrorType.INVALID_REQUEST, message), request_id)

        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return invalid("Content-Type must be application/json.")
        raw = await _read_limited(request.stream(), settings.max_body_bytes)
        if raw is None:
            return invalid("The request body is too large.")
        try:
            body: Any = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return invalid("The body must be a JSON object.")
        try:
            estimate = estimate_tokens(body, settings.default_max_tokens)
        except InvalidRequest as exc:
            return invalid(str(exc))

        streaming = body.get("stream") is True
        stream_options = body.get("stream_options")
        if stream_options is not None and not isinstance(stream_options, dict):
            return invalid("stream_options must be an object.")
        client_wants_usage = bool((stream_options or {}).get("include_usage"))
        if streaming and not client_wants_usage:
            body["stream_options"] = {**(stream_options or {}), "include_usage": True}
            raw = json.dumps(body).encode()

        try:
            reservation = await anyio.to_thread.run_sync(limiter.reserve, tenant, estimate)
        except RequestTooLarge:
            record(request_id, tenant, Decision.DENIED, "request exceeds window", started)
            return fail(GatewayError(GatewayErrorType.REQUEST_TOO_LARGE), request_id)
        except RateLimited as exc:
            record(request_id, tenant, Decision.DENIED, "token budget exceeded", started)
            return fail(
                GatewayError(GatewayErrorType.RATE_LIMIT_EXCEEDED, None, exc.retry_after_s),
                request_id,
            )
        except StoreUnavailable:
            log_event(logger, logging.ERROR, "rate limit store unavailable", request_id=request_id)
            record(request_id, tenant, Decision.DENIED, "limiter unavailable", started)
            return fail(GatewayError(GatewayErrorType.SERVICE_UNAVAILABLE), request_id)

        try:
            opened = await state["router"].open(raw, streaming=streaming)
        except AllProvidersFailed as exc:
            with anyio.CancelScope(shield=True):
                try:
                    await anyio.to_thread.run_sync(limiter.release, reservation)
                except StoreUnavailable:
                    log_event(logger, logging.ERROR, "release failed", request_id=request_id)
            path = ",".join(f"{a.provider}:{a.outcome}" for a in exc.attempts)
            log_event(
                logger,
                logging.WARNING,
                "no provider answered",
                request_id=request_id,
                attempts=path,
            )
            record(
                request_id,
                tenant,
                Decision.ALLOWED,
                f"providers failed: {path}",
                started,
                {"estimated_tokens": estimate, "charged_tokens": 0},
            )
            kind, message = _FAILURE_ERRORS[exc.failure]
            retry = exc.retry_after_s if exc.failure is Failure.RATE_LIMITED else None
            return fail(GatewayError(kind, message, retry), request_id)
        except BaseException:  # the client went away mid-route: refund, then let it unwind
            with anyio.CancelScope(shield=True):
                try:
                    await anyio.to_thread.run_sync(limiter.release, reservation)
                except StoreUnavailable:
                    log_event(logger, logging.ERROR, "release failed", request_id=request_id)
            raise

        route = ",".join(f"{a.provider}:{a.outcome}" for a in opened.attempts)
        headers = {"x-request-id": request_id, "x-gateway-provider": opened.provider.name}

        if not streaming:
            return await finish_plain(
                opened, reservation, request_id, tenant, estimate, route, started, headers
            )
        headers.update({"cache-control": "no-cache", "x-accel-buffering": "no"})
        return StreamingResponse(
            relay(
                opened,
                reservation,
                client_wants_usage,
                request_id,
                tenant,
                estimate,
                route,
                started,
            ),
            media_type="text/event-stream",
            headers=headers,
        )

    async def finish_plain(
        opened: Opened,
        reservation: Reservation,
        request_id: str,
        tenant: str,
        estimate: int,
        route: str,
        started: float,
        headers: dict[str, str],
    ) -> Response:
        try:
            answer = json.loads(opened.body) if opened.body is not None else None
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            # The provider answered and probably billed, so the estimate stays charged.
            record(
                request_id,
                tenant,
                Decision.ALLOWED,
                f"bad provider answer: {route}",
                started,
                {"estimated_tokens": estimate, "charged_tokens": estimate},
            )
            return fail(GatewayError(GatewayErrorType.UPSTREAM_UNAVAILABLE), request_id)
        charged = await settle(reservation, _total_tokens(answer.get("usage")))
        record(
            request_id,
            tenant,
            Decision.ALLOWED,
            f"complete: {route}",
            started,
            {"estimated_tokens": estimate, "charged_tokens": charged},
            opened.provider.name,
        )
        return JSONResponse(answer, headers=headers)

    async def relay(
        opened: Opened,
        reservation: Reservation,
        client_wants_usage: bool,
        request_id: str,
        tenant: str,
        estimate: int,
        route: str,
        started: float,
    ) -> AsyncIterator[bytes]:
        reader = SSEReader()
        usage: int | None = None
        outcome = "complete"

        def handle(event: str) -> bytes | None:
            nonlocal usage
            if event.strip() == "[DONE]":
                return _frame("[DONE]")
            try:
                chunk = json.loads(event)
            except ValueError:
                return _frame(event)
            if not isinstance(chunk, dict):
                return _frame(event)
            reported = _total_tokens(chunk.get("usage"))
            if reported is not None:
                usage = reported
            if client_wants_usage:
                return _frame(event)
            if "usage" in chunk and chunk.get("choices") == []:
                return None  # the usage-only chunk we asked for on the client's behalf
            chunk.pop("usage", None)
            return _frame(json.dumps(chunk))

        rest = opened.rest
        assert rest is not None  # noqa: S101 - Router.open always sets it for streams
        try:
            pending = opened.first_chunk
            while True:
                for event in reader.feed(pending):
                    frame = handle(event)
                    if frame is not None:
                        yield frame
                with anyio.move_on_after(settings.stream_idle_timeout_s) as idle:
                    pending = await anext(rest, b"")
                if idle.cancelled_caught:
                    outcome = "provider went silent; stream cut"
                    break
                if not pending:
                    for event in reader.finish():
                        frame = handle(event)
                        if frame is not None:
                            yield frame
                    break
        except EventTooLarge:
            outcome = "provider event too large; stream cut"
        except httpx2.HTTPError:
            outcome = "provider stream broke"
        finally:
            with anyio.CancelScope(shield=True):
                await opened.response.aclose()
                charged = await settle(reservation, usage)
            record(
                request_id,
                tenant,
                Decision.ALLOWED,
                f"{outcome}: {route}",
                started,
                {"estimated_tokens": estimate, "charged_tokens": charged},
                opened.provider.name,
            )

    return Starlette(routes=[Route(PATH, completions, methods=["POST"])], lifespan=lifespan)
