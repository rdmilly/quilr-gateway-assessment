"""The endpoint and the mock provider as real processes on real sockets.

Time to first token is measured here and not in memory: the in-memory test
transport collects a whole response before returning it, which would hide
streaming entirely.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

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
def gateway_url(tmp_path: Path) -> Iterator[str]:
    provider_port, gateway_port = free_port(), free_port()
    config = tmp_path / "config.toml"
    config.write_text(
        "[llm_gateway]\n"
        f'provider_url = "http://127.0.0.1:{provider_port}/v1/chat/completions"\n'
        f"port = {gateway_port}\n",
        encoding="utf-8",
    )
    env = {**os.environ, "MOCK_LLM_PORT": str(provider_port), "GATEWAY_CONFIG": str(config)}
    processes = [
        subprocess.Popen(  # noqa: S603 - fixed command, our own interpreter
            [sys.executable, "-m", module],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for module in ("mocks.llm_provider", "task3_pii_guardrail")
    ]
    try:
        wait_for_port(provider_port, processes[0])
        wait_for_port(gateway_port, processes[1])
        yield f"http://127.0.0.1:{gateway_port}/v1/chat/completions"
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            process.wait(timeout=20)


def test_first_text_arrives_long_before_the_provider_finishes(gateway_url: str) -> None:
    body = {
        "model": "mock-echo",
        "stream": True,
        "mock": {"chunk_chars": 12, "delay_ms": 40},
        "messages": [{"role": "user", "content": "plain words " * 20}],
    }
    started = time.perf_counter()
    first_text_at = None
    with httpx2.stream("POST", gateway_url, json=body, timeout=20) as response:
        for line in response.iter_lines():
            if first_text_at is None and '"content": "plain' in line:
                first_text_at = time.perf_counter() - started
    total = time.perf_counter() - started
    assert first_text_at is not None
    assert total > 0.7  # 20 chunks, 40 ms apart
    assert first_text_at < total / 3


def test_redaction_over_real_sockets(gateway_url: str) -> None:
    body = {
        "model": "mock-echo",
        "stream": True,
        "mock": {"chunk_chars": 1},
        "messages": [{"role": "user", "content": "reach me at jane@corp.example.com today"}],
    }
    response = httpx2.post(gateway_url, json=body, timeout=20)
    assert "jane@corp.example.com" not in response.text
    assert "[REDACTED]" in response.text
