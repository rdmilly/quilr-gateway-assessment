"""A gateway in front of the mock MCP server, both in memory: no ports, no network."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator

import httpx2
import pytest

from mocks.mcp_server import create_app as create_mock
from shared.audit import AuditLog
from shared.auth import Authenticator, AuthSettings, Role, TokenEntry, hash_token
from task2_mcp_gateway.app import create_app
from task2_mcp_gateway.settings import GatewaySettings
from tests.task2.support import ALLOWED_ORIGIN, TOKENS, Rig


@pytest.fixture
def authenticator() -> Authenticator:
    return Authenticator(
        AuthSettings(
            tokens=(
                TokenEntry(sha256=hash_token(TOKENS["admin"]), subject="alice", role=Role.ADMIN),
                TokenEntry(sha256=hash_token(TOKENS["viewer"]), subject="victor", role=Role.VIEWER),
            )
        )
    )


@pytest.fixture(params=[False, True], ids=["sse", "json"])
async def rig(request: pytest.FixtureRequest, authenticator: Authenticator) -> AsyncIterator[Rig]:
    seen: list[dict[str, str]] = []
    mock = create_mock(seen, json_response=request.param)
    # The SDK app's lifespan must start and stop in the same task, and pytest-asyncio
    # tears fixtures down in another one, so a background task holds it open.
    ready, done = asyncio.Event(), asyncio.Event()

    async def hold_lifespan() -> None:
        async with mock.router.lifespan_context(mock):
            ready.set()
            await done.wait()

    holder = asyncio.create_task(hold_lifespan())
    await ready.wait()
    downstream = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=mock), base_url="http://127.0.0.1"
    )
    sink = io.StringIO()
    settings = GatewaySettings(
        downstream_url="http://127.0.0.1/mcp",
        max_body_bytes=4096,
        allowed_origins=(ALLOWED_ORIGIN,),
    )
    app = create_app(settings, authenticator, client=downstream, audit=AuditLog(sink=sink))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://gateway"
    ) as gateway:
        yield Rig(gateway, app, seen, sink)
    await downstream.aclose()
    done.set()
    await holder
