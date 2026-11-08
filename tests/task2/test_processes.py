"""The gateway and the mock server as real processes on real ports, as the README runs them."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from shared.auth import hash_token
from tests.task2.support import META, TOKENS

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(port: int, process: subprocess.Popen[bytes], timeout_s: float = 20) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"process exited early with code {process.returncode}")
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError(f"nothing listening on port {port}")


@pytest.fixture
def running(tmp_path: Path) -> Iterator[int]:
    mock_port, gateway_port = free_port(), free_port()
    config = tmp_path / "config.toml"
    config.write_text(
        "[auth]\ntokens = [\n"
        f'  {{ sha256 = "{hash_token(TOKENS["admin"])}", subject = "alice", role = "admin" }},\n'
        f'  {{ sha256 = "{hash_token(TOKENS["viewer"])}", subject = "victor", role = "viewer" }},\n'
        "]\n\n[mcp_gateway]\n"
        f'downstream_url = "http://127.0.0.1:{mock_port}/mcp"\nport = {gateway_port}\n',
        encoding="utf-8",
    )
    env = {**os.environ, "MOCK_MCP_PORT": str(mock_port), "GATEWAY_CONFIG": str(config)}
    processes = [
        subprocess.Popen(  # noqa: S603 - fixed command, our own interpreter
            [sys.executable, "-m", module],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for module in ("mocks.mcp_server", "task2_mcp_gateway")
    ]
    try:
        wait_for_port(mock_port, processes[0])
        wait_for_port(gateway_port, processes[1])
        yield gateway_port
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            process.wait(timeout=20)


def tool_call(name: str, role: str, port: int) -> dict[str, object]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": {}, "_meta": META},
    }
    response = httpx2.post(
        f"http://127.0.0.1:{port}/mcp",
        content=json.dumps(body),
        headers={
            "authorization": f"Bearer {TOKENS[role]}",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2026-07-28",
            "mcp-method": "tools/call",
            "mcp-name": name,
        },
        timeout=20,
    )
    assert response.status_code == 200
    data = [ln[5:].strip() for ln in response.text.splitlines() if ln.startswith("data:")]
    return dict(json.loads(data[-1] if data else response.text))


def test_gateway_and_mock_server_run_as_real_processes(running: int) -> None:
    assert tool_call("admin_reset_key", "viewer", running)["error"] == {
        "code": -32001,
        "message": "Unauthorized Tool Call",
    }
    allowed = tool_call("admin_reset_key", "admin", running)
    assert "result" in allowed
