"""Logging for every task: one JSON object per line, written to stderr, with secrets blanked.

stderr, never stdout: Task 1's stdout may carry nothing but MCP messages.
Blanking happens here, once, so no task can forget to do it.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final, TextIO

REDACTED: Final = "[REDACTED]"

# Things that look like credentials, wherever they appear in a message or a field.
_SECRET_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"), rf"\1 {REDACTED}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), REDACTED),  # JWTs
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}"), REDACTED),  # provider API keys
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), REDACTED),
)

# Field names whose values are always blanked, whatever the value looks like.
_SECRET_KEYS: Final = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "api_key",
        "apikey",
        "x-api-key",
        "token",
        "access_token",
        "password",
        "secret",
        "cookie",
        "set-cookie",
    }
)

_CORE_KEYS: Final = frozenset({"ts", "level", "logger", "msg", "exc"})


def redact(text: str) -> str:
    """Blank anything in a string that looks like a credential."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrub(value: Any, key: str | None = None) -> Any:
    """Return a copy of a log field with secrets blanked, at any depth."""
    if key is not None and key.lower() in _SECRET_KEYS:
        return REDACTED
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {str(k): scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    """Formats each record as one line of JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            for key, value in fields.items():
                # Extra fields never overwrite the core ones, so a log line can't be forged
                # by passing a field called "level" or "msg".
                if str(key) not in _CORE_KEYS:
                    payload[str(key)] = scrub(value, str(key))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        # ensure_ascii keeps every line plain ASCII, and json.dumps escapes newlines, so one
        # event is always exactly one line, whatever the input contained.
        return json.dumps(payload, ensure_ascii=True, default=str)


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Send all logging to stderr (or `stream`, for tests) as JSON lines. Safe to call again."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)


def log_event(logger: logging.Logger, level: int, message: str, /, **fields: Any) -> None:
    """Log a message with structured fields, which come out as top-level JSON keys.

    The first three arguments are positional-only, so any field name can be passed, even
    "level" or "message"; core keys like those are then dropped by the formatter.
    """
    logger.log(level, message, extra={"fields": fields})
