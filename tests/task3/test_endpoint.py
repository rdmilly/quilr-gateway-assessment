"""The endpoint in front of the mock provider, in memory."""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest

from mocks.llm_provider import create_app as create_provider
from shared.audit import AuditLog
from task3_pii_guardrail.app import create_app
from task3_pii_guardrail.settings import LlmGatewaySettings

TEXT = "Hi, email john.doe@example.com, SSN 123-45-6789, card 4111 1111 1111 1111. Bye"
CLEAN = "Hi, email [REDACTED], SSN [REDACTED], card [REDACTED]. Bye"


class Rig:
    def __init__(
        self, gateway: httpx2.AsyncClient, seen: list[dict[str, Any]], audit: io.StringIO
    ) -> None:
        self.gateway, self.seen, self.audit = gateway, seen, audit

    async def chat(
        self,
        *,
        model: str = "mock-echo",
        stream: bool = True,
        text: str = TEXT,
        chunk_chars: int = 2,
        **extra: Any,
    ) -> httpx2.Response:
        body = {
            "model": model,
            "stream": stream,
            "mock": {"chunk_chars": chunk_chars},
            "messages": [{"role": "user", "content": text}],
            **extra,
        }
        return await self.gateway.post("/v1/chat/completions", json=body)


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    seen: list[dict[str, Any]] = []
    provider = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_provider(seen=seen)), base_url="http://p"
    )
    sink = io.StringIO()
    app = create_app(
        LlmGatewaySettings(
            provider_url="http://p/v1/chat/completions",
            provider_api_key_env="PROVIDER_API_KEY",
            max_body_bytes=4096,
        ),
        client=provider,
        audit=AuditLog(sink=sink),
        environ={"PROVIDER_API_KEY": "provider-key-held-by-gateway"},
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://g"
    ) as gateway:
        yield Rig(gateway, seen, sink)
    await provider.aclose()


def chunks_of(response: httpx2.Response) -> list[dict[str, Any]]:
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def text_of(response: httpx2.Response, index: int = 0) -> str:
    return "".join(
        c["delta"].get("content") or ""
        for ch in chunks_of(response)
        for c in ch["choices"]
        if c["index"] == index
    )


@pytest.mark.parametrize("chunk_chars", [1, 2, 3, 7, 50])
async def test_stream_is_redacted_whatever_the_chunk_size(rig: Rig, chunk_chars: int) -> None:
    response = await rig.chat(chunk_chars=chunk_chars)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert text_of(response) == CLEAN
    assert response.text.rstrip().endswith("data: [DONE]")
    for secret in ("john.doe@example.com", "123-45-6789", "4111 1111 1111 1111"):
        assert secret not in response.text


async def test_every_choice_is_redacted(rig: Rig) -> None:
    response = await rig.chat(n=3)
    assert [text_of(response, i) for i in range(3)] == [CLEAN] * 3


async def test_tool_call_arguments_are_redacted(rig: Rig) -> None:
    response = await rig.chat(model="mock-tool")
    args = "".join(
        tc["function"].get("arguments", "")
        for ch in chunks_of(response)
        for c in ch["choices"]
        for tc in c["delta"].get("tool_calls", [])
    )
    assert args == CLEAN


async def test_usage_chunk_passes_through(rig: Rig) -> None:
    response = await rig.chat(stream_options={"include_usage": True})
    usage = [c["usage"] for c in chunks_of(response) if c.get("usage")]
    assert usage
    assert usage[0]["total_tokens"] > 0


async def test_non_streamed_answer_is_redacted_too(rig: Rig) -> None:
    response = await rig.chat(stream=False)
    assert response.json()["choices"][0]["message"]["content"] == CLEAN


async def test_provider_gets_the_gateway_key_never_the_callers(rig: Rig) -> None:
    await rig.gateway.post(
        "/v1/chat/completions",
        json={"model": "mock-echo", "messages": []},
        headers={"authorization": "Bearer callers-own-key"},
    )
    sent = rig.seen[-1]["headers"]
    assert sent["authorization"] == "Bearer provider-key-held-by-gateway"


@pytest.mark.parametrize(
    ("model", "status", "error_type"),
    [("mock-500", 502, "upstream_unavailable"), ("mock-429", 429, "rate_limit_exceeded")],
)
async def test_provider_errors_are_sanitized(
    rig: Rig, model: str, status: int, error_type: str
) -> None:
    response = await rig.chat(model=model)
    assert response.status_code == status
    error = response.json()["error"]
    assert error["type"] == error_type
    assert error["request_id"].startswith("req_")
    for leak in ("Traceback", "/srv/", "10.0.3.7", "org-123", "CUDA"):
        assert leak not in response.text
    if status == 429:
        assert response.headers["retry-after"] == "7"


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"not json", "application/json"),
        (b"[1,2]", "application/json"),
        (b"{}", "text/plain"),
        (b'{"x": "' + b"a" * 5000 + b'"}', "application/json"),
    ],
)
async def test_bad_requests_get_a_clean_400(rig: Rig, body: bytes, content_type: str) -> None:
    response = await rig.gateway.post(
        "/v1/chat/completions", content=body, headers={"content-type": content_type}
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"
    assert rig.seen == []


async def test_audit_records_counts_not_text(rig: Rig) -> None:
    await rig.chat()
    record = json.loads(rig.audit.getvalue().splitlines()[-1])
    assert record["unmapped"]["counts"] == {"redactions": 3}
    assert record["action_id"] == 4  # OCSF: modified
    assert "john.doe" not in rig.audit.getvalue()
