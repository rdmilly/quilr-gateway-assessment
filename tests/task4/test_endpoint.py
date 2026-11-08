"""The router endpoint in memory: budget, failover rules, error cleaning, billing."""

from __future__ import annotations

import io
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from starlette.types import ASGIApp, Receive, Scope, Send

from mocks.llm_provider import create_app as create_provider
from shared.audit import AuditLog
from shared.auth import Authenticator, AuthSettings, Role, TokenEntry, hash_token
from task4_rate_limit_failover.app import create_app
from task4_rate_limit_failover.limiter import SlidingWindowLimiter
from task4_rate_limit_failover.router import Provider
from task4_rate_limit_failover.settings import RouterSettings

TOKENS = {"tenant-a": "tok-tenant-a", "tenant-b": "tok-tenant-b"}
LEAKS = ("Traceback", "/srv/", "10.0.3.7", "CUDA", "org-123", "primary-provider-key")


def by_host(apps: dict[str, ASGIApp]) -> ASGIApp:
    """One in-memory transport, two providers: route on the Host header."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        host = dict(scope["headers"])[b"host"].decode().split(":")[0]
        await apps[host](scope, receive, send)

    return app


class Rig:
    def __init__(
        self,
        gateway: httpx2.AsyncClient,
        limiter: SlidingWindowLimiter,
        primary_seen: list[dict[str, Any]],
        backup_seen: list[dict[str, Any]],
        audit: io.StringIO,
    ) -> None:
        self.gateway, self.limiter, self.audit = gateway, limiter, audit
        self.primary_seen, self.backup_seen = primary_seen, backup_seen

    async def chat(
        self,
        *,
        model: str = "mock-echo",
        stream: bool = False,
        tenant: str = "tenant-a",
        text: str = "hello world",
        **extra: Any,
    ) -> httpx2.Response:
        body = {
            "model": model,
            "stream": stream,
            "messages": [{"role": "user", "content": text}],
            "max_tokens": 50,
            **extra,
        }
        return await self.gateway.post(
            "/v1/chat/completions",
            json=body,
            headers={"authorization": f"Bearer {TOKENS[tenant]}"},
        )

    def audit_records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.audit.getvalue().splitlines()]


RigFactory = Callable[..., Awaitable[Rig]]


@pytest.fixture
async def make_rig(tmp_path: Path) -> AsyncIterator[RigFactory]:
    clients: list[httpx2.AsyncClient] = []

    async def factory(*, backup_behavior: str | None = "mock-echo", **overrides: Any) -> Rig:
        primary_seen: list[dict[str, Any]] = []
        backup_seen: list[dict[str, Any]] = []
        transport = httpx2.ASGITransport(
            app=by_host(
                {
                    "primary": create_provider("primary", primary_seen),
                    "backup": create_provider("backup", backup_seen, backup_behavior),
                }
            )
        )
        upstream = httpx2.AsyncClient(transport=transport)
        values: dict[str, Any] = {
            "failover_timeout_ms": 300,
            "tokens_per_window": 10_000,
            "max_body_bytes": 8192,
        }
        values.update(overrides)
        settings = RouterSettings(**values)
        limiter = SlidingWindowLimiter(
            tmp_path / f"limits-{len(clients)}.sqlite3", settings.tokens_per_window
        )
        auth = Authenticator(
            AuthSettings(
                tokens=tuple(
                    TokenEntry(sha256=hash_token(token), subject=subject, role=Role.VIEWER)
                    for subject, token in TOKENS.items()
                )
            )
        )
        sink = io.StringIO()
        app = create_app(
            settings,
            auth,
            limiter,
            [
                Provider("primary", "http://primary/v1/chat/completions", "primary-provider-key"),
                Provider("backup", "http://backup/v1/chat/completions", "backup-provider-key"),
            ],
            client=upstream,
            audit=AuditLog(sink=sink),
        )
        gateway = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://g")
        clients.extend([upstream, gateway])
        return Rig(gateway, limiter, primary_seen, backup_seen, sink)

    yield factory
    for client in clients:
        await client.aclose()


def error_of(response: httpx2.Response) -> dict[str, Any]:
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"type", "code", "message", "request_id"}
    assert body["error"]["code"] == response.status_code
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    for leak in LEAKS:
        assert leak not in response.text
    return dict(body["error"])


def sse_chunks(response: httpx2.Response) -> list[dict[str, Any]]:
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


# Routing --------------------------------------------------------------------------------


async def test_primary_answers_when_healthy(make_rig: RigFactory) -> None:
    rig = await make_rig()
    response = await rig.chat()
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "primary"
    assert response.json()["choices"][0]["message"]["content"] == "hello world"
    assert (len(rig.primary_seen), len(rig.backup_seen)) == (1, 0)


async def test_primary_429_fails_over_to_backup(make_rig: RigFactory) -> None:
    rig = await make_rig()
    response = await rig.chat(model="mock-429")
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "backup"
    assert response.json()["id"] == "chatcmpl-backup"
    assert (len(rig.primary_seen), len(rig.backup_seen)) == (1, 1)
    assert rig.backup_seen[0]["body"] == rig.primary_seen[0]["body"]  # the same request


async def test_primary_timeout_fails_over_within_the_deadline(make_rig: RigFactory) -> None:
    rig = await make_rig(failover_timeout_ms=200)
    began = time.perf_counter()
    response = await rig.chat(model="mock-stall")
    elapsed = time.perf_counter() - began
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "backup"
    assert 0.2 <= elapsed < 2.0


async def test_streamed_request_fails_over_too(make_rig: RigFactory) -> None:
    rig = await make_rig()
    response = await rig.chat(model="mock-429", stream=True)
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "backup"
    text = "".join(
        c["delta"].get("content") or "" for ch in sse_chunks(response) for c in ch["choices"]
    )
    assert text == "hello world"
    assert response.text.rstrip().endswith("data: [DONE]")


async def test_5xx_does_not_fail_over_by_default(make_rig: RigFactory) -> None:
    rig = await make_rig()
    response = await rig.chat(model="mock-500")
    assert response.status_code == 502
    assert error_of(response)["type"] == "upstream_unavailable"
    assert len(rig.backup_seen) == 0


async def test_5xx_fails_over_when_configured(make_rig: RigFactory) -> None:
    rig = await make_rig(failover_on_errors=True)
    response = await rig.chat(model="mock-500")
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "backup"


async def test_both_rate_limited_returns_429_with_retry_after(make_rig: RigFactory) -> None:
    rig = await make_rig(backup_behavior=None)
    response = await rig.chat(model="mock-429")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert error_of(response)["type"] == "rate_limit_exceeded"


async def test_both_timing_out_returns_504(make_rig: RigFactory) -> None:
    rig = await make_rig(backup_behavior=None, failover_timeout_ms=100)
    response = await rig.chat(model="mock-stall")
    assert response.status_code == 504
    assert error_of(response)["type"] == "upstream_timeout"


async def test_provider_error_details_never_reach_the_client(make_rig: RigFactory) -> None:
    rig = await make_rig(backup_behavior=None, failover_on_errors=True)
    for model in ("mock-500", "mock-429"):
        response = await rig.chat(model=model)
        error_of(response)  # checks every known leak string is absent


async def test_provider_keys_are_the_gateways_never_the_callers(make_rig: RigFactory) -> None:
    rig = await make_rig()
    await rig.chat(model="mock-429")
    assert rig.primary_seen[0]["headers"]["authorization"] == "Bearer primary-provider-key"
    assert rig.backup_seen[0]["headers"]["authorization"] == "Bearer backup-provider-key"


# Budget ---------------------------------------------------------------------------------


async def test_charges_the_reported_usage_not_the_estimate(make_rig: RigFactory) -> None:
    rig = await make_rig()
    response = await rig.chat()
    assert rig.limiter.used("tenant-a") == response.json()["usage"]["total_tokens"]


async def test_failover_charges_once(make_rig: RigFactory) -> None:
    rig = await make_rig()
    response = await rig.chat(model="mock-429")
    assert rig.limiter.used("tenant-a") == response.json()["usage"]["total_tokens"]


async def test_stream_charges_reported_usage_and_hides_it(make_rig: RigFactory) -> None:
    rig = await make_rig()
    response = await rig.chat(stream=True)
    chunks = sse_chunks(response)
    assert all("usage" not in chunk for chunk in chunks)  # the client did not ask
    assert all(chunk["choices"] for chunk in chunks)
    sent = rig.primary_seen[0]["body"]
    assert sent["stream_options"] == {"include_usage": True}  # we asked on its behalf
    assert 0 < rig.limiter.used("tenant-a") < 50  # real usage, below the 50+ estimate


async def test_stream_keeps_usage_when_the_client_asked(make_rig: RigFactory) -> None:
    rig = await make_rig()
    response = await rig.chat(stream=True, stream_options={"include_usage": True})
    usage = [chunk["usage"] for chunk in sse_chunks(response) if chunk.get("usage")]
    assert len(usage) == 1
    assert rig.limiter.used("tenant-a") == usage[0]["total_tokens"]


async def test_over_budget_is_429_and_never_reaches_a_provider(make_rig: RigFactory) -> None:
    rig = await make_rig(tokens_per_window=100)
    rig.limiter.reserve("tenant-a", 95)  # earlier traffic left 5 tokens
    response = await rig.chat(max_tokens=10)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1
    assert error_of(response)["type"] == "rate_limit_exceeded"
    assert len(rig.primary_seen) == 0
    assert rig.limiter.used("tenant-a") == 95  # a refusal costs nothing


async def test_tenants_have_separate_budgets(make_rig: RigFactory) -> None:
    rig = await make_rig(tokens_per_window=100)
    rig.limiter.reserve("tenant-a", 100)
    assert (await rig.chat(tenant="tenant-a")).status_code == 429
    assert (await rig.chat(tenant="tenant-b")).status_code == 200


async def test_request_bigger_than_the_window_is_400(make_rig: RigFactory) -> None:
    rig = await make_rig(tokens_per_window=100)
    response = await rig.chat(max_tokens=500)
    assert response.status_code == 400
    assert error_of(response)["type"] == "request_too_large"
    assert "retry-after" not in response.headers


async def test_failed_providers_refund_the_reservation(make_rig: RigFactory) -> None:
    rig = await make_rig(backup_behavior=None)
    response = await rig.chat(model="mock-429")
    assert response.status_code == 429
    assert rig.limiter.used("tenant-a") == 0


def corrupt_databases(directory: Path) -> None:
    for path in directory.glob("limits-*"):
        path.write_bytes(b"garbage" * 200)


async def test_broken_store_fails_closed(make_rig: RigFactory, tmp_path: Path) -> None:
    rig = await make_rig()
    corrupt_databases(tmp_path)
    response = await rig.chat()
    assert response.status_code == 503
    assert error_of(response)["type"] == "service_unavailable"
    assert len(rig.primary_seen) == 0


# Requests -------------------------------------------------------------------------------


async def test_missing_or_wrong_token_is_401(make_rig: RigFactory) -> None:
    rig = await make_rig()
    for headers in ({}, {"authorization": "Bearer nope"}):
        response = await rig.gateway.post("/v1/chat/completions", json={}, headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")
        assert error_of(response)["type"] == "authentication_error"
    assert len(rig.primary_seen) == 0


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"{}", "application/json"),
        (b"[1]", "application/json"),
        (b"not json", "application/json"),
        (b'{"messages": [{"role": "user", "content": "x"}]}', "text/plain"),
        (
            b'{"messages": [{"role": "user", "content": "x"}], "stream_options": 1}',
            "application/json",
        ),
        (
            b'{"messages": [{"role": "user", "content": "' + b"x" * 9000 + b'"}]}',
            "application/json",
        ),
    ],
)
async def test_bad_requests_are_400(
    make_rig: RigFactory, content: bytes, content_type: str
) -> None:
    rig = await make_rig()
    response = await rig.gateway.post(
        "/v1/chat/completions",
        content=content,
        headers={"authorization": f"Bearer {TOKENS['tenant-a']}", "content-type": content_type},
    )
    assert response.status_code == 400
    assert error_of(response)["type"] == "invalid_request"
    assert len(rig.primary_seen) == 0


async def test_every_request_is_audited_with_counts_not_content(make_rig: RigFactory) -> None:
    rig = await make_rig()
    await rig.chat(model="mock-429", text="my secret prompt")
    (event,) = rig.audit_records()
    text = json.dumps(event)
    assert "my secret prompt" not in text
    assert "primary:rate_limited" in text
    assert "backup:ok" in text
