"""The parser, the policy and the header check on their own."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shared.auth import Identity, Role
from task2_mcp_gateway.jsonrpc import Kind, ParseProblem, parse_message
from task2_mcp_gateway.mcp_headers import decode_header_value, header_problem
from task2_mcp_gateway.policy import decide, is_admin_tool, is_valid_tool_name

VIEWER = Identity(subject="victor", role=Role.VIEWER)
ADMIN = Identity(subject="alice", role=Role.ADMIN)


# --- parser ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        (b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}', Kind.REQUEST),
        (b'{"jsonrpc":"2.0","id":"a","method":"tools/call","params":{"name":"x"}}', Kind.REQUEST),
        (b'{"jsonrpc":"2.0","method":"notifications/initialized"}', Kind.NOTIFICATION),
        (b'{"jsonrpc":"2.0","id":3,"result":{}}', Kind.RESPONSE),
        (b'{"jsonrpc":"2.0","id":3,"error":{"code":1,"message":"m"}}', Kind.RESPONSE),
    ],
)
def test_valid_messages(raw: bytes, kind: Kind) -> None:
    assert parse_message(raw).kind is kind


@pytest.mark.parametrize(
    ("raw", "code", "request_id"),
    [
        (b"", -32700, None),
        (b"\xff\xfe", -32700, None),  # not UTF-8
        (b'{"a":1,"a":2}', -32700, None),
        (b'{"jsonrpc":"2.0","id":1,"method":"m","params":{"x":Infinity}}', -32700, None),
        (b'{"jsonrpc":"2.0","id":1,"method":"m","params":{"x":1e999}}', -32700, None),
        (b'{"jsonrpc":"2.0","id":1,"method":"m","params":{"x":"\\ud800"}}', -32700, None),
        (b"[" * 100_000, -32700, None),  # nesting deep enough to break a recursive parser
        (b"[]", -32600, None),
        (b"42", -32600, None),
        (b'{"jsonrpc":"1.0","id":5,"method":"m"}', -32600, 5),
        (b'{"id":5,"method":"m"}', -32600, 5),
        (b'{"jsonrpc":"2.0","id":5,"method":""}', -32600, 5),
        (b'{"jsonrpc":"2.0","id":5,"method":7}', -32600, 5),
        (b'{"jsonrpc":"2.0","id":true,"method":"m"}', -32600, None),
        (b'{"jsonrpc":"2.0","id":1.5,"method":"m"}', -32600, None),
        (b'{"jsonrpc":"2.0","id":5,"method":"m","params":[1]}', -32600, 5),
        (b'{"jsonrpc":"2.0","id":5}', -32600, 5),
        (b'{"jsonrpc":"2.0","id":5,"result":{},"error":{}}', -32600, 5),
    ],
)
def test_invalid_messages(raw: bytes, code: int, request_id: object) -> None:
    with pytest.raises(ParseProblem) as exc:
        parse_message(raw)
    assert exc.value.code == code
    assert exc.value.request_id == request_id


def test_forwarded_bytes_are_the_parsed_message() -> None:
    message = parse_message(b'{ "jsonrpc" : "2.0", "id" : 1, "method" : "tools/list" }')
    assert message.to_bytes() == b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'


# --- policy ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "admin"),
    [
        ("admin_reset_key", True),
        ("Admin_Reset_Key", True),
        ("ADMIN_X", True),
        ("administrator", False),
        ("admin", False),
        ("my_admin_tool", False),
        ("lookup_order", False),
    ],
)
def test_admin_tool_detection(name: str, admin: bool) -> None:
    assert is_admin_tool(name) is admin


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("lookup_order", True),
        ("user-profile/update", True),
        ("admin.tools.list", True),
        ("DATA_EXPORT_v2", True),
        ("x" * 128, True),
        ("x" * 129, False),
        ("", False),
        ("has space", False),
        ("comma,name", False),
        ("caf\u00e9", False),
        ("\u0430dmin_x", False),  # Cyrillic a
        ("ad\u00admin_x", False),  # soft hyphen
        ("admin_x\n", False),
    ],
)
def test_tool_name_character_set(name: str, valid: bool) -> None:
    assert is_valid_tool_name(name) is valid


@given(st.from_regex(r"\A[A-Za-z0-9_\-./]{0,30}\Z"))
def test_anything_after_admin_prefix_is_admin(suffix: str) -> None:
    assert is_admin_tool("admin_" + suffix)
    assert is_admin_tool("ADMIN_" + suffix)


@given(st.text(max_size=40))
def test_every_valid_name_is_plain_ascii(name: str) -> None:
    if is_valid_tool_name(name):
        assert name.isascii()
        assert name == name.strip()


def _call(name: object) -> bytes:
    import json

    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}
    ).encode()


def test_decide() -> None:
    assert not decide(VIEWER, parse_message(_call("admin_reset_key"))).allowed
    assert decide(ADMIN, parse_message(_call("admin_reset_key"))).allowed
    assert decide(VIEWER, parse_message(_call("lookup_order"))).allowed
    assert decide(VIEWER, parse_message(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')).allowed
    # A call with no usable name is left for the server to reject with -32602.
    assert decide(VIEWER, parse_message(_call(["admin_reset_key"]))).allowed
    refused = decide(ADMIN, parse_message(_call("bad name")))
    assert not refused.allowed
    assert refused.code == -32602


# --- 2026-07-28 routing headers -------------------------------------------------


def test_encoded_header_values() -> None:
    assert decode_header_value("lookup_order") == "lookup_order"
    assert decode_header_value("=?base64?bG9va3VwX8O2cmTDqXI=?=") == "lookup_ördér"
    assert decode_header_value("=?base64?not base64!?=") is None
    assert decode_header_value("has space inside") == "has space inside"
    assert decode_header_value(" leading") is None
    assert decode_header_value("") is None


@pytest.mark.parametrize(
    ("headers", "problem"),
    [
        ({}, None),
        ({"mcp-method": "tools/call", "mcp-name": "lookup_order"}, None),
        ({"mcp-method": "tools/list"}, "Mcp-Method does not match the body"),
        ({"mcp-name": "admin_reset_key"}, "Mcp-Name does not match the body"),
        ({"mcp-name": "=?base64?bG9va3VwX29yZGVy?="}, None),  # "lookup_order", encoded
        ({"mcp-name": "=?base64?%%%?="}, "Mcp-Name header has invalid characters"),
        ({"mcp-name": " lookup_order"}, "Mcp-Name header has invalid characters"),
        ({"mcp-method": "tools/call\t"}, "Mcp-Method header has invalid characters"),
    ],
)
def test_header_problem(headers: dict[str, str], problem: str | None) -> None:
    assert header_problem(headers, parse_message(_call("lookup_order"))) == problem


def test_name_header_on_a_method_without_a_name_is_a_mismatch() -> None:
    message = parse_message(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
    assert header_problem({"mcp-name": "x"}, message) == "Mcp-Name does not match the body"


def test_modern_request_needs_every_routing_header() -> None:
    import json

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "x", "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
    }
    message = parse_message(json.dumps(body).encode())
    full = {"mcp-protocol-version": "2026-07-28", "mcp-method": "tools/call", "mcp-name": "x"}
    assert header_problem(full, message) is None
    assert header_problem({}, message) == "MCP-Protocol-Version header is missing"
