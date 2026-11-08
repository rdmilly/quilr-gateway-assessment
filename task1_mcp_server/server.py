"""Wires the tools into the MCP SDK's low-level ``Server`` and serves it on stdio.

Why the low-level ``Server`` and not the decorator API (``MCPServer``): the
decorator API turns argument validation failures into ``isError`` tool results.
The brief asks for standard JSON-RPC error codes, so validation happens in
``tools.call_tool`` and a failure raises ``MCPError(-32602)``, which the SDK
sends as a JSON-RPC error response.

Keeping stdout clean takes four layers:

1. Logging is configured to stderr before anything else runs.
2. While it serves, ``sys.stdout`` points at stderr, so ``print()`` and any
   Python library writing to stdout land in stderr. This has to be done here:
   Python buffers ``print()`` output, and a buffer flushed at exit would reach
   the real stdout after the SDK has given it back.
3. While it serves, the SDK points file descriptor 1 at stderr and keeps a
   private copy for the protocol, which catches writes that bypass Python,
   such as C extensions or ``os.write(1, ...)``.
4. Tests run the real server as a subprocess, including one with a tool that
   calls ``print()``, and parse every byte of its stdout as JSON-RPC
   (``tests/task1/test_stdio.py``).
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Iterator
from typing import Any

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from shared.logs import configure_logging, log_event

from . import __version__
from .customers import CustomerStore
from .tools import TOOLS, call_tool
from .transport import serve_draining

SERVER_NAME = "quilr-customer-service"

logger = logging.getLogger(__name__)


def build_server(store: CustomerStore | None = None) -> Server[Any]:
    customers = store if store is not None else CustomerStore()

    async def on_list_tools(
        ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[spec.to_tool() for spec in TOOLS])

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        return call_tool(params.name, params.arguments, customers, request_id=ctx.request_id)

    return Server(
        SERVER_NAME,
        version=__version__,
        instructions="Customer-service tools. trigger_refund moves money; confirm before calling.",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


@contextlib.contextmanager
def stdout_goes_to_stderr() -> Iterator[None]:
    """Send Python-level writes to stdout to stderr until the block exits."""
    original = sys.stdout
    # Anything already buffered (say, printed by an import) is flushed now, while
    # the SDK has descriptor 1 pointed at stderr, rather than at exit.
    original.flush()
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stderr.flush()
        sys.stdout = original


async def serve_stdio(server: Server[Any]) -> None:
    """Serve until stdin closes, answer what is still in flight, then return."""
    async with stdio_server() as (read_stream, write_stream):
        with stdout_goes_to_stderr():
            await serve_draining(server, read_stream, write_stream)


def main() -> None:
    configure_logging()
    log_event(logger, logging.INFO, "server starting", transport="stdio", version=__version__)
    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(serve_stdio, build_server())
    log_event(logger, logging.INFO, "server stopped")
