"""Who may call which tool.

Two rules, checked in this order:

1. **The name must be a valid MCP tool name.** SEP-986 (Final) says tool names
   use only ASCII letters, digits, ``_``, ``-``, ``.`` and ``/``. A name with
   anything else (a Cyrillic letter that looks Latin, a full-width letter, a
   zero-width space, a leading space) is refused with ``-32602``. That one rule
   closes every look-alike trick, because the admin check below only ever sees
   plain ASCII.
2. **``admin_`` tools need the admin role.** The brief's rule. The check ignores
   case, so ``Admin_reset_key`` is treated as an admin tool too. MCP tool names
   are case-sensitive, so this can over-block a tool named, say,
   ``Admin_report``; that is deliberate, in case a server matches names without
   regard to case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from shared.auth import Identity, Role
from shared.errors import INVALID_PARAMS, UNAUTHORIZED_TOOL_CALL

from .jsonrpc import Message

ADMIN_PREFIX = "admin_"
POLICY_ID = "mcp.admin-tools.v1"
# SEP-986 suggests at most 64 characters; 128 leaves room for longer namespaced names.
TOOL_NAME = re.compile(r"[A-Za-z0-9_\-./]{1,128}")


def is_valid_tool_name(name: str) -> bool:
    return TOOL_NAME.fullmatch(name) is not None


def is_admin_tool(name: str) -> bool:
    return name.lower().startswith(ADMIN_PREFIX)


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    reason: str
    tool: str | None = None
    # The JSON-RPC error to send when refused; ignored when allowed.
    code: int = UNAUTHORIZED_TOOL_CALL
    message: str = "Unauthorized Tool Call"


def decide(identity: Identity, message: Message) -> Verdict:
    """Apply the tool policy to one parsed message."""
    if message.method != "tools/call":
        return Verdict(True, "not a tool call")
    name = message.params.get("name")
    if not isinstance(name, str):
        # Malformed call: the server rejects it with its own -32602. Nothing to protect here.
        return Verdict(True, "tool call without a string name")
    if not is_valid_tool_name(name):
        return Verdict(
            False,
            "tool name outside the MCP character set",
            name[:128],
            INVALID_PARAMS,
            "Invalid params: tool names use only A-Z a-z 0-9 _ - . /",
        )
    if is_admin_tool(name) and identity.role is not Role.ADMIN:
        return Verdict(False, "admin tool requires the admin role", name)
    return Verdict(True, "allowed by role", name)
