"""The official MCP client through the gateway: both protocols, both response formats."""

from __future__ import annotations

import httpx2
import pytest
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client

from tests.task2.support import TOKENS, Rig

MODES = ["legacy", "2026-07-28"]


def client_for(rig: Rig, role: str, mode: str) -> Client:
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=rig.gateway_app),
        base_url="http://gateway",
        headers={"authorization": f"Bearer {TOKENS[role]}"},
    )
    return Client(streamable_http_client("http://gateway/mcp", http_client=http), mode=mode)


@pytest.mark.parametrize("mode", MODES)
async def test_viewer_sees_every_tool_and_can_call_ordinary_ones(rig: Rig, mode: str) -> None:
    async with client_for(rig, "viewer", mode) as client:
        tools = await client.list_tools()
        result = await client.call_tool("lookup_order", {"order_id": "A1"})
    assert [t.name for t in tools.tools] == ["lookup_order", "admin_reset_key"]
    assert not result.is_error


@pytest.mark.parametrize("mode", MODES)
async def test_viewer_gets_32001_from_the_sdk_client(rig: Rig, mode: str) -> None:
    async with client_for(rig, "viewer", mode) as client:
        with pytest.raises(MCPError) as exc:
            await client.call_tool("admin_reset_key", {"key_id": "k1"})
    assert exc.value.error.code == -32001
    assert exc.value.error.message == "Unauthorized Tool Call"
    assert exc.value.error.data is None


@pytest.mark.parametrize("mode", MODES)
async def test_admin_can_call_admin_tools(rig: Rig, mode: str) -> None:
    async with client_for(rig, "admin", mode) as client:
        result = await client.call_tool("admin_reset_key", {"key_id": "k1"})
    assert not result.is_error
