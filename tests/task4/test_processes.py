"""The router and two mock providers as real processes, with the real 3000 ms deadline.

The in-memory transport collects whole responses, so timing and mid-stream behavior are
checked here, over real sockets.
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

from shared.auth import hash_token

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "tok-process-tenant"


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


@pytest.fixture(scope="module")
def router_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    tmp = tmp_path_factory.mktemp("router")
    primary, backup, router = free_port(), free_port(), free_port()
    config = tmp / "config.toml"
    config.write_text(
        "[auth]\n"
        f'tokens = [{{ sha256 = "{hash_token(TOKEN)}", subject = "tenant", role = "viewer" }}]\n'
        "[llm_router]\n"
        f'primary_url = "http://127.0.0.1:{primary}/v1/chat/completions"\n'
        f'backup_url = "http://127.0.0.1:{backup}/v1/chat/completions"\n'
        f'db_path = "{(tmp / "limits.sqlite3").as_posix()}"\n'
        "stream_idle_timeout_s = 0.5\n"
        f"port = {router}\n",
        encoding="utf-8",
    )
    # The primary obeys the model name; the backup always answers, so every failure mode
    # of the primary can be shown failing over.
    commands = [
        (
            {"MOCK_LLM_PORT": str(primary), "MOCK_LLM_NAME": "primary"},
            "mocks.llm_provider",
            primary,
        ),
        (
            {
                "MOCK_LLM_PORT": str(backup),
                "MOCK_LLM_NAME": "backup",
                "MOCK_LLM_BEHAVIOR": "mock-echo",
            },
            "mocks.llm_provider",
            backup,
        ),
        ({"GATEWAY_CONFIG": str(config)}, "task4_rate_limit_failover", router),
    ]
    processes = []
    try:
        for extra, module, port in commands:
            process = subprocess.Popen(  # noqa: S603 - fixed command, our own interpreter
                [sys.executable, "-m", module],
                cwd=ROOT,
                env={**os.environ, **extra},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(process)
            wait_for_port(port, process)
        yield f"http://127.0.0.1:{router}/v1/chat/completions"
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=3)  # a mock still sleeping on a stalled request
            except subprocess.TimeoutExpired:
                process.kill()


def post(url: str, model: str, **extra: object) -> tuple[httpx2.Response, float]:
    body = {"model": model, "messages": [{"role": "user", "content": "hello"}], **extra}
    began = time.perf_counter()
    response = httpx2.post(url, json=body, headers={"authorization": f"Bearer {TOKEN}"}, timeout=30)
    return response, time.perf_counter() - began


def test_stalled_primary_fails_over_at_3000_ms(router_url: str) -> None:
    response, elapsed = post(router_url, "mock-stall")
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "backup"
    assert 3.0 <= elapsed < 4.5


def test_rate_limited_primary_fails_over_at_once(router_url: str) -> None:
    response, elapsed = post(router_url, "mock-429")
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "backup"
    assert elapsed < 1.5


def test_stalled_primary_stream_fails_over_too(router_url: str) -> None:
    response, elapsed = post(router_url, "mock-stall", stream=True)
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "backup"
    assert response.text.rstrip().endswith("data: [DONE]")
    assert 3.0 <= elapsed < 4.5


def test_stream_that_goes_silent_is_cut(router_url: str) -> None:
    # The primary sends its first chunks, then pauses 3 s between chunks. The stream already
    # started, so it cannot move to the backup; the 0.5 s idle limit ends it instead.
    response, elapsed = post(
        router_url, "mock-echo", stream=True, mock={"chunk_chars": 2, "delay_ms": 3000}
    )
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "primary"
    assert "[DONE]" not in response.text
    assert elapsed < 2.5
