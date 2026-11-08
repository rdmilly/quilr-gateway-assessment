"""Shared helpers for the Task 2 tests: tokens, request builders, reply parsing."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any

import httpx2

TOKENS = {"admin": "test-admin-token", "viewer": "test-viewer-token"}
ALLOWED_ORIGIN = "https://app.example"
META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
}


@dataclass
class Rig:
    gateway: httpx2.AsyncClient
    gateway_app: Any
    seen: list[dict[str, str]]  # headers of every request that reached the mock server
    audit: io.StringIO

    def audit_records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.audit.getvalue().splitlines()]


def call(request_id: Any, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}, "_meta": META},
    }


def list_tools(request_id: Any = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": {"_meta": META}}


def mcp_headers(body: Any) -> dict[str, str]:
    """The 2026-07-28 routing headers a real client sends for this body."""
    headers = {"mcp-protocol-version": "2026-07-28"}
    if isinstance(body, dict) and isinstance(body.get("method"), str):
        headers["mcp-method"] = body["method"]
        name = body.get("params", {}).get("name")
        if body["method"] == "tools/call" and isinstance(name, str) and name.isascii():
            headers["mcp-name"] = name
    return headers


async def post(
    rig: Rig,
    body: Any,
    *,
    as_role: str | None = "viewer",
    headers: dict[str, str] | None = None,
    routing_headers: bool = True,
) -> httpx2.Response:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    sent = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    if as_role is not None:  # a role name from TOKENS, or any other string to send as-is
        sent["authorization"] = f"Bearer {TOKENS.get(as_role, as_role)}"
    if routing_headers and not isinstance(body, bytes):
        sent.update(mcp_headers(body))
    sent.update(headers or {})
    return await rig.gateway.post("/mcp", content=content, headers=sent)


def reply(response: httpx2.Response) -> dict[str, Any]:
    """The JSON-RPC message in a response, whether it came as JSON or as SSE."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = [
            line[len("data:") :].strip()
            for line in response.text.splitlines()
            if line.startswith("data:") and line[len("data:") :].strip()
        ]
        return dict(json.loads(data[-1]))
    return dict(response.json())
