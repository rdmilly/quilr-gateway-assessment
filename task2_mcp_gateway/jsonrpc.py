"""Read one JSON-RPC message from a request body, strictly, exactly once.

The gateway makes its security decision on what this module parses, then
forwards *that* parse, re-serialized, rather than the original bytes. If the
gateway and the server could read the same bytes differently, a caller could
show the gateway one tool name and the server another. So anything two parsers
might disagree on is refused here:

* duplicate keys (``{"name": "safe", "name": "admin_reset_key"}``),
* batches (a JSON array of messages; MCP's schema defines single messages only),
* ``NaN``, ``Infinity`` and numbers too large for a float,
* lone surrogates such as ``"\\ud800"``, which the MCP SDK refuses to parse,
* invalid UTF-8.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from shared.errors import INVALID_REQUEST, PARSE_ERROR, JsonRpcId


class Kind(StrEnum):
    REQUEST = "request"  # has an id and a method; expects a reply
    NOTIFICATION = "notification"  # has a method, no id; no reply
    RESPONSE = "response"  # has an id and a result or error (client answering the server)


@dataclass(frozen=True, slots=True)
class Message:
    kind: Kind
    body: dict[str, Any]

    @property
    def id(self) -> JsonRpcId:
        value = self.body.get("id")
        return value if isinstance(value, str | int) else None

    @property
    def method(self) -> str | None:
        method = self.body.get("method")
        return method if isinstance(method, str) else None

    @property
    def params(self) -> dict[str, Any]:
        params = self.body.get("params")
        return params if isinstance(params, dict) else {}

    def to_bytes(self) -> bytes:
        """The parsed message, serialized again. This, not the original body, is forwarded."""
        return json.dumps(self.body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class ParseProblem(Exception):
    """The body is not one readable JSON-RPC message. Carries the JSON-RPC error to send."""

    def __init__(self, code: int, message: str, request_id: JsonRpcId = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("number out of range")
    return value


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON")


def _valid_id(value: object) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def parse_message(raw: bytes) -> Message:
    try:
        text = raw.decode("utf-8")
        data = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
        )
        # Serializing once proves it can be forwarded: this fails on a lone surrogate.
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (UnicodeError, ValueError, RecursionError):
        raise ParseProblem(PARSE_ERROR, "Parse error") from None

    if isinstance(data, list):
        raise ParseProblem(INVALID_REQUEST, "Invalid Request: batches are not supported")
    if not isinstance(data, dict):
        raise ParseProblem(INVALID_REQUEST, "Invalid Request")

    request_id = data["id"] if _valid_id(data.get("id")) else None
    if data.get("jsonrpc") != "2.0":
        raise ParseProblem(INVALID_REQUEST, "Invalid Request", request_id)
    if "params" in data and not isinstance(data["params"], dict):
        # MCP params are always an object; JSON-RPC's by-position arrays are not used.
        raise ParseProblem(INVALID_REQUEST, "Invalid Request: params must be an object", request_id)

    if "method" in data:
        if not isinstance(data["method"], str) or not data["method"]:
            raise ParseProblem(INVALID_REQUEST, "Invalid Request", request_id)
        if "id" not in data:
            return Message(Kind.NOTIFICATION, data)
        if not _valid_id(data["id"]):
            # MCP: a request id is a string or an integer and never null.
            raise ParseProblem(INVALID_REQUEST, "Invalid Request: id must be a string or integer")
        return Message(Kind.REQUEST, data)

    if _valid_id(data.get("id")) and (("result" in data) != ("error" in data)):
        return Message(Kind.RESPONSE, data)
    raise ParseProblem(INVALID_REQUEST, "Invalid Request", request_id)
