"""When input ends, requests already received still get answered, within a time limit."""

import anyio
import pytest
from mcp import types
from mcp.server import Server
from mcp.shared.message import SessionMessage
from pydantic import ValidationError

from task1_mcp_server.transport import (
    INVALID_ID_PLACEHOLDER,
    error_reply_for,
    screen_line,
    serve_draining,
)

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
}


def slow_server(delay_s: float) -> Server[object]:
    async def on_call_tool(
        ctx: object, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        await anyio.sleep(delay_s)
        return types.CallToolResult(content=[types.TextContent(type="text", text="done")])

    return Server("slow", on_call_tool=on_call_tool)


async def run_one_request_then_close_input(
    server: Server[object], drain_timeout_s: float
) -> list[SessionMessage]:
    client_send, server_read = anyio.create_memory_object_stream[SessionMessage | Exception](10)
    server_write, client_read = anyio.create_memory_object_stream[SessionMessage](10)
    request = types.JSONRPCRequest(
        jsonrpc="2.0",
        id=7,
        method="tools/call",
        params={"name": "anything", "arguments": {}, "_meta": META},
    )
    await client_send.send(SessionMessage(request))
    await client_send.aclose()  # end of input, immediately
    with anyio.fail_after(5):
        await serve_draining(server, server_read, server_write, drain_timeout_s=drain_timeout_s)
    return [message async for message in client_read]


async def test_request_in_flight_at_end_of_input_is_still_answered() -> None:
    replies = await run_one_request_then_close_input(slow_server(0.2), drain_timeout_s=5)
    assert len(replies) == 1
    assert isinstance(replies[0].message, types.JSONRPCResponse)
    assert replies[0].message.id == 7


async def test_a_request_that_never_finishes_cannot_keep_the_process_alive() -> None:
    with anyio.move_on_after(4) as scope:
        replies = await run_one_request_then_close_input(slow_server(3600), drain_timeout_s=0.2)
    assert not scope.cancelled_caught
    assert all(not isinstance(r.message, types.JSONRPCResponse) for r in replies)


@pytest.mark.parametrize("delay_s", [0, 0.05])
async def test_input_already_closed_with_nothing_pending(delay_s: float) -> None:
    client_send, server_read = anyio.create_memory_object_stream[SessionMessage | Exception](1)
    server_write, client_read = anyio.create_memory_object_stream[SessionMessage](1)
    await client_send.aclose()
    with anyio.fail_after(5):
        await serve_draining(slow_server(delay_s), server_read, server_write)
    assert [m async for m in client_read] == []


# --- replies to lines the SDK could not read --------------------------------


def unreadable(line: str) -> Exception:
    try:
        types.jsonrpc_message_adapter.validate_json(line, by_name=False)
    except ValidationError as exc:  # the same call, and the same exception, as the SDK's reader
        return exc
    raise AssertionError(f"line was readable: {line!r}")


@pytest.mark.parametrize(
    ("line", "code", "request_id"),
    [
        ("{not json", -32700, None),
        ('{"jsonrpc":"2.0","id":1,"method":"x","params":{"r":"a \\ud800 b"}}', -32700, None),
        ('[{"jsonrpc":"2.0","id":3,"method":"tools/list"}]', -32600, None),  # batch
        ('{"jsonrpc":"2.0","id":5}', -32600, 5),  # no method: id is recoverable
        ('{"jsonrpc":"1.0","id":"abc","method":"tools/list"}', -32600, "abc"),
        ('{"jsonrpc":"1.0","id":true,"method":"tools/list"}', -32600, None),  # bool is not an id
        ('"hello"', -32600, None),
    ],
)
def test_unreadable_lines_get_a_jsonrpc_error(
    line: str, code: int, request_id: str | int | None
) -> None:
    reply = error_reply_for(unreadable(line))
    assert reply is not None
    assert isinstance(reply.message, types.JSONRPCError)
    assert reply.message.error.code == code
    assert reply.message.id == request_id
    assert reply.message.error.data is None  # nothing from the input is echoed


@pytest.mark.parametrize("line", ["", "   ", "\n"])
def test_blank_lines_are_ignored(line: str) -> None:
    assert error_reply_for(unreadable(line)) is None


def test_other_exceptions_are_invalid_request() -> None:
    reply = error_reply_for(RuntimeError("transport hiccup"))
    assert reply is not None
    assert isinstance(reply.message, types.JSONRPCError)
    assert reply.message.error.code == -32600


# --- requests whose id is not a string or an integer ------------------------


@pytest.mark.parametrize("bad_id", ["null", '{"a": 1}', "[1]", "true", "false", "1.5"])
def test_request_with_invalid_id_is_swapped_for_an_invalid_message(bad_id: str) -> None:
    line = '{"jsonrpc":"2.0","id":' + bad_id + ',"method":"tools/list"}\n'
    assert screen_line(line) == INVALID_ID_PLACEHOLDER
    reply = error_reply_for(unreadable(INVALID_ID_PLACEHOLDER))
    assert reply is not None
    assert isinstance(reply.message, types.JSONRPCError)
    assert reply.message.error.code == -32600
    assert reply.message.id is None


@pytest.mark.parametrize(
    "line",
    [
        '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
        '{"jsonrpc":"2.0","id":"abc","method":"tools/list"}\n',
        '{"jsonrpc":"2.0","id":0,"method":"tools/list"}\n',
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n',  # no id: a notification
        '{"jsonrpc":"2.0","id":null,"result":{}}\n',  # a response, not a request
        '{"jsonrpc":"2.0","method":"x","params":{"note":"says \\"id\\" but has none"}}\n',
        '{not json, mentions "id"\n',  # left for the SDK, which reports a parse error
        '["id"]\n',
    ],
)
def test_other_lines_pass_through_unchanged(line: str) -> None:
    assert screen_line(line) == line
