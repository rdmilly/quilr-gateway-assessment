"""A thin layer between the SDK's stdio transport and the server.

It fixes two things the SDK leaves out, both visible on the wire.

**Unreadable input gets an answer.** The SDK hands a line it cannot parse to the
server as an exception, and the server drops it, so the client waits forever.
JSON-RPC 2.0 says the server must reply. This layer replies with:

* ``-32700 Parse error`` (``id: null``) when the line is not JSON at all, which
  includes JSON with a lone surrogate such as ``"\\ud800"``.
* ``-32600 Invalid Request`` when it is JSON but not a JSON-RPC message, for
  example a batch (``[...]``), which MCP does not allow. If the message had a
  usable ``id``, the reply carries it so the client can match it up.

Blank lines are skipped. Neither reply repeats what the client sent.

**Requests with an invalid id get an answer too.** MCP says a request id is a
string or an integer and never ``null``. The SDK reads a message with
``"id": null`` (or an object, array, boolean or fraction) as a notification,
drops the id, and so never replies. ``checked_lines`` looks at each raw line
before the SDK does and swaps such a request for one the SDK will reject, which
then gets ``-32600`` like any other invalid request. Because this passes the SDK
an explicit input stream, the SDK no longer points descriptor 0 at the null
device while serving; that only matters to a tool that starts a child process
reading stdin, and these tools start none.

**Requests already received get answered before exit.** When stdin closes, the
SDK stops the server straight away and any request still being handled never
gets its response. That bites the most common way to try a stdio server::

    printf '%s\\n' '<request 1>' '<request 2>' | python -m task1_mcp_server

So this layer counts requests in and responses out, and only passes the end of
input on to the server once every request has been answered, or once
``drain_timeout_s`` runs out so a stuck request cannot keep the process alive.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import AsyncIterator
from typing import Any, cast

import anyio
from anyio.abc import ObjectReceiveStream, ObjectSendStream
from mcp import types
from mcp.server import Server
from mcp.shared.message import SessionMessage
from pydantic import ValidationError

from shared.errors import INVALID_REQUEST, PARSE_ERROR
from shared.logs import log_event

DRAIN_TIMEOUT_S = 10.0

# Valid JSON that is not a JSON-RPC message, so the SDK rejects it and
# ``error_reply_for`` answers -32600 with no id.
INVALID_ID_PLACEHOLDER = '{"jsonrpc": "2.0", "invalid_request_id": true}\n'

logger = logging.getLogger(__name__)


class _InFlight:
    """IDs of requests that have come in but not been answered yet."""

    def __init__(self) -> None:
        self._ids: set[types.RequestId] = set()
        self._empty = anyio.Event()
        self._empty.set()

    def add(self, request_id: types.RequestId) -> None:
        if not self._ids:
            self._empty = anyio.Event()
        self._ids.add(request_id)

    def answered(self, request_id: types.RequestId | None) -> None:
        if request_id is None:
            return
        self._ids.discard(request_id)
        if not self._ids:
            self._empty.set()

    async def wait_until_empty(self) -> None:
        await self._empty.wait()


def _recover_id(exc: ValidationError) -> types.RequestId | None:
    """The ``id`` of a malformed message, if it had a valid one."""
    for err in exc.errors(include_url=False):
        candidate = err.get("input")
        if isinstance(candidate, dict):
            request_id = candidate.get("id")
            if isinstance(request_id, str) or (
                isinstance(request_id, int) and not isinstance(request_id, bool)
            ):
                return request_id
            return None
    return None


def _is_valid_request_id(value: object) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def screen_line(line: str) -> str:
    """Return the line unchanged, or a placeholder if it is a request with an invalid id."""
    if '"id"' not in line:  # cheap check first: most lines are not affected
        return line
    try:
        message = json.loads(line)
    except ValueError:
        return line  # not JSON: the SDK reports it, and we answer -32700
    if (
        isinstance(message, dict)
        and "method" in message
        and "id" in message
        and not _is_valid_request_id(message["id"])
    ):
        log_event(logger, logging.INFO, "request with invalid id")
        return INVALID_ID_PLACEHOLDER
    return line


class _CheckedLines:
    """A line source for ``stdio_server`` that runs every line through ``screen_line``."""

    def __init__(self, source: Any) -> None:
        self._source = source

    def __aiter__(self) -> AsyncIterator[str]:
        return self._lines()

    async def _lines(self) -> AsyncIterator[str]:
        async for line in self._source:
            yield screen_line(line)


def checked_stdin() -> anyio.AsyncFile[str]:
    """Stdin as UTF-8 text (bad bytes become U+FFFD, as in the SDK), screened line by line."""
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    # stdio_server only iterates over the lines, so this small wrapper is all it needs.
    return cast("anyio.AsyncFile[str]", _CheckedLines(anyio.wrap_file(sys.stdin)))


def error_reply_for(exc: Exception) -> SessionMessage | None:
    """The JSON-RPC error to send for a line the SDK could not read, or None to stay silent."""
    if not isinstance(exc, ValidationError):
        code, message, request_id = INVALID_REQUEST, "Invalid Request", None
    else:
        errors = exc.errors(include_url=False)
        if errors and errors[0]["type"] == "json_invalid":
            raw = errors[0].get("input")
            if isinstance(raw, str | bytes) and not raw.strip():
                return None  # a blank line is not a message
            code, message, request_id = PARSE_ERROR, "Parse error", None
        else:
            code, message, request_id = INVALID_REQUEST, "Invalid Request", _recover_id(exc)
    log_event(logger, logging.INFO, "unreadable message", code=code, request_id=request_id)
    return SessionMessage(
        types.JSONRPCError(
            jsonrpc="2.0",
            id=request_id,
            error=types.ErrorData(code=code, message=message),
        )
    )


async def serve_draining(
    server: Server[Any],
    read_stream: ObjectReceiveStream[SessionMessage | Exception],
    write_stream: ObjectSendStream[SessionMessage],
    *,
    drain_timeout_s: float = DRAIN_TIMEOUT_S,
) -> None:
    in_flight = _InFlight()
    to_server, server_reads = anyio.create_memory_object_stream[SessionMessage | Exception]()
    server_writes, from_server = anyio.create_memory_object_stream[SessionMessage]()

    async def forward_input() -> None:
        async with to_server:
            async for item in read_stream:
                if isinstance(item, Exception):
                    reply = error_reply_for(item)
                    if reply is not None:
                        # Sent through the server's output so one task does all the writing.
                        await server_writes.send(reply)
                    continue
                if isinstance(item.message, types.JSONRPCRequest):
                    in_flight.add(item.message.id)
                await to_server.send(item)
            # Input ended. Hold the server's input open until it has answered
            # everything, then closing it (leaving this block) stops the server.
            with anyio.move_on_after(drain_timeout_s):
                await in_flight.wait_until_empty()

    async def forward_output() -> None:
        # Closing write_stream when the server is done tells the transport there
        # is nothing more to write, which is what lets the process exit.
        async with from_server, write_stream:
            async for item in from_server:
                await write_stream.send(item)
                if isinstance(item.message, types.JSONRPCResponse | types.JSONRPCError):
                    in_flight.answered(item.message.id)

    async with anyio.create_task_group() as tg:
        tg.start_soon(forward_output)
        input_scope = anyio.CancelScope()

        async def forward_input_until_server_stops() -> None:
            with input_scope:
                await forward_input()

        tg.start_soon(forward_input_until_server_stops)
        try:
            await server.run(server_reads, server_writes, server.create_initialization_options())
        finally:
            input_scope.cancel()
            await server_writes.aclose()
