"""What counts as PII, as regular expressions that also work on unfinished text.

The ``regex`` module (not the standard ``re``) is used for one feature:
*partial matching*. ``search(text, partial=True)`` also reports a match that
reaches the end of the text and could still complete if more text arrived, such
as ``john@ex`` (which may become ``john@example.com``). That is what lets the
redactor release text safely while a stream is still arriving.

``\\d`` and ``\\w`` here match Unicode digits and letters, so full-width or
other-script digits (such as U+FF11, a full-width 1) are caught without a
normalization pass.

Choices, and their trade-offs:

* **Email:** a practical pattern (``local@domain.tld``), not the full RFC 5322
  grammar, which allows quoted strings and comments no real address uses.
* **SSN:** three, two and four digits separated by the same dash or space.
  Every such number is redacted, including ones the SSA would never issue
  (000, 666 or 9xx area), so made-up test numbers like ``999-99-9999`` do not
  leak. Nine digits with no separators are not treated as an SSN: too many
  order numbers and phone extensions look like that.
* **Card:** 13 to 19 digits, optionally split by single spaces or dashes. A
  number is redacted if it passes the Luhn checksum (as every real card
  number does), or if it is written in the common 4-4-4-4 grouping even
  without a valid checksum, so a made-up ``1234 5678 9012 3456`` is redacted
  too. Other long digit runs that fail Luhn, like order or tracking numbers,
  pass through.

Not covered, and documented as such: PII spelled out in words ("five five
five..."), deliberately obfuscated addresses ("john at example dot com"), and
names, phone numbers or street addresses, which the brief does not list.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Final

import regex

REDACTED: Final = "[REDACTED]"

EMAIL: Final = regex.compile(r"(?<![\w.+%-])[\w.+%-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})+")
# Digits that continue past a single space or dash belong to one longer number
# (a phone number, a tracking number), so neither pattern may start or stop there.
SSN: Final = regex.compile(r"(?<!\d[ -]?)\d{3}([- ])\d{2}\1\d{4}(?![ -]?\d)")
CARD: Final = regex.compile(r"(?<!\d[ -]?)\d(?:[ -]?\d){12,18}(?![ -]?\d)")
_FOUR_BY_FOUR: Final = regex.compile(r"\d{4}([ -])\d{4}\1\d{4}\1\d{4}")

# Longest text any pattern can match: an email address is at most 254 characters
# (RFC 5321). Holding back more than this can never complete a match.
MAX_MATCH_CHARS: Final = 256


def luhn_valid(number: str) -> bool:
    digits = [unicodedata.digit(ch) for ch in number if unicodedata.category(ch) == "Nd"]
    total = 0
    for position, digit in enumerate(reversed(digits)):
        if position % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return len(digits) >= 13 and total % 10 == 0


def is_card(candidate: str) -> bool:
    return luhn_valid(candidate) or _FOUR_BY_FOUR.fullmatch(candidate) is not None


@dataclass(frozen=True, slots=True)
class Kind:
    name: str
    pattern: regex.Pattern[str]

    def accepts(self, text: str) -> bool:
        return self.name != "card" or is_card(text)


KINDS: Final = (Kind("email", EMAIL), Kind("ssn", SSN), Kind("card", CARD))
