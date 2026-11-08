"""Failure paths with a scripted provider: broken streams, bad answers, a failing store."""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest

from shared.audit import AuditLog
from shared.auth import Authenticator, AuthSettings, Role, TokenEntry, hash_token
from shared.sse import MAX_EVENT_CHARS
from task4_rate_limit_failover.app import create_app
from task4_rate_limit_failover.limiter import Reservation, SlidingWindowLimiter, StoreUnavailable
from task4_rate_limit_failover.router import Provider, Router
from task4_rate_limit_failover.settings import RouterSettings

TOKEN = "tok-failures"
Handler = Callable[[httpx2.Request], httpx2.Response]


def chunk(content: str) -> bytes:
    body = {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]}
    return f"data: {json.dumps(body)}\n\n".encode()


class FlakyLimiter(SlidingWindowLimiter):
    """Reserves normally, then loses its database before settling or releasing."""

    def settle(self, reservation: Reservation, actual_tokens: int) -> None:
        raise StoreUnavailable

    def release(self, reservation: Reservation) -> None:
        raise StoreUnavailable


def build(
    handler: Handler,
    tmp_path: Path,
    *,
    limiter_type: type[SlidingWindowLimiter] = SlidingWindowLimiter,
) -> tuple[httpx2.AsyncClient, SlidingWindowLimiter, io.StringIO]:
    settings = RouterSettings(failover_timeout_ms=500, stream_idle_timeout_s=0.5)
    limiter = limiter_type(tmp_path / "limits.sqlite3", settings.tokens_per_window)
    auth = Authenticator(
        AuthSettings(tokens=(TokenEntry(sha256=hash_token(TOKEN), subject="t", role=Role.VIEWER),))
    )
    sink = io.StringIO()
    upstream = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    app = create_app(
        settings,
        auth,
        limiter,
        [Provider("primary", "http://primary/v1"), Provider("backup", "http://backup/v1")],
        client=upstream,
        audit=AuditLog(sink=sink),
    )
    gateway = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://g")
    return gateway, limiter, sink


async def ask(gateway: httpx2.AsyncClient, **extra: Any) -> httpx2.Response:
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10, **extra}
    return await gateway.post(
        "/v1/chat/completions", json=body, headers={"authorization": f"Bearer {TOKEN}"}
    )


def reason(sink: io.StringIO) -> str:
    return json.dumps([json.loads(line) for line in sink.getvalue().splitlines()])


async def test_stream_that_breaks_midway_ends_and_keeps_the_estimate(tmp_path: Path) -> None:
    async def body() -> AsyncIterator[bytes]:
        yield chunk("partial")
        raise httpx2.ReadError("connection reset")

    gateway, limiter, sink = build(lambda request: httpx2.Response(200, content=body()), tmp_path)
    response = await ask(gateway, stream=True)
    assert response.status_code == 200
    assert "partial" in response.text
    assert "[DONE]" not in response.text
    assert limiter.used("t") > 10  # no usage was reported, so the estimate stands
    assert "provider stream broke" in reason(sink)


async def test_oversized_stream_event_is_cut(tmp_path: Path) -> None:
    async def body() -> AsyncIterator[bytes]:
        yield b"data: " + b"x" * (MAX_EVENT_CHARS + 10)

    gateway, _, sink = build(lambda request: httpx2.Response(200, content=body()), tmp_path)
    response = await ask(gateway, stream=True)
    assert response.status_code == 200
    assert "xxxx" not in response.text
    assert "too large" in reason(sink)


async def test_non_json_stream_events_pass_through(tmp_path: Path) -> None:
    async def body() -> AsyncIterator[bytes]:
        yield b"data: keepalive\n\ndata: [1, 2]\n\ndata: [DONE]\n\n"

    gateway, _, _ = build(lambda request: httpx2.Response(200, content=body()), tmp_path)
    response = await ask(gateway, stream=True)
    assert "data: keepalive" in response.text
    assert "data: [1, 2]" in response.text


async def test_garbled_plain_answer_is_502_and_stays_charged(tmp_path: Path) -> None:
    gateway, limiter, _ = build(lambda request: httpx2.Response(200, content=b"<html>"), tmp_path)
    response = await ask(gateway)
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_unavailable"
    assert limiter.used("t") > 0  # the provider answered, and probably billed


async def test_oversized_plain_answer_is_refused(tmp_path: Path) -> None:
    big = b'{"x": "' + b"y" * (9 * 1_048_576) + b'"}'
    gateway, _, _ = build(lambda request: httpx2.Response(200, content=big), tmp_path)
    response = await ask(gateway)
    assert response.status_code == 502
    assert len(response.content) < 1000


async def test_answer_without_usage_keeps_the_estimate(tmp_path: Path) -> None:
    gateway, limiter, _ = build(
        lambda request: httpx2.Response(200, json={"choices": []}), tmp_path
    )
    response = await ask(gateway)
    assert response.status_code == 200
    assert limiter.used("t") > 10


@pytest.mark.parametrize("usage", [{"total_tokens": -1}, {"total_tokens": True}, "lots"])
async def test_nonsense_usage_is_ignored(tmp_path: Path, usage: object) -> None:
    gateway, limiter, _ = build(
        lambda request: httpx2.Response(200, json={"choices": [], "usage": usage}), tmp_path
    )
    await ask(gateway)
    assert limiter.used("t") > 10


async def test_connection_refused_is_502(tmp_path: Path) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    gateway, limiter, _ = build(refuse, tmp_path)
    response = await ask(gateway)
    assert response.status_code == 502
    assert limiter.used("t") == 0


async def test_store_lost_after_reserving_still_answers(tmp_path: Path) -> None:
    gateway, _, _ = build(
        lambda request: httpx2.Response(200, json={"choices": [], "usage": {"total_tokens": 3}}),
        tmp_path,
        limiter_type=FlakyLimiter,
    )
    assert (await ask(gateway)).status_code == 200


async def test_store_lost_before_refund_still_reports_the_failure(tmp_path: Path) -> None:
    gateway, _, _ = build(lambda request: httpx2.Response(429), tmp_path, limiter_type=FlakyLimiter)
    response = await ask(gateway)
    assert response.status_code == 429


# The router on its own ---------------------------------------------------------------


def test_router_needs_a_provider() -> None:
    with pytest.raises(ValueError, match="provider"):
        Router(httpx2.AsyncClient(), [])


async def test_cancelled_attempt_closes_the_half_open_response() -> None:
    closed = anyio.Event()

    async def never() -> AsyncIterator[bytes]:
        try:
            await anyio.sleep(60)
            yield b""
        finally:
            closed.set()

    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, content=never()))
    )
    router = Router(client, [Provider("primary", "http://primary/v1")], timeout_s=30)
    with anyio.move_on_after(0.2):  # the client gives up while we wait for the first chunk
        await router.open(b"{}", streaming=True)
    with anyio.fail_after(2):
        await closed.wait()
    await client.aclose()


async def test_timed_out_attempt_closes_before_the_backup_runs() -> None:
    events: list[str] = []

    async def slow() -> AsyncIterator[bytes]:
        try:
            await anyio.sleep(60)
            yield b""
        finally:
            events.append("primary closed")

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "primary":
            return httpx2.Response(200, content=slow())
        events.append("backup called")

        async def answer() -> AsyncIterator[bytes]:
            yield chunk("hello")

        return httpx2.Response(200, content=answer())

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    router = Router(
        client,
        [Provider("primary", "http://primary/v1"), Provider("backup", "http://backup/v1")],
        timeout_s=0.1,
    )
    opened = await router.open(b"{}", streaming=True)
    assert opened.provider.name == "backup"
    assert events == ["primary closed", "backup called"]
    await opened.response.aclose()
    await client.aclose()
