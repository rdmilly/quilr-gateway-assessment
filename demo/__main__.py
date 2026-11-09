"""Run every task for real and show the behavior the brief asks for, in about 20 seconds.

    uv sync
    uv run python -m demo

It starts the mock servers and the three gateways as background processes on free local
ports, with a throwaway config, then sends real requests and prints what came back. Each
line says what was expected and whether it happened, so the tour doubles as a smoke test:
the exit code is 0 only if every check passed. Everything is stopped at the end.

Nothing here is used by the tasks themselves; it only drives them from outside.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx2

from shared.auth import hash_token

ROOT = Path(__file__).resolve().parents[1]
ADMIN, VIEWER = "test-admin-token", "test-viewer-token"  # the fake tokens in config.example.toml
META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "demo", "version": "1"},
}
PII = "Mail john.doe@example.com, SSN 123-45-6789, card 4111 1111 1111 1111. Thanks"
LEAKS = ("Traceback", "/srv/", "10.0.3.7", "org-123")

failures: list[str] = []


def heading(title: str) -> None:
    print(f"\n== {title}")


def check(label: str, passed: bool, detail: str = "") -> None:
    print(f"  [{'ok' if passed else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))
    if not passed:
        failures.append(label)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(port: int, process: subprocess.Popen[bytes], timeout_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"a process exited early (code {process.returncode})")
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError(f"nothing listening on port {port}")


@contextmanager
def services(workdir: Path) -> Iterator[dict[str, int]]:
    ports = {name: free_port() for name in ("mcp", "gw", "p1", "p2", "pii", "router")}
    config = workdir / "config.toml"
    tokens = ", ".join(
        f'{{ sha256 = "{hash_token(token)}", subject = "{subject}", role = "{role}" }}'
        for token, subject, role in ((ADMIN, "alice", "admin"), (VIEWER, "victor", "viewer"))
    )
    config.write_text(
        f"[auth]\ntokens = [{tokens}]\n\n"
        f'[mcp_gateway]\ndownstream_url = "http://127.0.0.1:{ports["mcp"]}/mcp"\n'
        f"port = {ports['gw']}\n\n"
        f'[llm_gateway]\nprovider_url = "http://127.0.0.1:{ports["p1"]}/v1/chat/completions"\n'
        f"port = {ports['pii']}\n\n"
        f'[llm_router]\nprimary_url = "http://127.0.0.1:{ports["p1"]}/v1/chat/completions"\n'
        f'backup_url = "http://127.0.0.1:{ports["p2"]}/v1/chat/completions"\n'
        f'db_path = "{(workdir / "limits.sqlite3").as_posix()}"\n'
        "tokens_per_window = 3000\n"  # small on purpose, so the tour can show a refusal
        f"port = {ports['router']}\n",
        encoding="utf-8",
    )
    plan = [
        ("mocks.mcp_server", {"MOCK_MCP_PORT": str(ports["mcp"])}, ports["mcp"]),
        (
            "mocks.llm_provider",
            {"MOCK_LLM_PORT": str(ports["p1"]), "MOCK_LLM_NAME": "primary"},
            ports["p1"],
        ),
        (
            "mocks.llm_provider",
            {
                "MOCK_LLM_PORT": str(ports["p2"]),
                "MOCK_LLM_NAME": "backup",
                "MOCK_LLM_BEHAVIOR": "mock-echo",
            },
            ports["p2"],
        ),
        ("task2_mcp_gateway", {}, ports["gw"]),
        ("task3_pii_guardrail", {}, ports["pii"]),
        ("task4_rate_limit_failover", {}, ports["router"]),
    ]
    processes: list[subprocess.Popen[bytes]] = []
    log = (workdir / "services.log").open("wb")
    try:
        for module, extra, port in plan:
            process = subprocess.Popen(  # noqa: S603 - fixed modules, our own interpreter
                [sys.executable, "-m", module],
                cwd=ROOT,
                env={**os.environ, "GATEWAY_CONFIG": str(config), **extra},
                stdout=log,
                stderr=log,
            )
            processes.append(process)
            wait_for_port(port, process)
        yield ports
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        log.close()


def task1() -> None:
    heading("Task 1: MCP server over stdio, strict validation")

    def call(request_id: int, name: str, arguments: dict[str, Any]) -> str:
        params = {"name": name, "arguments": arguments, "_meta": META}
        return json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}
        )

    requests = [
        call(1, "get_customer_record", {"customer_id": "CUST-00042"}),
        call(2, "get_customer_record", {"customer_id": "CUST-42"}),
        call(
            3,
            "trigger_refund",
            {"customer_id": "CUST-00042", "amount": -5, "reason": "Damaged on arrival"},
        ),
        call(4, "trigger_refund", {"customer_id": "CUST-00042", "amount": 12.5, "reason": "short"}),
        call(
            5,
            "trigger_refund",
            {"customer_id": "CUST-00042", "amount": 12.5, "reason": "Damaged on arrival"},
        ),
        "this line is not JSON",
    ]
    result = subprocess.run(
        [sys.executable, "-m", "task1_mcp_server"],
        cwd=ROOT,
        input="\n".join(requests) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except ValueError:
            parsed.append(None)
    check(
        "stdout carries nothing but JSON-RPC (logs went to stderr)",
        bool(parsed) and all(isinstance(p, dict) and p.get("jsonrpc") == "2.0" for p in parsed),
        f"{len(lines)} lines on stdout",
    )
    by_id = {p.get("id"): p for p in parsed if isinstance(p, dict)}

    def error_code(request_id: int | None) -> object:
        return (by_id.get(request_id) or {}).get("error", {}).get("code")

    record = by_id.get(1, {}).get("result", {})
    check(
        "valid customer_id CUST-00042 returns the record",
        "result" in by_id.get(1, {}) and not record.get("isError"),
    )
    check(
        "malformed customer_id CUST-42 is -32602 Invalid params",
        error_code(2) == -32602,
        json.dumps(by_id.get(2, {}).get("error", {}))[:110],
    )
    check("negative amount is -32602", error_code(3) == -32602)
    check("reason shorter than 10 characters is -32602", error_code(4) == -32602)
    check(
        "valid refund succeeds",
        "result" in by_id.get(5, {}) and not by_id[5]["result"].get("isError"),
    )
    check("a line that is not JSON gets -32700 Parse error", error_code(None) == -32700)


def task2(port: int) -> None:
    heading("Task 2: MCP gateway, bearer token roles, admin_ tools")
    url = f"http://127.0.0.1:{port}/mcp"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "admin_reset_key", "arguments": {"key_id": "k1"}},
    }
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}

    def post(token: str | None, payload: dict[str, Any]) -> httpx2.Response:
        extra = {"authorization": f"Bearer {token}"} if token else {}
        return httpx2.post(url, json=payload, headers={**headers, **extra}, timeout=15)

    def message(response: httpx2.Response) -> dict[str, Any]:
        text = response.text
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
        loaded: dict[str, Any] = json.loads(text)
        return loaded

    viewer = post(VIEWER, body)
    error = message(viewer).get("error", {})
    check(
        "viewer calling admin_reset_key gets -32001 Unauthorized Tool Call",
        error == {"code": -32001, "message": "Unauthorized Tool Call"},
        json.dumps(error),
    )
    admin = post(ADMIN, body)
    check(
        "admin calling admin_reset_key is forwarded and the tool runs",
        admin.status_code == 200 and "result" in message(admin),
    )
    listing = post(VIEWER, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = [t["name"] for t in message(listing).get("result", {}).get("tools", [])]
    check("tools/list is forwarded transparently", "admin_reset_key" in names, ", ".join(names))
    anonymous = post(None, body)
    check(
        "no token is 401 with WWW-Authenticate",
        anonymous.status_code == 401 and "www-authenticate" in anonymous.headers,
    )


def task3(port: int) -> None:
    heading("Task 3: streaming PII redaction")
    body = {
        "model": "mock-echo",
        "stream": True,
        "mock": {"chunk_chars": 2, "delay_ms": 5},
        "messages": [{"role": "user", "content": PII}],
    }
    began = time.perf_counter()
    first_ms = None
    pieces: list[str] = []
    raw: list[str] = []
    with httpx2.stream(
        "POST", f"http://127.0.0.1:{port}/v1/chat/completions", json=body, timeout=30
    ) as response:
        for line in response.iter_lines():
            raw.append(line)
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            for choice in json.loads(line[6:]).get("choices", []):
                text = (choice.get("delta") or {}).get("content") or ""
                if text and first_ms is None:
                    first_ms = (time.perf_counter() - began) * 1000
                pieces.append(text)
    total_ms = (time.perf_counter() - began) * 1000
    answer = "".join(pieces)
    print(f"  provider streamed 2 characters at a time; client received:\n         {answer}")
    check(
        "email, SSN and card number replaced with [REDACTED]",
        answer == "Mail [REDACTED], SSN [REDACTED], card [REDACTED]. Thanks",
    )
    check(
        "no PII anywhere in the raw stream",
        not any(s in "\n".join(raw) for s in ("john.doe", "123-45-6789", "4111 1111")),
    )
    check(
        "first text arrived long before the stream ended (no full buffering)",
        first_ms is not None and first_ms < total_ms / 2,
        f"first text at {first_ms or 0:.0f} ms of {total_ms:.0f} ms",
    )


def task4(port: int) -> None:
    heading("Task 4: token rate limiter and failover")
    url = f"http://127.0.0.1:{port}/v1/chat/completions"

    def ask(model: str, max_tokens: int = 50, token: str = VIEWER) -> tuple[httpx2.Response, float]:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": max_tokens,
        }
        began = time.perf_counter()
        response = httpx2.post(
            url, json=body, headers={"authorization": f"Bearer {token}"}, timeout=30
        )
        return response, time.perf_counter() - began

    ok, _ = ask("mock-echo")
    check(
        "healthy primary answers",
        ok.status_code == 200 and ok.headers.get("x-gateway-provider") == "primary",
    )
    limited, seconds = ask("mock-429")
    check(
        "primary 429: fails over to the backup at once",
        limited.headers.get("x-gateway-provider") == "backup" and seconds < 1.5,
        f"answered by {limited.headers.get('x-gateway-provider')} in {seconds:.2f} s",
    )
    stalled, seconds = ask("mock-stall")
    check(
        "primary silent: fails over to the backup at the 3000 ms deadline",
        stalled.headers.get("x-gateway-provider") == "backup" and 3.0 <= seconds < 4.5,
        f"answered by {stalled.headers.get('x-gateway-provider')} in {seconds:.2f} s",
    )
    broken, _ = ask("mock-500")
    leaked = [s for s in LEAKS if s in broken.text]
    check(
        "provider 500 with a stack trace: clean error, nothing leaked",
        broken.status_code == 502
        and not leaked
        and set(broken.json().get("error", {})) == {"type", "code", "message", "request_id"},
        broken.text,
    )

    holder: dict[str, httpx2.Response] = {}
    in_flight = threading.Thread(target=lambda: holder.update(r=ask("mock-stall", 1500)[0]))
    in_flight.start()
    time.sleep(0.5)  # the first request now holds its reservation for about 3 s
    refused, _ = ask("mock-echo", 2000)
    in_flight.join()
    check(
        "over budget: 429 with Retry-After (demo budget is 3,000 tokens a minute)",
        refused.status_code == 429 and "retry-after" in refused.headers,
        f"{refused.status_code}, Retry-After: {refused.headers.get('retry-after')}",
    )
    too_big, _ = ask("mock-echo", 10_000)
    check(
        "bigger than the whole budget: 400 request_too_large, not a retryable 429",
        too_big.status_code == 400 and too_big.json()["error"]["type"] == "request_too_large",
    )
    anonymous = httpx2.post(url, json={}, timeout=10)
    check("no token is 401", anonymous.status_code == 401)


def main() -> int:
    print("QuilrAI assessment: a guided tour of all four tasks")
    print("Starting the mock servers and the three gateways on free local ports...")
    with tempfile.TemporaryDirectory(prefix="quilr-demo-") as tmp:
        task1()
        with services(Path(tmp)) as ports:
            task2(ports["gw"])
            task3(ports["pii"])
            task4(ports["router"])
    print()
    if failures:
        print(f"{len(failures)} check(s) failed: " + "; ".join(failures))
        return 1
    print("All checks passed. Full test suite: ./check.sh  (or: uv run pytest)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
