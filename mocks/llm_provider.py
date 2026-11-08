"""A fake OpenAI-compatible model provider for Tasks 3 and 4.

``POST /v1/chat/completions`` answers by echoing the last user message, so a
test controls exactly what the "model" says. The model name picks a behavior:

* ``mock-echo`` (or anything else): echo, streamed or not.
* ``mock-tool``: stream a tool call whose arguments are the echoed text.
* ``mock-429``: refuse with 429 Too Many Requests and a ``Retry-After``.
* ``mock-500``: fail with a body full of internal detail (a stack trace and a
  file path), so the gateway's error cleaning can be tested.
* ``mock-stall``: wait before answering, longer than any sane timeout.

An extra body field shapes the stream, since a gateway forwards the body but
not arbitrary headers: ``"mock": {"chunk_chars": 3, "delay_ms": 0}`` sets the
characters per chunk and the pause between chunks.

    uv run python -m mocks.llm_provider      # 127.0.0.1:9100 ($MOCK_LLM_PORT)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

STALL_S = 30.0


def _echo_text(body: dict[str, Any]) -> str:
    for message in reversed(body.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content)
    return ""


def _usage(body: dict[str, Any], text: str) -> dict[str, int]:
    prompt = sum(len(str(m.get("content", ""))) for m in body.get("messages") or []) // 4 + 1
    completion = len(text) // 4 + 1
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def create_app(name: str = "mock-provider", seen: list[dict[str, Any]] | None = None) -> Starlette:
    """``name`` labels responses (``id``), so tests can tell a primary from a backup."""

    async def completions(request: Request) -> Response:
        body = await request.json()
        if seen is not None:
            seen.append({"headers": dict(request.headers), "body": body})
        model = str(body.get("model", "mock-echo"))
        if model == "mock-429":
            return JSONResponse(
                {
                    "error": {
                        "message": f"Rate limit reached for {name} org-123 on tokens",
                        "type": "rate_limit_error",
                    }
                },
                429,
                headers={"retry-after": "7"},
            )
        if model == "mock-500":
            return Response(
                f'Traceback (most recent call last):\n  File "/srv/{name}/inference.py", '
                "line 88, in generate\nRuntimeError: CUDA out of memory (db=10.0.3.7)",
                500,
            )
        if model == "mock-stall":
            await asyncio.sleep(STALL_S)

        text = _echo_text(body)
        controls = body.get("mock") if isinstance(body.get("mock"), dict) else {}
        chunk_chars = max(1, int(controls.get("chunk_chars", 3)))
        delay_s = int(controls.get("delay_ms", 0)) / 1000
        n = max(1, int(body.get("n") or 1))
        created = int(time.time())
        completion_id = f"chatcmpl-{name}"
        tool = model == "mock-tool"

        if not body.get("stream"):
            message: dict[str, Any] = {"role": "assistant", "content": None if tool else text}
            if tool:
                message["tool_calls"] = [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "send_email", "arguments": text},
                    }
                ]
            return JSONResponse(
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": i,
                            "message": message,
                            "finish_reason": "tool_calls" if tool else "stop",
                        }
                        for i in range(n)
                    ],
                    "usage": _usage(body, text),
                }
            )

        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        def frame(choices: list[dict[str, Any]], **extra: Any) -> bytes:
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": choices,
                **extra,
            }
            return f"data: {json.dumps(chunk)}\n\n".encode()

        async def stream() -> AsyncIterator[bytes]:
            yield frame(
                [
                    {
                        "index": i,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                    for i in range(n)
                ]
            )
            for start in range(0, len(text), chunk_chars):
                piece = text[start : start + chunk_chars]
                for i in range(n):
                    if tool:
                        call: dict[str, Any] = {"index": 0, "function": {"arguments": piece}}
                        if start == 0:
                            call.update({"id": "call_1", "type": "function"})
                            call["function"]["name"] = "send_email"
                        delta: dict[str, Any] = {"tool_calls": [call]}
                    else:
                        delta = {"content": piece}
                    yield frame([{"index": i, "delta": delta, "finish_reason": None}])
                if delay_s:
                    await asyncio.sleep(delay_s)
            for i in range(n):
                yield frame(
                    [{"index": i, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]
                )
            if include_usage:
                yield frame([], usage=_usage(body, text))
            yield b"data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])


if __name__ == "__main__":
    import os

    import uvicorn

    from shared.logs import configure_logging

    configure_logging()
    port = int(os.environ.get("MOCK_LLM_PORT", "9100"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port, access_log=False, log_config=None)
