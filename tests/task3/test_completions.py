"""Redacting chat completion chunks: per choice, per tool call, flushed on finish."""

from __future__ import annotations

from typing import Any

from task3_pii_guardrail.completions import CompletionStreamRedactor, redact_completion


def chunk(index: int, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": index, "delta": delta, "finish_reason": finish}],
    }


def content_of(chunks: list[dict[str, Any]], index: int = 0) -> str:
    return "".join(
        c["delta"].get("content") or ""
        for ch in chunks
        for c in ch["choices"]
        if c["index"] == index
    )


def test_choices_are_kept_apart() -> None:
    r = CompletionStreamRedactor()
    out = [
        r.process(chunk(0, {"content": "mail jo"})),
        r.process(chunk(1, {"content": "hn@ex"})),  # different choice: not joined with choice 0
        r.process(chunk(0, {"content": "hn@example.com ok"})),
        r.process(chunk(1, {"content": " plain"}, "stop")),
        r.process(chunk(0, {}, "stop")),
    ]
    assert content_of(out, 0) == "mail [REDACTED] ok"
    assert content_of(out, 1) == "hn@ex plain"
    assert r.redactions == 1


def test_held_text_is_released_before_finish() -> None:
    r = CompletionStreamRedactor()
    first = r.process(chunk(0, {"content": "card 4111 1111 1111 1111"}))
    assert first["choices"][0]["delta"]["content"] == "card "
    last = r.process(chunk(0, {}, "stop"))
    assert last["choices"][0]["delta"]["content"] == "[REDACTED]"
    assert last["choices"][0]["finish_reason"] == "stop"


def test_tool_call_arguments_are_redacted() -> None:
    r = CompletionStreamRedactor()
    calls = [
        {"index": 0, "id": "call_1", "function": {"name": "send_email", "arguments": '{"to": "a'}},
        {"index": 0, "function": {"arguments": 'nn@corp.example.com"}'}},
    ]
    out = [r.process(chunk(0, {"tool_calls": [c]})) for c in calls]
    out.append(r.process(chunk(0, {}, "tool_calls")))
    args = "".join(
        tc["function"].get("arguments", "")
        for ch in out
        for c in ch["choices"]
        for tc in c["delta"].get("tool_calls", [])
    )
    assert args == '{"to": "[REDACTED]"}'


def test_leftovers_when_the_stream_never_finishes() -> None:
    r = CompletionStreamRedactor()
    r.process(chunk(0, {"content": "SSN 123-45-6789"}))
    tail = r.leftovers()
    assert tail is not None
    assert tail["choices"][0]["delta"]["content"] == "[REDACTED]"
    assert tail["id"] == "c1"
    assert r.leftovers() is None  # nothing twice


def test_malformed_chunks_do_not_break_it() -> None:
    r = CompletionStreamRedactor()
    weird: dict[str, Any] = {"choices": ["not a dict", {"index": True, "delta": None}]}
    assert r.process(weird)["choices"][1]["delta"] == {}
    assert r.process({"usage": {"total_tokens": 3}}) == {"usage": {"total_tokens": 3}}


def test_non_streamed_completion() -> None:
    body: dict[str, Any] = {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "a@b.co and 078-05-1120"}},
            {
                "index": 1,
                "message": {"tool_calls": [{"function": {"arguments": "4111111111111111"}}]},
            },
            {"index": 2, "text": "legacy x@y.io"},
        ]
    }
    assert redact_completion(body) == 4
    assert body["choices"][0]["message"]["content"] == "[REDACTED] and [REDACTED]"
    assert body["choices"][1]["message"]["tool_calls"][0]["function"]["arguments"] == "[REDACTED]"
    assert body["choices"][2]["text"] == "legacy [REDACTED]"
