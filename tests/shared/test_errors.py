"""Errors: one place for every code, the caller's id echoed exactly, nothing internal leaks."""

import pytest

from shared import errors
from shared.errors import GatewayError, GatewayErrorType, jsonrpc_error


@pytest.mark.parametrize("request_id", [0, 1, "", "abc", None])
def test_jsonrpc_error_echoes_the_callers_id_exactly(request_id: str | int | None) -> None:
    body = jsonrpc_error(request_id, errors.INVALID_PARAMS, "Invalid params")
    assert body["id"] == request_id
    assert type(body["id"]) is type(request_id)
    assert body["jsonrpc"] == "2.0"
    assert "result" not in body


def test_jsonrpc_error_includes_data_only_when_given() -> None:
    assert "data" not in jsonrpc_error(1, errors.INVALID_PARAMS, "Invalid params")["error"]
    with_data = jsonrpc_error(1, errors.INVALID_PARAMS, "Invalid params", {"field": "amount"})
    assert with_data["error"]["data"] == {"field": "amount"}


def test_codes_match_json_rpc_mcp_and_the_brief() -> None:
    standard = (
        errors.PARSE_ERROR,
        errors.INVALID_REQUEST,
        errors.METHOD_NOT_FOUND,
        errors.INVALID_PARAMS,
        errors.INTERNAL_ERROR,
    )
    assert standard == (-32700, -32600, -32601, -32602, -32603)
    assert errors.HEADER_MISMATCH == -32020
    assert errors.UNAUTHORIZED_TOOL_CALL == -32001


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (GatewayErrorType.AUTHENTICATION_ERROR, 401),
        (GatewayErrorType.INVALID_REQUEST, 400),
        (GatewayErrorType.REQUEST_TOO_LARGE, 400),
        (GatewayErrorType.RATE_LIMIT_EXCEEDED, 429),
        (GatewayErrorType.UPSTREAM_UNAVAILABLE, 502),
        (GatewayErrorType.UPSTREAM_TIMEOUT, 504),
        (GatewayErrorType.SERVICE_UNAVAILABLE, 503),
        (GatewayErrorType.INTERNAL_ERROR, 500),
    ],
)
def test_every_gateway_error_has_a_fixed_status(kind: GatewayErrorType, status: int) -> None:
    assert GatewayError(kind).status == status


def test_A4_9_gateway_error_body_holds_only_safe_fixed_fields() -> None:
    body = GatewayError(GatewayErrorType.INTERNAL_ERROR).body("req_1")
    assert body == {
        "error": {
            "type": "internal_error",
            "code": 500,
            "message": "Internal error.",
            "request_id": "req_1",
        }
    }


def test_a_request_that_can_never_fit_is_not_told_to_retry() -> None:
    error = GatewayError(GatewayErrorType.REQUEST_TOO_LARGE)
    assert error.status == 400
    assert error.headers() == {}


def test_retry_after_is_sent_only_when_set_and_never_below_one_second() -> None:
    assert GatewayError(GatewayErrorType.RATE_LIMIT_EXCEEDED).headers() == {}
    limited = GatewayError(GatewayErrorType.RATE_LIMIT_EXCEEDED, retry_after_s=12)
    assert limited.headers() == {"Retry-After": "12"}
    assert GatewayError(GatewayErrorType.RATE_LIMIT_EXCEEDED, retry_after_s=0).headers() == {
        "Retry-After": "1"
    }


def test_gateway_error_can_be_raised_and_caught() -> None:
    with pytest.raises(GatewayError) as caught:
        raise GatewayError(GatewayErrorType.UPSTREAM_TIMEOUT)
    assert caught.value.status == 504


def test_request_ids_are_unique_and_prefixed() -> None:
    ids = {errors.new_request_id() for _ in range(1000)}
    assert len(ids) == 1000
    assert all(i.startswith("req_") for i in ids)
