"""Send a completion request to the primary provider, falling over to the backup.

The rule, from the brief: if the primary answers ``429 Too Many Requests`` or has not
answered within 3000 ms, send the same request to the backup.

What "answered" means is the timing decision that matters:

* Plain request: the whole response body must arrive inside the timeout.
* Streamed request: the response headers *and the first chunk* must arrive inside it. A
  provider that sends ``200 OK`` and then nothing would otherwise pass the check and hang.

Once a streamed answer has started reaching the client it stays with that provider. Moving
mid-stream would send the client the start of two different answers.

Race conditions, and how each is closed:

* The timeout and the response race. ``anyio.fail_after`` wraps every step of an attempt, so
  when it fires, whatever was in progress (connect, headers, first chunk) is cancelled, and
  the half-open response is closed before the backup is tried. Only one answer ever reaches
  the client, and no connection is left open behind it.
* The client gives up mid-attempt. The response is closed on every exit path, including
  cancellation.
* Billing. The router never touches the rate limiter, so a failover cannot charge twice.
  The endpoint reserves once before routing and settles once after (see app.py).

Nothing from a provider's error response is read: status code and ``Retry-After`` only.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

import anyio
import httpx2


class Failure(StrEnum):
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Attempt:
    """One try at one provider, for logs and the audit record. Holds no response content."""

    provider: str
    outcome: str  # "ok" or a Failure value
    status: int | None
    latency_ms: float


@dataclass
class Opened:
    """A provider that answered 200 in time. The caller owns ``response`` and must close it."""

    provider: Provider
    response: httpx2.Response
    attempts: list[Attempt]
    body: bytes | None = None  # plain requests: the whole body
    first_chunk: bytes = b""  # streams: the chunk that arrived inside the timeout
    rest: AsyncIterator[bytes] | None = None  # streams: everything after it


class AllProvidersFailed(Exception):
    """No provider answered. ``failure`` is the last one's, which decides the error shown."""

    def __init__(self, attempts: list[Attempt], failure: Failure, retry_after_s: int | None):
        super().__init__(failure.value)
        self.attempts = attempts
        self.failure = failure
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class _Failed:
    attempt: Attempt
    failure: Failure
    retry_after_s: int | None


def _retry_after(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else None


async def _read_limited(stream: AsyncIterator[bytes], limit: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    async for chunk in stream:
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


class Router:
    def __init__(
        self,
        client: httpx2.AsyncClient,
        providers: Sequence[Provider],
        *,
        timeout_s: float = 3.0,
        failover_on_errors: bool = False,
        max_response_bytes: int = 8 * 1_048_576,
    ) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self._client = client
        self._providers = tuple(providers)
        self._timeout_s = timeout_s
        self._failover_on_errors = failover_on_errors
        self._max_response_bytes = max_response_bytes

    async def open(self, content: bytes, *, streaming: bool) -> Opened:
        attempts: list[Attempt] = []
        for index, provider in enumerate(self._providers):
            result = await self._attempt(provider, content, streaming)
            if isinstance(result, Opened):
                attempts.append(result.attempts[0])
                result.attempts = attempts
                return result
            attempts.append(result.attempt)
            last = index == len(self._providers) - 1
            if last or not self._fails_over(result.failure):
                raise AllProvidersFailed(attempts, result.failure, result.retry_after_s)
        raise AssertionError("unreachable")  # pragma: no cover

    def _fails_over(self, failure: Failure) -> bool:
        return failure is not Failure.ERROR or self._failover_on_errors

    def _request(self, provider: Provider, content: bytes, streaming: bool) -> httpx2.Request:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream" if streaming else "application/json",
        }
        if provider.api_key:
            headers["authorization"] = f"Bearer {provider.api_key}"
        return self._client.build_request("POST", provider.url, content=content, headers=headers)

    async def _attempt(
        self, provider: Provider, content: bytes, streaming: bool
    ) -> Opened | _Failed:
        started = time.perf_counter()

        def done(outcome: str, status: int | None) -> Attempt:
            latency = round((time.perf_counter() - started) * 1000, 3)
            return Attempt(provider.name, outcome, status, latency)

        response: httpx2.Response | None = None
        handed_over = False
        try:
            try:
                with anyio.fail_after(self._timeout_s):
                    response = await self._client.send(
                        self._request(provider, content, streaming), stream=True
                    )
                    status = response.status_code
                    if status != 200:
                        failure = Failure.RATE_LIMITED if status == 429 else Failure.ERROR
                        retry = _retry_after(response.headers.get("retry-after"))
                        return _Failed(done(failure.value, status), failure, retry)
                    if streaming:
                        rest = response.aiter_raw()
                        first = await anext(rest, b"")
                        handed_over = True
                        return Opened(
                            provider, response, [done("ok", 200)], first_chunk=first, rest=rest
                        )
                    body = await _read_limited(response.aiter_bytes(), self._max_response_bytes)
            except (TimeoutError, httpx2.TimeoutException):
                return _Failed(done(Failure.TIMEOUT.value, None), Failure.TIMEOUT, None)
            except httpx2.HTTPError:
                return _Failed(done(Failure.ERROR.value, None), Failure.ERROR, None)
            if body is None:  # bigger than we are willing to hold
                return _Failed(done(Failure.ERROR.value, 200), Failure.ERROR, None)
            return Opened(provider, response, [done("ok", 200)], body=body)
        finally:
            # Every path but a live stream closes the response here, cancellation included.
            if response is not None and not handed_over:
                with anyio.CancelScope(shield=True):
                    await response.aclose()
