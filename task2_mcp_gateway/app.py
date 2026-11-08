"""The gateway's HTTP endpoint: every request takes the same path through it.

1. **Origin.** A browser Origin not on the allow list: 403. (DNS rebinding.)
2. **Who are you.** No valid bearer token: 401 with ``WWW-Authenticate``.
   Checked before the body is read, so an anonymous caller costs almost nothing.
3. **Is it JSON.** Wrong content type: 415. Body over the limit: 413.
4. **Parse once.** Not exactly one JSON-RPC message: 400 with -32700 or -32600.
5. **Routing headers** present and equal to the body (2026-07-28): otherwise 400 with -32020.
6. **Policy.** ``tools/call`` on an ``admin_`` tool without the admin role:
   ``-32001 Unauthorized Tool Call``, and the downstream server is never contacted.
7. **Forward** the parsed message to the one downstream server; stream its reply back.

Every tool call decision, allowed or denied, is written to the audit log.

A denied tool call is answered with HTTP 200 and a JSON-RPC error body, the way
an MCP server answers a request it refuses. That is what lets an MCP client
show ``-32001``: the official SDK client reports any non-2xx status as a
generic transport error and the code would be lost.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from shared.audit import AuditLog, Decision, DecisionEvent
from shared.auth import Authenticator, Identity
from shared.errors import (
    HEADER_MISMATCH,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    UNAUTHORIZED_TOOL_CALL,
    JsonRpcId,
    jsonrpc_error,
    new_request_id,
)
from shared.logs import log_event

from .jsonrpc import Kind, Message, ParseProblem, parse_message
from .mcp_headers import LEGACY_VERSIONS, header_problem
from .policy import POLICY_ID, decide
from .proxy import Downstream, body_chunks, response_headers
from .settings import GatewaySettings

AUTHENTICATION_REQUIRED = -31001
"""Missing or invalid bearer token. MCP asks for new application codes outside the
JSON-RPC reserved range (-32768 to -32000), so this one sits just above it. The
HTTP 401 is what clients act on; the code is for logs and humans."""

MCP_PATH = "/mcp"
LANE = "mcp"

logger = logging.getLogger(__name__)


def _error_without_id(status: int, code: int, message: str) -> JSONResponse:
    """For refusals that are not an answer to one request; MCP says these carry no id."""
    return JSONResponse({"jsonrpc": "2.0", "error": {"code": code, "message": message}}, status)


def _error(
    status: int, request_id: JsonRpcId, code: int, message: str, **extra: Any
) -> JSONResponse:
    headers = extra.pop("headers", None)
    data = extra or None
    return JSONResponse(jsonrpc_error(request_id, code, message, data), status, headers=headers)


async def _read_body(request: Request, limit: int) -> bytes | None:
    """The body, or None if it is larger than ``limit`` bytes. Stops reading once over."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(
    settings: GatewaySettings,
    authenticator: Authenticator,
    *,
    client: httpx2.AsyncClient | None = None,
    audit: AuditLog | None = None,
) -> Starlette:
    audit_log = audit if audit is not None else AuditLog()
    state: dict[str, Downstream] = {}

    if client is not None:
        state["downstream"] = Downstream(settings.downstream_url, client)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if client is not None:
            yield
            return
        timeout = httpx2.Timeout(settings.upstream_timeout_s, connect=5.0)
        async with httpx2.AsyncClient(timeout=timeout, follow_redirects=False) as own_client:
            state["downstream"] = Downstream(settings.downstream_url, own_client)
            yield

    def record(
        message: Message,
        identity: Identity,
        verdict_allowed: bool,
        reason: str,
        tool: str | None,
        request_id: str,
        started: float,
    ) -> None:
        audit_log.record(
            DecisionEvent(
                request_id=request_id,
                lane=LANE,
                operation=message.method or "",
                decision=Decision.ALLOWED if verdict_allowed else Decision.DENIED,
                reason=reason,
                policy_id=POLICY_ID,
                subject=identity.subject,
                role=identity.role.value,
                target=tool,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
            )
        )

    async def handle(request: Request) -> Response:
        started = time.perf_counter()
        request_id = new_request_id()
        headers = request.headers

        origin = headers.get("origin")
        if origin is not None and origin not in settings.allowed_origins:
            return _error_without_id(403, INVALID_REQUEST, "Origin not allowed")

        identity = authenticator.identify(headers.get("authorization"))
        if identity is None:
            log_event(logger, logging.INFO, "unauthenticated", request_id=request_id)
            return _error(
                401,
                None,
                AUTHENTICATION_REQUIRED,
                "Authentication required",
                headers={"WWW-Authenticate": 'Bearer realm="mcp-gateway"'},
            )

        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return _error(415, None, INVALID_REQUEST, "Content-Type must be application/json")

        raw = await _read_body(request, settings.max_body_bytes)
        if raw is None:
            return _error(413, None, INVALID_REQUEST, "Request body too large")

        try:
            message = parse_message(raw)
        except ParseProblem as problem:
            log_event(
                logger,
                logging.INFO,
                "unreadable message",
                request_id=request_id,
                code=problem.code,
                subject=identity.subject,
            )
            return _error(400, problem.request_id, problem.code, problem.message)

        header_issue = header_problem(headers, message)
        if header_issue is not None:
            return _error(400, message.id, HEADER_MISMATCH, "Header mismatch", detail=header_issue)

        version_header = headers.get("mcp-protocol-version")
        if message.kind is Kind.RESPONSE and version_header not in (None, *LEGACY_VERSIONS):
            # From 2026-07-28 on, clients send only requests and notifications.
            return _error(400, message.id, INVALID_REQUEST, "Invalid Request: unexpected response")

        verdict = decide(identity, message)
        if message.method == "tools/call":
            record(
                message,
                identity,
                verdict.allowed,
                verdict.reason,
                verdict.tool,
                request_id,
                started,
            )
        if not verdict.allowed:
            log_event(
                logger,
                logging.WARNING,
                "tool call denied",
                request_id=request_id,
                subject=identity.subject,
                role=identity.role.value,
                tool=verdict.tool,
            )
            if message.kind is Kind.NOTIFICATION:
                # A notification the server cannot accept gets an HTTP error status, never 202.
                return _error_without_id(403, UNAUTHORIZED_TOOL_CALL, "Unauthorized Tool Call")
            return _error(
                200,
                message.id,
                UNAUTHORIZED_TOOL_CALL,
                "Unauthorized Tool Call",
                tool=verdict.tool,
                required_role="admin",
            )

        try:
            upstream = await state["downstream"].send(
                message.to_bytes(), headers, identity, request_id
            )
        except httpx2.TimeoutException:
            log_event(logger, logging.WARNING, "downstream timeout", request_id=request_id)
            return _error(504, message.id, INTERNAL_ERROR, "Upstream timeout")
        except httpx2.HTTPError:
            log_event(logger, logging.WARNING, "downstream unavailable", request_id=request_id)
            return _error(502, message.id, INTERNAL_ERROR, "Upstream unavailable")

        log_event(
            logger,
            logging.INFO,
            "forwarded",
            request_id=request_id,
            subject=identity.subject,
            method=message.method,
            kind=message.kind.value,
            status=upstream.status_code,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return StreamingResponse(
            body_chunks(upstream),
            status_code=upstream.status_code,
            headers=response_headers(upstream),
        )

    async def not_allowed(request: Request) -> Response:
        return Response(status_code=405, headers={"Allow": "POST"})

    return Starlette(
        routes=[
            Route(MCP_PATH, handle, methods=["POST"]),
            Route(MCP_PATH, not_allowed, methods=["GET", "DELETE", "PUT", "PATCH"]),
        ],
        lifespan=lifespan,
    )
