"""Forward an allowed message to the downstream server and hand back its reply.

Only the headers MCP needs are forwarded. The caller's ``Authorization`` is
never passed on: the downstream server did not issue that token and must not
accept it (MCP forbids token passthrough). Instead the gateway states who the
caller is in ``X-Gateway-Subject`` and ``X-Gateway-Role``. The server should
only trust those on a network where nothing but the gateway can reach it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

import httpx2

from shared.auth import Identity

_FORWARD_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "content-type",
        "mcp-protocol-version",
        "mcp-method",
        "mcp-name",
        "mcp-session-id",
        "last-event-id",
        "traceparent",
        "tracestate",
    }
)
_RETURN_RESPONSE_HEADERS = frozenset(
    {"content-type", "mcp-session-id", "mcp-protocol-version", "cache-control", "retry-after"}
)


def request_headers(
    incoming: Mapping[str, str], identity: Identity, request_id: str
) -> dict[str, str]:
    headers = {
        name: value
        for name, value in incoming.items()
        if name in _FORWARD_REQUEST_HEADERS or name.startswith("mcp-param-")
    }
    headers["x-gateway-subject"] = identity.subject
    headers["x-gateway-role"] = identity.role.value
    headers["x-request-id"] = request_id
    return headers


def response_headers(upstream: httpx2.Response) -> dict[str, str]:
    headers = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() in _RETURN_RESPONSE_HEADERS
    }
    if upstream.headers.get("content-type", "").startswith("text/event-stream"):
        # Tells proxies such as nginx to pass each event on at once instead of buffering.
        headers["x-accel-buffering"] = "no"
    return headers


class Downstream:
    """The one MCP server behind the gateway."""

    def __init__(self, url: str, client: httpx2.AsyncClient) -> None:
        self.url = url
        self._client = client

    async def send(
        self, body: bytes, incoming: Mapping[str, str], identity: Identity, request_id: str
    ) -> httpx2.Response:
        """Start the request and return as soon as the status and headers arrive.

        The body is streamed afterwards, so a long SSE response passes through
        without being held in memory. The caller must close the response.
        """
        request = self._client.build_request(
            "POST", self.url, content=body, headers=request_headers(incoming, identity, request_id)
        )
        return await self._client.send(request, stream=True)


async def body_chunks(upstream: httpx2.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
    finally:
        await upstream.aclose()
