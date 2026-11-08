"""Redact PII from text that arrives in pieces, without waiting for the whole text.

The rule for each piece of text the redactor holds:

* Text that cannot be part of any match is released at once.
* A complete match that is followed by more text is final: it is replaced
  with ``[REDACTED]`` and released.
* A match that reaches the end of what has arrived so far, complete or not,
  might still grow (``john@ex.c`` may become ``john@ex.com``), so it is held
  back until the next piece decides it.
* Nothing is held longer than the longest possible match (256 characters).
  Past that, a held candidate cannot be PII, so its first character is
  released and the rest is looked at again.

So the memory used is bounded by 256 characters plus one incoming chunk, and
text is delayed only while it genuinely might be the start of an email, SSN or
card number, which is usually a few characters.

``feed`` and ``finish`` together produce exactly what ``redact`` produces on
the whole text at once, however the text is split. The tests check that
property on generated text and every way of splitting it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .patterns import KINDS, MAX_MATCH_CHARS, REDACTED

# Lookbehinds in the patterns read up to two characters before a match, so two
# characters of already-released text are kept as context.
_CONTEXT_CHARS = 2


@dataclass
class StreamRedactor:
    """Feed text in pieces; get back the part that is safe to send now."""

    max_holdback: int = MAX_MATCH_CHARS
    redactions: int = 0  # how many matches were replaced, for logs and metrics
    _context: str = ""
    _pending: str = ""
    held_high_water: int = field(default=0, init=False)

    def feed(self, text: str) -> str:
        self._pending += text
        out = self._scan(final=False)
        self.held_high_water = max(self.held_high_water, len(self._pending))
        return out

    def finish(self) -> str:
        """End of the stream: whatever is held is now final."""
        return self._scan(final=True)

    @property
    def held(self) -> int:
        return len(self._pending)

    def _scan(self, *, final: bool) -> str:
        out: list[str] = []
        text = self._context + self._pending
        pos = len(self._context)
        while pos < len(text):
            match = self._earliest(text, pos, final)
            if match is None:
                out.append(text[pos:])
                pos = len(text)
                break
            start, end, settled, kind_accepts = match
            if not settled:
                if len(text) - start > self.max_holdback:
                    # Too long to be PII: release one character and look again.
                    out.append(text[pos : start + 1])
                    pos = start + 1
                    continue
                out.append(text[pos:start])
                pos = start
                break
            if kind_accepts:
                out.append(text[pos:start])
                out.append(REDACTED)
                self.redactions += 1
                pos = end
            else:
                # Looked like a card number but is not one: release its first
                # character and keep scanning the rest.
                out.append(text[pos : start + 1])
                pos = start + 1
        released = "".join(out)
        self._pending = text[pos:]
        tail_source = text[:pos]
        self._context = tail_source[-_CONTEXT_CHARS:] if tail_source else self._context
        return released

    @staticmethod
    def _earliest(text: str, pos: int, final: bool) -> tuple[int, int, bool, bool] | None:
        """The leftmost candidate among all kinds: (start, end, settled, accepted).

        A candidate is *settled* when no more text can change it: either the
        stream has ended, or nothing from its start to the end of what has
        arrived could still be the beginning of a longer match. ``search`` alone
        is not enough for that: given ``a@mail.example.`` it reports the
        complete ``a@mail.example`` and not that the trailing dot may continue
        it, so a second, anchored check asks whether the rest could still grow.
        """
        best: tuple[int, int, bool, bool] | None = None
        for kind in KINDS:
            m = kind.pattern.search(text, pos, partial=not final)
            if m is None or (m.partial and m.start() == m.end()):
                # No match, or only "a match could start after the end": nothing to hold.
                continue
            could_grow = not final and (
                m.partial or kind.pattern.fullmatch(text, m.start(), partial=True) is not None
            )
            settled = not could_grow
            accepted = settled and kind.accepts(m.group())
            candidate = (m.start(), m.end(), settled, accepted)
            if best is None or candidate[0] < best[0]:
                best = candidate
            elif candidate[0] == best[0] and not settled and best[2]:
                # Same start: an unsettled candidate wins, so nothing is released early.
                best = candidate
        return best


def redact(text: str) -> str:
    """Redact a complete text in one go (used for non-streamed responses)."""
    redactor = StreamRedactor()
    return redactor.feed(text) + redactor.finish()


def redact_stream(pieces: Iterable[str]) -> str:
    redactor = StreamRedactor()
    return "".join(redactor.feed(p) for p in pieces) + redactor.finish()
