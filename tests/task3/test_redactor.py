"""The streaming redactor: same answer however the text is split, bounded memory, early release."""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from task3_pii_guardrail.patterns import MAX_MATCH_CHARS
from task3_pii_guardrail.redactor import StreamRedactor, redact, redact_stream

EMAILS = ["john@example.com", "a.b+c@mail.example.org", "x@y.io"]
SSNS = ["123-45-6789", "078 05 1120", "999-99-9999"]
CARDS = ["4111111111111111", "4111 1111 1111 1111", "1234-5678-9012-3456", "378282246310005"]
PII = EMAILS + SSNS + CARDS

pieces = st.one_of(
    st.sampled_from(PII),
    st.sampled_from([" ", ", ", ". ", "\n", "@", "-", "1", "12", "a", "the ", "email: "]),
    st.text(alphabet=st.characters(codec="utf-8"), max_size=8),  # utf-8: no lone surrogates
)
texts = st.lists(pieces, max_size=25).map("".join)


def split(text: str, cuts: list[int]) -> list[str]:
    points = sorted({c % (len(text) + 1) for c in cuts})
    bounds = [0, *points, len(text)]
    return [text[a:b] for a, b in itertools.pairwise(bounds)]


@settings(max_examples=400, deadline=None)
@given(texts, st.lists(st.integers(min_value=0, max_value=10_000), max_size=30))
def test_streaming_gives_the_same_answer_however_the_text_is_split(
    text: str, cuts: list[int]
) -> None:
    assert redact_stream(split(text, cuts)) == redact(text)


@settings(max_examples=200, deadline=None)
@given(texts)
def test_one_character_at_a_time_gives_the_same_answer(text: str) -> None:
    assert redact_stream(list(text)) == redact(text)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(PII), min_size=1, max_size=5), st.data())
def test_pii_never_survives_in_any_split(values: list[str], data: st.DataObject) -> None:
    text = " | ".join(values) + " end"
    cuts = data.draw(st.lists(st.integers(min_value=0, max_value=len(text)), max_size=20))
    out = redact_stream(split(text, cuts))
    for value in values:
        assert value not in out
    assert out.count("[REDACTED]") == len(values)


@settings(max_examples=200, deadline=None)
@given(st.text(alphabet="ab.@-_1", min_size=0, max_size=2000))
def test_held_text_never_exceeds_the_cap(text: str) -> None:
    redactor = StreamRedactor()
    for ch in text:
        redactor.feed(ch)
        assert redactor.held <= MAX_MATCH_CHARS + 1


def test_text_without_pii_candidates_is_released_immediately() -> None:
    redactor = StreamRedactor()
    assert redactor.feed("Hello, ") == "Hello, "
    assert redactor.feed("how are you? ") == "how are you? "
    assert redactor.held == 0


def test_only_a_possible_match_is_held() -> None:
    redactor = StreamRedactor()
    assert redactor.feed("Write to jo") == "Write to "
    assert redactor.feed("hn@exam") == ""
    assert redactor.feed("ple.com today") == "[REDACTED] "
    assert redactor.finish() == "today"
    assert redactor.redactions == 1


def test_a_card_number_split_across_many_chunks() -> None:
    chunks = ["card: 41", "11 11", "11 1", "111 11", "11, thanks"]
    assert redact_stream(chunks) == "card: [REDACTED], thanks"


def test_long_digit_runs_do_not_grow_memory() -> None:
    redactor = StreamRedactor()
    out = "".join(redactor.feed("1") for _ in range(10_000)) + redactor.finish()
    assert out == "1" * 10_000
    assert redactor.held_high_water <= MAX_MATCH_CHARS + 1


@pytest.mark.parametrize("ending", ["john@example.com", "123-45-6789", "4111111111111111"])
def test_pii_at_the_very_end_is_redacted_on_finish(ending: str) -> None:
    redactor = StreamRedactor()
    out = redactor.feed("value: ") + redactor.feed(ending) + redactor.finish()
    assert out == "value: [REDACTED]"


def test_benchmark_runs_and_reports() -> None:
    from task3_pii_guardrail.bench import run

    result = run(repeats=5)
    assert result["redactions"] == 15
    assert result["max_chars_held"] <= MAX_MATCH_CHARS
