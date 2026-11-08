"""Redact the parts of a chat completion a person or another system will read.

Streamed responses arrive as ``chat.completion.chunk`` objects. Each choice
(``n`` can be more than one) has its own text stream, and each tool call
inside a choice has its own stream of argument text, so each gets its own
``StreamRedactor``: PII split between two chunks of the same stream is
caught, and text from different streams is never mixed.

When a choice finishes (``finish_reason`` is set), anything still held for it
is released into that same chunk, before the finish. If the upstream stream
ends without finishing a choice, ``leftovers`` releases the rest in one extra
chunk.

Fields redacted: ``content`` and tool call ``arguments`` (the model may put a
customer's email straight into a ``send_email`` call). Other fields (roles,
ids, finish reasons, usage) carry no free text from the model.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .redactor import StreamRedactor, redact


@dataclass
class _Choice:
    content: StreamRedactor = field(default_factory=StreamRedactor)
    tools: dict[int, StreamRedactor] = field(default_factory=dict)
    finished: bool = False

    def tool(self, index: int) -> StreamRedactor:
        return self.tools.setdefault(index, StreamRedactor())

    def drain(self, delta: dict[str, Any]) -> None:
        """Release everything still held for this choice into ``delta``."""
        rest = self.content.finish()
        if rest:
            delta["content"] = (delta.get("content") or "") + rest
        for index, redactor in self.tools.items():
            rest = redactor.finish()
            if not rest:
                continue
            calls = delta.setdefault("tool_calls", [])
            for call in calls:
                if isinstance(call, dict) and call.get("index", 0) == index:
                    function = call.setdefault("function", {})
                    function["arguments"] = (function.get("arguments") or "") + rest
                    break
            else:
                calls.append({"index": index, "function": {"arguments": rest}})
        self.finished = True


def _index(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass
class CompletionStreamRedactor:
    _choices: dict[int, _Choice] = field(default_factory=dict)
    _template: dict[str, Any] | None = None

    @property
    def redactions(self) -> int:
        return sum(
            c.content.redactions + sum(t.redactions for t in c.tools.values())
            for c in self._choices.values()
        )

    def process(self, chunk: dict[str, Any]) -> dict[str, Any]:
        """Redact one chunk in place and return it."""
        if self._template is None:
            self._template = {
                k: v for k, v in chunk.items() if k in ("id", "object", "created", "model")
            }
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            state = self._choices.setdefault(_index(choice.get("index")), _Choice())
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = {}
                choice["delta"] = delta
            if isinstance(delta.get("content"), str):
                delta["content"] = state.content.feed(delta["content"])
            for call in delta.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                    function["arguments"] = state.tool(_index(call.get("index"))).feed(
                        function["arguments"]
                    )
            if choice.get("finish_reason") is not None and not state.finished:
                state.drain(delta)
        return chunk

    def leftovers(self) -> dict[str, Any] | None:
        """A final chunk with whatever is still held for unfinished choices, or None."""
        choices = []
        for index, state in sorted(self._choices.items()):
            if state.finished:
                continue
            delta: dict[str, Any] = {}
            state.drain(delta)
            if delta:
                choices.append({"index": index, "delta": delta, "finish_reason": None})
        if not choices:
            return None
        chunk = copy.deepcopy(self._template or {"object": "chat.completion.chunk"})
        chunk["choices"] = choices
        return chunk


def redact_completion(body: dict[str, Any]) -> int:
    """Redact a non-streamed completion in place; return how many values were replaced."""
    count = 0
    for choice in body.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        targets: list[tuple[dict[str, Any], str]] = []
        message = choice.get("message")
        if isinstance(message, dict):
            targets.append((message, "content"))
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict):
                    targets.append((function, "arguments"))
        targets.append((choice, "text"))  # the older completions API puts text here
        for holder, key in targets:
            value = holder.get(key)
            if isinstance(value, str):
                redacted = redact(value)
                count += redacted.count("[REDACTED]") - value.count("[REDACTED]")
                holder[key] = redacted
    return count
