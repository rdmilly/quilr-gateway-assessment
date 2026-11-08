"""The SSE reader, cut at every possible byte."""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shared.sse import EventTooLarge, SSEReader

STREAM = (
    ": comment line\n"
    'data: {"a": "café ☃"}\n\n'
    "event: message\r\n"
    "data: line one\r\n"
    "data: line two\r\n\r\n"
    "data:no-space\r\r"
    "id: 7\n"
    "data: [DONE]\n\n"
).encode()
EXPECTED = ['{"a": "café ☃"}', "line one\nline two", "no-space", "[DONE]"]


def read_all(pieces: list[bytes]) -> list[str]:
    reader = SSEReader()
    events: list[str] = []
    for piece in pieces:
        events += reader.feed(piece)
    return events + reader.finish()


def test_whole_stream() -> None:
    assert read_all([STREAM]) == EXPECTED


def test_one_byte_at_a_time_including_inside_utf8_characters() -> None:
    assert read_all([STREAM[i : i + 1] for i in range(len(STREAM))]) == EXPECTED


@given(st.lists(st.integers(min_value=0, max_value=len(STREAM)), max_size=12))
def test_any_split(cuts: list[int]) -> None:
    bounds = [0, *sorted(set(cuts)), len(STREAM)]
    assert read_all([STREAM[a:b] for a, b in itertools.pairwise(bounds)]) == EXPECTED


def test_last_event_without_blank_line_still_counts() -> None:
    assert read_all([b"data: tail"]) == ["tail"]


def test_oversized_event_is_refused() -> None:
    reader = SSEReader()
    with pytest.raises(EventTooLarge):
        reader.feed(b"data: " + b"x" * 1_100_000 + b"\n")
    with pytest.raises(EventTooLarge):
        SSEReader().feed(b"x" * 1_100_000)  # a line that never ends
