"""How much time the redactor adds per streamed chunk.

    uv run python -m task3_pii_guardrail.bench

Feeds a long, realistic answer (prose with an email, an SSN and a card number
every few sentences) through the redactor in 4-character chunks, about the
size of one model token, and reports the cost per chunk and the most text
ever held back.
"""

from __future__ import annotations

import statistics
import time

from .redactor import StreamRedactor

SENTENCE = (
    "Thanks for reaching out about your order from last Tuesday. "
    "I have updated the account for jane.doe@example.com, verified SSN 123-45-6789 "
    "and the card ending 4111 1111 1111 1111, and the refund should post in 3 to 5 days. "
)


def run(repeats: int = 400, chunk_chars: int = 4) -> dict[str, float]:
    text = SENTENCE * repeats
    chunks = [text[i : i + chunk_chars] for i in range(0, len(text), chunk_chars)]
    redactor = StreamRedactor()
    timings: list[float] = []
    started = time.perf_counter()
    for chunk in chunks:
        t0 = time.perf_counter()
        redactor.feed(chunk)
        timings.append(time.perf_counter() - t0)
    redactor.finish()
    total = time.perf_counter() - started
    timings.sort()
    return {
        "chunks": len(chunks),
        "characters": len(text),
        "median_us_per_chunk": statistics.median(timings) * 1e6,
        "p99_us_per_chunk": timings[int(len(timings) * 0.99)] * 1e6,
        "chars_per_second": len(text) / total,
        "max_chars_held": redactor.held_high_water,
        "redactions": redactor.redactions,
    }


if __name__ == "__main__":
    for key, value in run().items():
        print(f"{key:>22}: {value:,.1f}")
