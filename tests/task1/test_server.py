"""The server through the official SDK client, in memory, on both protocol versions.

``legacy`` is the 2025-11-25 protocol (initialize handshake), which most clients
still speak; ``2026-07-28`` is the current stateless protocol. The server
answers both, so every test runs on each.
"""

import pytest
from mcp import Client, MCPError

from task1_mcp_server.server import build_server

PROTOCOLS = {"legacy": "2025-11-25", "2026-07-28": "2026-07-28"}


@pytest.fixture(params=sorted(PROTOCOLS))
def mode(request: pytest.FixtureRequest) -> str:
    return str(request.param)


async def test_negotiates_the_expected_protocol(mode: str) -> None:
    async with Client(build_server(), mode=mode) as client:
        await client.list_tools()
        assert client.protocol_version == PROTOCOLS[mode]


async def test_lists_both_tools_with_schemas(mode: str) -> None:
    async with Client(build_server(), mode=mode) as client:
        result = await client.list_tools()
    tools = {tool.name: tool for tool in result.tools}
    assert set(tools) == {"get_customer_record", "trigger_refund"}
    refund_schema = tools["trigger_refund"].input_schema
    assert refund_schema["additionalProperties"] is False
    assert set(refund_schema["required"]) == {"customer_id", "amount", "reason"}


async def test_valid_call_returns_structured_content(mode: str) -> None:
    async with Client(build_server(), mode=mode) as client:
        result = await client.call_tool("get_customer_record", {"customer_id": "CUST-00042"})
    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["customer_id"] == "CUST-00042"


async def test_refund_round_trip(mode: str) -> None:
    async with Client(build_server(), mode=mode) as client:
        result = await client.call_tool(
            "trigger_refund",
            {"customer_id": "CUST-00042", "amount": 49.99, "reason": "Item arrived damaged"},
        )
        after = await client.call_tool("get_customer_record", {"customer_id": "CUST-00042"})
    assert not result.is_error
    assert after.structured_content is not None
    assert after.structured_content["refundable_balance"] == "200.01"


@pytest.mark.parametrize(
    ("tool", "arguments", "bad_fields"),
    [
        ("get_customer_record", {"customer_id": "CUST-123"}, {"customer_id"}),
        ("get_customer_record", {}, {"customer_id"}),
        (
            "trigger_refund",
            {"customer_id": "CUST-00042", "amount": -1, "reason": "x"},
            {"amount", "reason"},
        ),
        (
            "trigger_refund",
            {"customer_id": "CUST-00042", "amount": "10", "reason": "Long enough reason"},
            {"amount"},
        ),
    ],
)
async def test_invalid_arguments_are_jsonrpc_invalid_params(
    mode: str, tool: str, arguments: dict[str, object], bad_fields: set[str]
) -> None:
    async with Client(build_server(), mode=mode) as client:
        with pytest.raises(MCPError) as exc:
            await client.call_tool(tool, arguments)
    assert exc.value.error.code == -32602
    assert exc.value.error.message == "Invalid params"
    assert {e["field"] for e in exc.value.error.data["errors"]} == bad_fields


async def test_unknown_tool_is_invalid_params(mode: str) -> None:
    async with Client(build_server(), mode=mode) as client:
        with pytest.raises(MCPError) as exc:
            await client.call_tool("admin_delete_customer", {})
    assert exc.value.error.code == -32602


async def test_business_failure_is_a_tool_error(mode: str) -> None:
    async with Client(build_server(), mode=mode) as client:
        result = await client.call_tool("get_customer_record", {"customer_id": "CUST-99999"})
    assert result.is_error
