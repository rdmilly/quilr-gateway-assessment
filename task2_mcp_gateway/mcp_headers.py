"""Check the MCP routing headers against the body.

MCP 2026-07-28 copies the protocol version, the method and the tool name into
HTTP headers (``MCP-Protocol-Version``, ``Mcp-Method``, ``Mcp-Name``) so
infrastructure can route without reading the body. If a header says one thing
and the body another, something in the path could act on the header while the
server acts on the body. So for a 2026-07-28 request the spec requires:

* all three headers present (``Mcp-Name`` for tools/call, prompts/get and
  resources/read),
* each one equal to the body,
* plain values made only of visible ASCII; anything else travels as
  ``=?base64?<data>?=`` and is decoded before comparing.

A failure is HTTP 400 with ``-32020 HeaderMismatch``. Older protocol versions
did not define these headers, so for them only headers that are present are
checked. The gateway's own policy decision is always made on the parsed body.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping

from .jsonrpc import Kind, Message

PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
LEGACY_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"})

_ENCODED_PREFIX = "=?base64?"
_ENCODED_SUFFIX = "?="

# Which params field Mcp-Name mirrors, per method.
_NAME_FIELD = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}


def _plain_value_ok(value: str) -> bool:
    """Visible ASCII, with spaces or tabs allowed only inside the value (RFC 9110)."""
    return (
        value != ""
        and value == value.strip(" \t")
        and all(ch in " \t" or 0x21 <= ord(ch) <= 0x7E for ch in value)
    )


def decode_header_value(value: str) -> str | None:
    """The header's text, or None if it is neither a valid plain value nor valid base64."""
    if value.startswith(_ENCODED_PREFIX) and value.endswith(_ENCODED_SUFFIX):
        encoded = value[len(_ENCODED_PREFIX) : -len(_ENCODED_SUFFIX)]
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
    return value if _plain_value_ok(value) else None


def body_protocol_version(message: Message) -> str | None:
    meta = message.params.get("_meta")
    version = meta.get(PROTOCOL_VERSION_KEY) if isinstance(meta, dict) else None
    return version if isinstance(version, str) else None


def header_problem(headers: Mapping[str, str], message: Message) -> str | None:
    """What is wrong with the routing headers, or None if they agree with the body."""
    body_version = body_protocol_version(message)
    # Per-request metadata in the body marks a 2026-07-28 (or later) request.
    modern = message.kind is Kind.REQUEST and body_version is not None

    version_header = headers.get("mcp-protocol-version")
    if modern:
        if version_header is None:
            return "MCP-Protocol-Version header is missing"
        if version_header != body_version:
            return "MCP-Protocol-Version does not match the body"

    method_header = headers.get("mcp-method")
    if method_header is None:
        if modern:
            return "Mcp-Method header is missing"
    elif not _plain_value_ok(method_header):
        return "Mcp-Method header has invalid characters"
    elif method_header != message.method:
        return "Mcp-Method does not match the body"

    field = _NAME_FIELD.get(message.method or "")
    name_header = headers.get("mcp-name")
    if name_header is None:
        if modern and field is not None:
            return "Mcp-Name header is missing"
        return None
    decoded = decode_header_value(name_header)
    if decoded is None:
        return "Mcp-Name header has invalid characters"
    expected = message.params.get(field) if field is not None else None
    if decoded != expected:
        return "Mcp-Name does not match the body"
    return None
