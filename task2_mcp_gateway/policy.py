"""Who may call which tool.

The rule from the brief: a tool whose name starts with ``admin_`` may only be
called by the admin role. Anything else is allowed.

The check runs on a canonical form of the name, so a caller cannot dodge it
with ``Admin_reset_key``, a full-width a (U+FF41), a zero-width space, or
leading spaces. A server would not run a tool under those spellings anyway,
but the gateway should not be the part that relies on that.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from shared.auth import Identity, Role

from .jsonrpc import Message

ADMIN_PREFIX = "admin_"
POLICY_ID = "mcp.admin-tools.v1"


def canonical_tool_name(name: str) -> str:
    """NFKC-normalized, invisible (format) characters removed, trimmed, case-folded."""
    folded = unicodedata.normalize("NFKC", name)
    visible = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")
    return visible.strip().casefold()


def is_admin_tool(name: str) -> bool:
    return canonical_tool_name(name).startswith(ADMIN_PREFIX)


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    reason: str
    tool: str | None = None


def decide(identity: Identity, message: Message) -> Verdict:
    """Apply the tool policy to one parsed message."""
    if message.method != "tools/call":
        return Verdict(True, "not a tool call")
    name = message.params.get("name")
    if not isinstance(name, str):
        # Malformed call: let the server reject it with its own -32602. Nothing to protect here.
        return Verdict(True, "tool call without a string name", None)
    if is_admin_tool(name) and identity.role is not Role.ADMIN:
        return Verdict(False, "admin tool requires the admin role", name)
    return Verdict(True, "allowed by role", name)
