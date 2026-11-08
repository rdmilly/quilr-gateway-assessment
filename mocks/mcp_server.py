"""A small downstream MCP server for the Task 2 gateway to sit in front of.

A real MCP server on the official SDK, served over Streamable HTTP, with one
ordinary tool and one admin tool. It records the headers of every request it
receives, so tests can check what the gateway passed on (and what it did not).

    uv run python -m mocks.mcp_server        # listens on 127.0.0.1:9000/mcp ($MOCK_MCP_PORT)
"""

from __future__ import annotations

from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

ALLOWED_HOSTS = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]

TOOLS = [
    types.Tool(
        name="lookup_order",
        description="Look up an order by ID.",
        input_schema={
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    ),
    types.Tool(
        name="admin_reset_key",
        description="Rotate a customer's API key. Admins only.",
        input_schema={
            "type": "object",
            "properties": {"key_id": {"type": "string"}},
            "required": ["key_id"],
        },
    ),
]


async def _list_tools(
    ctx: Any, params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=TOOLS)


async def _call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    if params.name not in {tool.name for tool in TOOLS}:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Unknown tool: {params.name}")],
            is_error=True,
        )
    text = f"{params.name} ran with {params.arguments or {}}"
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


class _RecordHeaders:
    def __init__(self, app: ASGIApp, seen: list[dict[str, str]]) -> None:
        self.app = app
        self.seen = seen

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            self.seen.append(
                {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            )
        await self.app(scope, receive, send)


def create_app(
    seen_headers: list[dict[str, str]] | None = None, *, json_response: bool = False
) -> Any:
    """The mock's ASGI app. ``json_response=False`` answers over SSE, like most servers."""
    server = Server(
        "mock-downstream", version="1.0.0", on_list_tools=_list_tools, on_call_tool=_call_tool
    )
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=json_response,
        transport_security=TransportSecuritySettings(allowed_hosts=ALLOWED_HOSTS),
    )
    if seen_headers is None:
        return app
    wrapped = _RecordHeaders(app, seen_headers)
    wrapped.router = app.router  # type: ignore[attr-defined]  # tests run the app's lifespan
    return wrapped


if __name__ == "__main__":
    import uvicorn

    from shared.logs import configure_logging

    configure_logging()
    import os

    port = int(os.environ.get("MOCK_MCP_PORT", "9000"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port, access_log=False, log_config=None)
