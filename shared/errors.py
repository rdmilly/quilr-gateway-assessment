"""Every error code and error shape in the project, in one place.

Two kinds of error leave this system:

* JSON-RPC errors, for MCP traffic (Tasks 1 and 2).
* The gateway error envelope, for the LLM endpoint (Tasks 3 and 4).

Neither kind ever carries a stack trace, a file path or an upstream response body.
The full detail goes to the log under the same request id, where only operators see it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

# JSON-RPC 2.0 standard codes.
PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603

# MCP 2026-07-28: an HTTP header is missing or disagrees with the request body.
HEADER_MISMATCH: Final = -32020

# Required by the assignment brief for a blocked tool call (Task 2). MCP 2026-07-28 asks new
# implementations not to use -32000 to -32019; we follow the brief and explain why in the README.
UNAUTHORIZED_TOOL_CALL: Final = -32001

type JsonRpcId = str | int | None


def jsonrpc_error(
    request_id: JsonRpcId, code: int, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a JSON-RPC error response.

    The caller's id is echoed exactly, including 0 and "", so the caller can match the answer
    to its question. `data` is only for safe, structured detail, such as which field failed.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def new_request_id() -> str:
    """A random id that ties a client-facing error to the matching log lines."""
    return f"req_{uuid.uuid4().hex}"


class GatewayErrorType(StrEnum):
    """Every kind of error the LLM gateway can return."""

    AUTHENTICATION_ERROR = "authentication_error"
    INVALID_REQUEST = "invalid_request"
    REQUEST_TOO_LARGE = "request_too_large"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL_ERROR = "internal_error"


_STATUS: Final[dict[GatewayErrorType, int]] = {
    GatewayErrorType.AUTHENTICATION_ERROR: 401,
    GatewayErrorType.INVALID_REQUEST: 400,
    # A request whose estimate alone is over the budget can never succeed, so it is a 400,
    # not a 429: a 429 tells the client to retry, and a retry would fail forever.
    GatewayErrorType.REQUEST_TOO_LARGE: 400,
    GatewayErrorType.RATE_LIMIT_EXCEEDED: 429,
    GatewayErrorType.UPSTREAM_UNAVAILABLE: 502,
    GatewayErrorType.UPSTREAM_TIMEOUT: 504,
    GatewayErrorType.SERVICE_UNAVAILABLE: 503,
    GatewayErrorType.INTERNAL_ERROR: 500,
}

_DEFAULT_MESSAGE: Final[dict[GatewayErrorType, str]] = {
    GatewayErrorType.AUTHENTICATION_ERROR: "Missing or invalid API key.",
    GatewayErrorType.INVALID_REQUEST: "The request is not valid.",
    GatewayErrorType.REQUEST_TOO_LARGE: "The request can never fit within the token budget.",
    GatewayErrorType.RATE_LIMIT_EXCEEDED: "Token budget exceeded. Retry later.",
    GatewayErrorType.UPSTREAM_UNAVAILABLE: "No model provider could answer the request.",
    GatewayErrorType.UPSTREAM_TIMEOUT: "The model providers did not answer in time.",
    GatewayErrorType.SERVICE_UNAVAILABLE: "The service is temporarily unavailable.",
    GatewayErrorType.INTERNAL_ERROR: "Internal error.",
}


@dataclass(eq=False)
class GatewayError(Exception):
    """An error the LLM gateway returns to a client.

    `message` must be safe, fixed text written by us. Never pass an exception's text or an
    upstream response into it.
    """

    kind: GatewayErrorType
    message: str | None = None
    retry_after_s: int | None = None

    def __str__(self) -> str:
        return self.kind.value

    @property
    def status(self) -> int:
        return _STATUS[self.kind]

    def body(self, request_id: str) -> dict[str, Any]:
        return {
            "error": {
                "type": self.kind.value,
                "code": self.status,
                "message": self.message or _DEFAULT_MESSAGE[self.kind],
                "request_id": request_id,
            }
        }

    def headers(self) -> dict[str, str]:
        if self.retry_after_s is None:
            return {}
        return {"Retry-After": str(max(1, self.retry_after_s))}
