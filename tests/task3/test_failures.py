"""What the endpoint does when the provider misbehaves."""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx2
import pytest

from shared.audit import AuditLog
from task3_pii_guardrail.app import create_app
from task3_pii_guardrail.settings import LlmGatewaySettings

Handler = Callable[[httpx2.Request], httpx2.Response]


async def call(
    handler: Handler, *, stream: bool = True, **settings: Any
) -> tuple[httpx2.Response, str]:
    sink = io.StringIO()
    app = create_app(
        LlmGatewaySettings(provider_url="http://p/v1/chat/completions", **settings),
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        audit=AuditLog(sink=sink),
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://g"
    ) as gateway:
        response = await gateway.post(
            "/v1/chat/completions", json={"model": "m", "stream": stream, "messages": []}
        )
    return response, sink.getvalue()


def sse(*events: str, done: bool = True) -> bytes:
    body = "".join(f"data: {e}\n\n" for e in events)
    return (body + ("data: [DONE]\n\n" if done else "")).encode()


def streamed(body: bytes) -> httpx2.Response:
    """A provider response that arrives as a stream, like a real one."""

    async def pieces() -> AsyncIterator[bytes]:
        yield body

    return httpx2.Response(200, content=pieces(), headers={"content-type": "text/event-stream"})


def chunk(content: str, finish: str | None = None) -> str:
    return json.dumps(
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}],
        }
    )


def text_of(response: httpx2.Response) -> str:
    out = ""
    for line in response.text.splitlines():
        if line.startswith("data: {"):
            for c in json.loads(line[6:]).get("choices", []):
                out += c["delta"].get("content") or ""
    return out


@pytest.mark.parametrize(
    ("error", "status", "kind"),
    [
        (httpx2.ReadTimeout("t"), 504, "upstream_timeout"),
        (httpx2.ConnectError("refused"), 502, "upstream_unavailable"),
    ],
)
async def test_provider_unreachable(error: Exception, status: int, kind: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise error

    response, _ = await call(handler)
    assert response.status_code == status
    assert response.json()["error"]["type"] == kind
    assert "refused" not in response.text


async def test_stream_that_ends_without_done_still_releases_held_text() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return streamed(sse(chunk("SSN 123-45-6789"), done=False))

    response, audit = await call(handler)
    assert text_of(response) == "SSN [REDACTED]"
    assert "123-45-6789" not in response.text
    assert "without [DONE]" in audit


async def test_non_json_events_are_redacted_as_text() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return streamed(sse("plain words for x@y.io"))

    response, _ = await call(handler)
    assert "data: plain words for [REDACTED]" in response.text
    assert "x@y.io" not in response.text


async def test_oversized_event_cuts_the_stream() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return streamed(b"data: " + b"x" * 1_100_000 + b"\n\n")

    response, audit = await call(handler)
    assert "x" * 1000 not in response.text
    assert "too large" in audit


async def test_stream_that_breaks_midway_is_closed_cleanly() -> None:
    async def broken() -> AsyncIterator[bytes]:
        yield sse(chunk("card 4111 1111 "), done=False)
        raise httpx2.ReadError("connection reset")

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=broken(), headers={"content-type": "text/event-stream"})

    response, audit = await call(handler)
    assert "4111" not in response.text  # the held half of a card number is never sent
    assert "stream broke" in audit


@pytest.mark.parametrize("content", [b"not json", b"[1, 2]", b'{"x": "' + b"a" * 600 + b'"}'])
async def test_bad_non_streamed_answers_become_a_clean_502(content: bytes) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=content)

    response, _ = await call(handler, stream=False, max_response_bytes=500)
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_unavailable"


async def test_gateway_creates_and_closes_its_own_client() -> None:
    app = create_app(
        LlmGatewaySettings(
            provider_url="http://127.0.0.1:9/v1/chat/completions", upstream_timeout_s=2
        ),
        audit=AuditLog(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://g") as gateway,
    ):
        response = await gateway.post("/v1/chat/completions", json={"messages": []})
    assert response.status_code in (502, 504)


@pytest.mark.parametrize("url", ["ftp://p/v1", "not a url"])
def test_provider_url_must_be_http(url: str) -> None:
    with pytest.raises(ValueError, match="http or https URL"):
        LlmGatewaySettings(provider_url=url)
