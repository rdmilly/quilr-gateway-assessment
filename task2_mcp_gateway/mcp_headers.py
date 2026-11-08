"""Check the 2026-07-28 routing headers against the body.

MCP 2026-07-28 puts the method and the tool name in HTTP headers (``Mcp-Method``,
``Mcp-Name``) so infrastructure can route without reading the body. If a header
says one thing and the body another, something in the path could act on the
header while the server acts on the body. The spec says such a request MUST be
refused with HTTP 400 and ``-32020 HeaderMismatch``.

Non-ASCII names travel as ``=?base64?<data>?=``; that form is decoded before
comparing. The gateway only checks headers that are present: its own decision
is always made on the parsed body.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping

from .jsonrpc import Message

_ENCODED_PREFIX = "=?base64?"
_ENCODED_SUFFIX = "?="

# Which params field Mcp-Name mirrors, per method.
_NAME_FIELD = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}


def decode_header_value(value: str) -> str | None:
    """The header's text, or None if it claims to be encoded but is not valid."""
    if value.startswith(_ENCODED_PREFIX) and value.endswith(_ENCODED_SUFFIX):
        encoded = value[len(_ENCODED_PREFIX) : -len(_ENCODED_SUFFIX)]
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
    return value


def header_mismatch(headers: Mapping[str, str], message: Message) -> str | None:
    """A short description of the first mismatch, or None if the headers agree with the body."""
    method_header = headers.get("mcp-method")
    if method_header is not None and method_header != message.method:
        return "Mcp-Method does not match the body"
    name_header = headers.get("mcp-name")
    if name_header is not None:
        field = _NAME_FIELD.get(message.method or "")
        expected = message.params.get(field) if field else None
        if decode_header_value(name_header) != expected:
            return "Mcp-Name does not match the body"
    return None
