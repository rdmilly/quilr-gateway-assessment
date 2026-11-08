"""The pre-flight token estimate: prompt bytes / 4, plus the completion cap per choice."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from task4_rate_limit_failover.estimate import InvalidRequest, estimate_tokens

MESSAGES = [{"role": "user", "content": "hello there"}]


def prompt_tokens(body: dict[str, Any]) -> int:
    text = json.dumps(body["messages"], ensure_ascii=False) + json.dumps(
        body.get("tools") or [], ensure_ascii=False
    )
    return math.ceil(len(text.encode()) / 4)


def test_uses_default_completion_cap_when_none_given() -> None:
    body = {"messages": MESSAGES}
    assert estimate_tokens(body, 1024) == prompt_tokens(body) + 1024


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_uses_the_requested_cap(field: str) -> None:
    body = {"messages": MESSAGES, field: 50}
    assert estimate_tokens(body, 1024) == prompt_tokens(body) + 50


def test_max_completion_tokens_wins_over_max_tokens() -> None:
    body = {"messages": MESSAGES, "max_completion_tokens": 10, "max_tokens": 999}
    assert estimate_tokens(body, 1024) == prompt_tokens(body) + 10


def test_tools_count_toward_the_prompt() -> None:
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    plain = estimate_tokens({"messages": MESSAGES, "max_tokens": 1}, 0)
    with_tools = estimate_tokens({"messages": MESSAGES, "tools": tools, "max_tokens": 1}, 0)
    assert with_tools > plain


def test_each_choice_is_charged() -> None:
    one = estimate_tokens({"messages": MESSAGES, "max_tokens": 100}, 0)
    three = estimate_tokens({"messages": MESSAGES, "max_tokens": 100, "n": 3}, 0)
    assert three - one == 200


def test_non_ascii_text_is_not_undercounted() -> None:
    # Three bytes per character: the estimate must not fall below one token per character.
    text = "".join(chr(0x4E00 + i) for i in range(400))
    body = {"messages": [{"role": "user", "content": text}], "max_tokens": 1}
    assert estimate_tokens(body, 0) >= 300


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"messages": []},
        {"messages": "hi"},
        {"messages": MESSAGES, "tools": "x"},
        {"messages": MESSAGES, "max_tokens": 0},
        {"messages": MESSAGES, "max_tokens": -5},
        {"messages": MESSAGES, "max_tokens": 1.5},
        {"messages": MESSAGES, "max_tokens": True},
        {"messages": MESSAGES, "max_tokens": "100"},
        {"messages": MESSAGES, "n": 0},
        {"messages": MESSAGES, "n": 129},
        {"messages": MESSAGES, "n": False},
    ],
)
def test_rejects_bodies_it_cannot_price(body: dict[str, Any]) -> None:
    with pytest.raises(InvalidRequest):
        estimate_tokens(body, 1024)


def test_null_n_means_one() -> None:
    body = {"messages": MESSAGES, "max_tokens": 7, "n": None}
    assert estimate_tokens(body, 0) == prompt_tokens(body) + 7
