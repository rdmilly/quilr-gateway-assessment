"""The gateway end to end over HTTP, against the real mock MCP server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx2
import pytest

from shared.audit import AuditLog, verify_chain
from shared.auth import Authenticator
from task2_mcp_gateway.app import create_app
from task2_mcp_gateway.settings import GatewaySettings
from tests.task2.support import ALLOWED_ORIGIN, Rig, call, list_tools, mcp_headers, post, reply

MODERN_LIST = {"mcp-protocol-version": "2026-07-28", "mcp-method": "tools/list"}

# --- who are you -------------------------------------------------------------


@pytest.mark.parametrize("token", [None, "wrong-token", "Basic dXNlcjpwYXNz"])
async def test_missing_or_bad_token_is_401_and_nothing_is_forwarded(
    rig: Rig, token: str | None
) -> None:
    headers = {"authorization": token} if token and token.startswith("Basic") else None
    response = await post(rig, list_tools(), as_role=None if headers else token, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="mcp-gateway"'
    assert response.json()["error"]["code"] == -31001
    assert rig.seen == []


# --- tools/list is forwarded untouched ----------------------------------------


@pytest.mark.parametrize("role", ["viewer", "admin"])
async def test_tools_list_is_forwarded_transparently(rig: Rig, role: str) -> None:
    response = await post(rig, list_tools(7), as_role=role)
    assert response.status_code == 200
    message = reply(response)
    assert message["id"] == 7
    names = [tool["name"] for tool in message["result"]["tools"]]
    assert names == ["lookup_order", "admin_reset_key"]  # nothing hidden, nothing reordered
    assert len(rig.seen) == 1


# --- tools/call policy ---------------------------------------------------------


async def test_viewer_can_call_an_ordinary_tool(rig: Rig) -> None:
    response = await post(rig, call(3, "lookup_order", {"order_id": "A1"}))
    message = reply(response)
    assert message["result"]["content"][0]["text"] == "lookup_order ran with {'order_id': 'A1'}"


async def test_viewer_calling_an_admin_tool_gets_32001_and_server_is_never_called(
    rig: Rig,
) -> None:
    response = await post(rig, call("abc", "admin_reset_key", {"key_id": "k1"}))
    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": "abc",
        "error": {"code": -32001, "message": "Unauthorized Tool Call"},
    }
    assert rig.seen == []


async def test_admin_can_call_an_admin_tool(rig: Rig) -> None:
    response = await post(rig, call(4, "admin_reset_key", {"key_id": "k1"}), as_role="admin")
    assert reply(response)["result"]["content"][0]["text"].startswith("admin_reset_key ran")


@pytest.mark.parametrize(
    "name",
    [
        "Admin_reset_key",
        "ADMIN_RESET_KEY",
        " admin_reset_key",
        "\u200badmin_reset_key",  # zero-width space in front
        "admin\u200b_reset_key",  # zero-width space inside
        "\uff41dmin_reset_key",  # full-width a
    ],
)
async def test_disguised_admin_tool_names_are_still_refused(rig: Rig, name: str) -> None:
    response = await post(rig, call(5, name))
    assert response.json()["error"]["code"] == -32001
    assert rig.seen == []


@pytest.mark.parametrize("name", ["administrator_report", "my_admin_tool", "lookup_order"])
async def test_names_that_only_look_similar_are_not_blocked(rig: Rig, name: str) -> None:
    response = await post(rig, call(6, name))
    assert response.status_code == 200
    assert "result" in reply(response)  # forwarded; the mock answers (unknown tools: isError)
    assert len(rig.seen) == 1


async def test_denied_tool_call_notification_is_refused_not_forwarded(rig: Rig) -> None:
    body = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "admin_reset_key"}}
    response = await post(rig, body)
    # MCP: a notification the server cannot accept gets an HTTP error status, never 202,
    # and the error carries no id.
    assert response.status_code == 403
    assert response.json() == {
        "jsonrpc": "2.0",
        "error": {"code": -32001, "message": "Unauthorized Tool Call"},
    }
    assert rig.seen == []


# --- what reaches the server ---------------------------------------------------


async def test_caller_token_is_not_passed_downstream(rig: Rig) -> None:
    await post(rig, list_tools(), as_role="viewer", headers={"cookie": "session=1"})
    (sent,) = rig.seen
    assert "authorization" not in sent
    assert "cookie" not in sent
    assert sent["x-gateway-subject"] == "victor"
    assert sent["x-gateway-role"] == "viewer"
    assert sent["x-request-id"].startswith("req_")


# --- one message, read strictly ------------------------------------------------


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"{not json", -32700),
        (b'{"jsonrpc":"2.0","id":1,"method":"tools/list","x":NaN}', -32700),
        # Gateway and server must not be able to read a different tool name:
        (
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            b'"params":{"name":"lookup_order","name":"admin_reset_key"}}',
            -32700,
        ),
        (b'[{"jsonrpc":"2.0","id":1,"method":"tools/list"}]', -32600),
        (b'{"jsonrpc":"2.0","id":null,"method":"tools/list"}', -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":["admin_reset_key"]}', -32600),
        (b'"just a string"', -32600),
    ],
)
async def test_unreadable_bodies_are_refused_before_policy(
    rig: Rig, body: bytes, code: int
) -> None:
    response = await post(rig, body, as_role="admin")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert rig.seen == []


async def test_header_that_disagrees_with_body_is_refused(rig: Rig) -> None:
    body = call(8, "admin_reset_key")
    response = await post(
        rig,
        body,
        as_role="admin",
        routing_headers=False,
        headers={
            "mcp-protocol-version": "2026-07-28",
            "mcp-method": "tools/call",
            "mcp-name": "lookup_order",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": -32020,
        "message": "Header mismatch",
        "data": {"detail": "Mcp-Name does not match the body"},
    }
    assert response.json()["id"] == 8
    assert rig.seen == []


async def test_method_header_that_disagrees_is_refused(rig: Rig) -> None:
    response = await post(
        rig, list_tools(), routing_headers=False, headers={"mcp-method": "tools/call"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020


# --- HTTP hygiene --------------------------------------------------------------


async def test_wrong_content_type_is_415(rig: Rig) -> None:
    response = await post(rig, list_tools(), headers={"content-type": "text/plain"})
    assert response.status_code == 415


async def test_oversized_body_is_413(rig: Rig) -> None:
    big = call(9, "lookup_order", {"order_id": "x" * 5000})
    response = await post(rig, big)
    assert response.status_code == 413
    assert rig.seen == []


async def test_unknown_origin_is_403_and_allowed_origin_passes(rig: Rig) -> None:
    refused = await post(rig, list_tools(), headers={"origin": "https://evil.example"})
    assert refused.status_code == 403
    assert "id" not in refused.json()  # MCP: this error carries no id
    allowed = await post(rig, list_tools(), headers={"origin": ALLOWED_ORIGIN})
    assert allowed.status_code == 200
    assert "origin" not in rig.seen[-1]


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_other_http_methods_are_405(rig: Rig, method: str) -> None:
    response = await rig.gateway.request(method, "/mcp")
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


# --- audit trail ---------------------------------------------------------------


async def test_every_tool_call_decision_is_audited(rig: Rig) -> None:
    await post(rig, call(1, "admin_reset_key", {"key_id": "secret-value"}))
    await post(rig, call(2, "lookup_order", {"order_id": "A1"}))
    await post(rig, list_tools())  # not a tool call: not audited
    records = rig.audit_records()
    assert [(r["unmapped"]["decision"], r["unmapped"]["target"]) for r in records] == [
        ("denied", "admin_reset_key"),
        ("allowed", "lookup_order"),
    ]
    assert records[0]["actor"]["user"]["uid"] == "victor"
    assert records[0]["action_id"] == 2  # OCSF: denied
    assert verify_chain(rig.audit.getvalue().splitlines()) is None
    assert "secret-value" not in rig.audit.getvalue()  # decisions, never arguments


# --- downstream failures -------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "status"),
    [(httpx2.ConnectError("refused"), 502), (httpx2.ReadTimeout("slow"), 504)],
)
async def test_downstream_failure_is_a_clean_error(
    authenticator: Authenticator, error: Exception, status: int
) -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise error

    failing = httpx2.AsyncClient(transport=httpx2.MockTransport(fail))
    app = create_app(GatewaySettings(), authenticator, client=failing, audit=AuditLog())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://gateway"
    ) as gateway:
        response = await gateway.post(
            "/mcp",
            content=json.dumps(list_tools(11)),
            headers={
                "authorization": "Bearer test-viewer-token",
                **MODERN_LIST,
                "content-type": "application/json",
            },
        )
    assert response.status_code == status
    body = response.json()
    assert body["id"] == 11
    assert body["error"]["code"] == -32603
    assert "refused" not in response.text
    assert "slow" not in response.text


async def test_declared_oversized_body_is_refused_before_reading(rig: Rig) -> None:
    response = await post(rig, list_tools(), headers={"content-length": "999999999"})
    assert response.status_code == 413


@pytest.mark.parametrize("url", ["ftp://127.0.0.1/mcp", "http:///mcp", "not a url"])
def test_downstream_url_must_be_http_with_a_host(url: str) -> None:
    with pytest.raises(ValueError, match="http or https URL"):
        GatewaySettings(downstream_url=url)


async def test_gateway_creates_and_closes_its_own_client(authenticator: Authenticator) -> None:
    # Nothing listens on port 9 (discard), so the forward fails cleanly with 502.
    app = create_app(
        GatewaySettings(downstream_url="http://127.0.0.1:9/mcp", upstream_timeout_s=2),
        authenticator,
        audit=AuditLog(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://gateway"
        ) as gateway,
    ):
        response = await gateway.post(
            "/mcp",
            content=json.dumps(list_tools(12)),
            headers={
                "authorization": "Bearer test-viewer-token",
                **MODERN_LIST,
                "content-type": "application/json",
            },
        )
    assert response.status_code in (502, 504)
    assert response.json()["id"] == 12


async def test_chunked_body_over_the_limit_is_cut_off(rig: Rig) -> None:
    async def chunks() -> AsyncIterator[bytes]:  # no Content-Length: the gateway must count
        for _ in range(10):
            yield b" " * 1000

    response = await rig.gateway.post(
        "/mcp",
        content=chunks(),
        headers={
            "authorization": "Bearer test-viewer-token",
            **MODERN_LIST,
            "content-type": "application/json",
        },
    )
    assert response.status_code == 413


async def test_injected_client_survives_the_app_lifespan(rig: Rig) -> None:
    async with rig.gateway_app.router.lifespan_context(rig.gateway_app):
        response = await post(rig, list_tools(13))
    assert reply(response)["id"] == 13


# --- 2026-07-28 routing headers are required -----------------------------------


@pytest.mark.parametrize(
    ("drop", "problem"),
    [
        ("mcp-protocol-version", "MCP-Protocol-Version header is missing"),
        ("mcp-method", "Mcp-Method header is missing"),
        ("mcp-name", "Mcp-Name header is missing"),
    ],
)
async def test_missing_routing_header_is_refused(rig: Rig, drop: str, problem: str) -> None:
    body = call(20, "lookup_order", {"order_id": "A1"})
    headers = mcp_headers(body)
    del headers[drop]
    response = await post(rig, body, routing_headers=False, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    assert response.json()["error"]["data"] == {"detail": problem}
    assert rig.seen == []


async def test_version_header_must_match_the_body(rig: Rig) -> None:
    response = await post(rig, list_tools(21), headers={"mcp-protocol-version": "2025-11-25"})
    assert response.status_code == 400
    assert response.json()["error"]["data"]["detail"] == (
        "MCP-Protocol-Version does not match the body"
    )


async def test_header_with_characters_that_need_encoding_is_refused(rig: Rig) -> None:
    body = call(22, "lookup_order")
    response = await post(rig, body, headers={"mcp-name": "lookup_order "})  # trailing space
    assert response.status_code == 400
    assert response.json()["error"]["data"]["detail"] == "Mcp-Name header has invalid characters"


async def test_encoded_name_header_is_accepted(rig: Rig) -> None:
    body = call(23, "lookup_order", {"order_id": "A1"})
    encoded = "=?base64?" + "bG9va3VwX29yZGVy" + "?="  # "lookup_order"
    response = await post(rig, body, headers={"mcp-name": encoded})
    assert response.status_code == 200
    assert "result" in reply(response)


async def test_legacy_requests_need_no_routing_headers(rig: Rig) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 24,
        "method": "tools/call",
        "params": {"name": "admin_reset_key"},
    }  # no _meta: a 2025-11-25 style request
    response = await post(rig, body, routing_headers=False)
    assert response.json()["error"]["code"] == -32001  # policy still applies


async def test_client_responses_are_refused_on_2026_07_28(rig: Rig) -> None:
    body = {"jsonrpc": "2.0", "id": 25, "result": {}}
    response = await post(
        rig, body, routing_headers=False, headers={"mcp-protocol-version": "2026-07-28"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600
    assert rig.seen == []


async def test_streamed_replies_tell_proxies_not_to_buffer(rig: Rig) -> None:
    response = await post(rig, list_tools(26))
    if response.headers["content-type"].startswith("text/event-stream"):
        assert response.headers["x-accel-buffering"] == "no"
    else:
        assert "x-accel-buffering" not in response.headers
