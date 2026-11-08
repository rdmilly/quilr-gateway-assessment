"""Task 3: an LLM gateway endpoint that redacts PII from the response as it streams.

Emails, US Social Security numbers and payment card numbers are replaced with
``[REDACTED]`` chunk by chunk, holding back only the few characters that could
still turn out to be the start of a match, so the first token is not delayed
and the full response is never held in memory.
"""

__version__ = "1.0.0"
