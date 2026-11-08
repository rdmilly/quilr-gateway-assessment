"""The real server, as a separate process, over real pipes.

These are the tests behind the stdio claims: every byte on stdout is a JSON-RPC
message, logs land on stderr as JSON, a stray ``print`` cannot corrupt the
stream, every request gets an answer even if stdin closes straight away, and
the process exits cleanly when stdin closes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, MCPError, StdioServerParameters

ROOT = Path(__file__).resolve().parents[2]
SERVER = [sys.executable, "-m", "task1_mcp_server"]
TIMEOUT_S = 30

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
}
LEGACY_HANDSHAKE = [
    {
        "jsonrpc": "2.0",
        "id": "init",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
]


def call(request_id: int, name: str, arguments: Any, *, modern: bool) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name, "arguments": arguments}
    if modern:
        params["_meta"] = META
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}


def run(lines: list[Any], command: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    """Send ``lines`` on stdin, then close it straight away, like ``printf ... |``."""
    payload = "".join((ln if isinstance(ln, str) else json.dumps(ln)) + "\n" for ln in lines)
    return subprocess.run(  # noqa: S603 - fixed command, our own interpreter
        command or SERVER,
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=TIMEOUT_S,
        cwd=ROOT,
        check=False,
    )


def stdout_messages(proc: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
    """Parse stdout. Fails if any line, or any byte, is not a JSON-RPC message."""
    assert proc.stdout.endswith("\n") or proc.stdout == ""
    messages = []
    for line in proc.stdout.splitlines():
        message = json.loads(line)
        assert message["jsonrpc"] == "2.0"
        assert "id" in message
        assert ("result" in message) != ("error" in message)
        messages.append(message)
    return messages


def by_id(messages: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    return {m["id"]: m for m in messages}


@pytest.mark.parametrize("modern", [True, False], ids=["2026-07-28", "2025-11-25"])
def test_full_session_over_pipes(modern: bool) -> None:
    lines: list[Any] = [] if modern else list(LEGACY_HANDSHAKE)
    lines += [
        call(1, "get_customer_record", {"customer_id": "CUST-00042"}, modern=modern),
        call(2, "get_customer_record", {"customer_id": "CUST-4"}, modern=modern),
        call(3, "trigger_refund", {"customer_id": "CUST-00042", "amount": 0}, modern=modern),
        call(4, "no_such_tool", {}, modern=modern),
        call(5, "get_customer_record", {"customer_id": "CUST-99999"}, modern=modern),
        call(
            6,
            "trigger_refund",
            {"customer_id": "CUST-00042", "amount": 12.5, "reason": "Arrived broken, refund"},
            modern=modern,
        ),
    ]
    proc = run(lines)
    assert proc.returncode == 0, proc.stderr
    replies = by_id(stdout_messages(proc))
    assert set(replies) == {1, 2, 3, 4, 5, 6} | (set() if modern else {"init"})

    assert replies[1]["result"]["structuredContent"]["customer_id"] == "CUST-00042"
    assert replies[1]["result"]["isError"] is False
    for invalid in (2, 3, 4):
        assert replies[invalid]["error"]["code"] == -32602
    fields = {e["field"] for e in replies[3]["error"]["data"]["errors"]}
    assert fields == {"amount", "reason"}
    assert replies[5]["result"]["isError"] is True
    assert replies[6]["result"]["structuredContent"]["amount"] == "12.50"


def test_logs_go_to_stderr_as_json_lines() -> None:
    proc = run([call(1, "get_customer_record", {"customer_id": "CUST-00042"}, modern=True)])
    records = [json.loads(line) for line in proc.stderr.splitlines()]
    assert [r["msg"] for r in records] == ["server starting", "tool call", "server stopped"]
    assert all(r["level"] == "info" for r in records)


def test_unreadable_lines_get_errors_and_the_server_keeps_going() -> None:
    proc = run(
        [
            "{not json",
            "",
            json.dumps([call(1, "get_customer_record", {}, modern=True)]),  # a batch
            '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"trigger_refund",'
            '"arguments":{"customer_id":"CUST-00042","amount":5,"reason":"lone \\ud800 here"}}}',
            call(3, "get_customer_record", {"customer_id": "CUST-00042"}, modern=True),
        ]
    )
    assert proc.returncode == 0, proc.stderr
    messages = stdout_messages(proc)
    codes = [m["error"]["code"] for m in messages if "error" in m]
    assert codes == [-32700, -32600, -32700]
    assert all(m["id"] is None for m in messages if "error" in m)
    assert messages[-1]["id"] == 3
    assert "result" in messages[-1]


def test_stray_print_cannot_corrupt_stdout() -> None:
    """A tool that writes to stdout still leaves stdout as pure JSON-RPC.

    Covers both a Python ``print()`` (left unflushed, the case that would leak at
    exit) and a raw write to descriptor 1 (the case a C extension would hit).
    """
    script = textwrap.dedent(
        """
        import dataclasses
        import os
        from task1_mcp_server import tools
        from task1_mcp_server.server import main

        spec = tools.TOOLS_BY_NAME["get_customer_record"]
        original = spec.handler

        def noisy(args, store):
            print("DEBUG: this would break the protocol", end="")  # unflushed, no newline
            os.write(1, b"RAW: written straight to descriptor 1\\n")
            return original(args, store)

        tools.TOOLS_BY_NAME["get_customer_record"] = dataclasses.replace(spec, handler=noisy)
        main()
        """
    )
    proc = run(
        [call(1, "get_customer_record", {"customer_id": "CUST-00042"}, modern=True)],
        command=[sys.executable, "-c", script],
    )
    assert proc.returncode == 0, proc.stderr
    assert [m["id"] for m in stdout_messages(proc)] == [1]
    assert "DEBUG: this would break the protocol" in proc.stderr
    assert "RAW: written straight to descriptor 1" in proc.stderr


def test_importing_the_server_writes_nothing_to_stdout() -> None:
    proc = run(
        [],
        command=[sys.executable, "-c", "import task1_mcp_server.server, task1_mcp_server.tools"],
    )
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_exits_cleanly_when_stdin_closes_with_nothing_sent() -> None:
    proc = run([])
    assert proc.returncode == 0
    assert proc.stdout == ""


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_official_sdk_client_over_stdio(mode: str) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "task1_mcp_server"])
    async with Client(params, mode=mode) as client:
        listed = await client.list_tools()
        assert {t.name for t in listed.tools} == {"get_customer_record", "trigger_refund"}
        result = await client.call_tool("get_customer_record", {"customer_id": "CUST-10001"})
        assert result.structured_content is not None
        assert result.structured_content["name"] == "Grace Hopper"
        with pytest.raises(MCPError) as exc:
            await client.call_tool("trigger_refund", {"customer_id": "CUST-10001"})
        assert exc.value.error.code == -32602


@pytest.mark.parametrize("bad_id", [None, {"a": 1}, [1], True, 1.5])
def test_request_with_invalid_id_gets_invalid_request(bad_id: object) -> None:
    request = {
        "jsonrpc": "2.0",
        "id": bad_id,
        "method": "tools/list",
        "params": {"_meta": META},
    }
    proc = run(
        [request, call(2, "get_customer_record", {"customer_id": "CUST-00042"}, modern=True)]
    )
    assert proc.returncode == 0, proc.stderr
    messages = stdout_messages(proc)
    assert messages[0] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert messages[1]["id"] == 2
