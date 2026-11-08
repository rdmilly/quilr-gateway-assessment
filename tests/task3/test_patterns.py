"""Which strings count as PII."""

from __future__ import annotations

import pytest

from task3_pii_guardrail.patterns import is_card, luhn_valid
from task3_pii_guardrail.redactor import redact


@pytest.mark.parametrize(
    "text",
    [
        "john@example.com",
        "john.doe+tag@mail.example.co.uk",
        "A_B-c%d@sub-domain.example.org",
        "élodie@exemple.fr",  # accented local part
        "123-45-6789",
        "123 45 6789",
        "999-99-9999",  # never issued by the SSA, still redacted
        "078-05-1120",
        "\uff11\uff12\uff13-45-6789",  # full-width digits
        "4111111111111111",  # Visa test number
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "378282246310005",  # Amex, 15 digits
        "6011 1111 1111 1117",
        "1234 5678 9012 3456",  # fails Luhn, but 4-4-4-4 grouping
    ],
)
def test_pii_is_redacted(text: str) -> None:
    assert redact(f"before {text} after") == "before [REDACTED] after"


@pytest.mark.parametrize(
    "text",
    [
        "not an email: john@",
        "@handle",
        "user@localhost",  # no dot in the domain
        "call 555-0100",
        "123-456-7890",  # phone layout, not 3-2-4
        "123-45-6789-0",  # a longer number that merely contains the shape
        "123-45 6789",  # mixed separators
        "123456789",  # nine bare digits: not treated as an SSN
        "order 1234567890123",  # 13 digits, fails Luhn
        "tracking 9400 1000 0000 0000 0000 00",  # too long a run
        "version 1.2.3, 2026-11-08, $1,234.56",
    ],
)
def test_lookalikes_are_left_alone(text: str) -> None:
    assert redact(text) == text


def test_luhn() -> None:
    assert luhn_valid("4111111111111111")
    assert luhn_valid("79927398713") is False  # valid checksum but too short for a card
    assert not luhn_valid("4111111111111112")
    assert luhn_valid("\uff14111111111111111")  # full-width digit counts


def test_card_rule() -> None:
    assert is_card("4111 1111 1111 1111")
    assert is_card("1234-5678-9012-3456")
    assert not is_card("1234567890123456")  # ungrouped and fails Luhn
    assert not is_card("1234 5678-9012 3456")  # inconsistent grouping, fails Luhn


def test_several_in_one_text() -> None:
    text = "a@b.co, 123-45-6789 and 4111111111111111; c@d.io"
    assert redact(text) == "[REDACTED], [REDACTED] and [REDACTED]; [REDACTED]"
