"""Logs: JSON lines on stderr only, secrets blanked, one event per line."""

import io
import json
import logging
import sys
from collections.abc import Iterator

import pytest

from shared.logs import REDACTED, configure_logging, log_event, redact, scrub


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture
def buffer(restore_root_logger: None) -> io.StringIO:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    return stream


def _lines(stream: io.StringIO) -> list[str]:
    return stream.getvalue().splitlines()


def test_by_default_logs_go_to_stderr_and_never_stdout(
    restore_root_logger: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_out, fake_err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)
    configure_logging()
    logging.getLogger("test").warning("hello")
    assert fake_out.getvalue() == ""
    record = json.loads(fake_err.getvalue())
    assert record["msg"] == "hello"
    assert record["level"] == "warning"


def test_calling_configure_twice_leaves_one_handler(restore_root_logger: None) -> None:
    configure_logging(stream=io.StringIO())
    configure_logging(stream=io.StringIO())
    assert len(logging.getLogger().handlers) == 1


def test_A0_5_bearer_token_in_a_message_is_blanked(buffer: io.StringIO) -> None:
    logging.getLogger("test").info("got header Bearer abc.def-123")
    line = _lines(buffer)[0]
    assert "abc.def-123" not in line
    assert json.loads(line)["msg"] == f"got header Bearer {REDACTED}"


def test_A2_7_authorization_field_is_blanked_whatever_it_holds(buffer: io.StringIO) -> None:
    log_event(logging.getLogger("test"), logging.INFO, "request", authorization="x", path="/mcp")
    record = json.loads(_lines(buffer)[0])
    assert record["authorization"] == REDACTED
    assert record["path"] == "/mcp"


@pytest.mark.parametrize(
    "secret",
    [
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl",
        "sk-proj-abcdefghijklmnop",
        "ghp_abcdefghijklmnopqrstuvwxyz0123",
        "github_pat_abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_jwts_and_api_keys_are_blanked(secret: str) -> None:
    assert secret not in redact(f"value={secret} end")


def test_secrets_are_blanked_inside_nested_fields() -> None:
    cleaned = scrub({"headers": {"Authorization": "x", "Accept": "json"}, "notes": ["Bearer y"]})
    assert cleaned == {
        "headers": {"Authorization": REDACTED, "Accept": "json"},
        "notes": [f"Bearer {REDACTED}"],
    }


def test_A0_7_newlines_in_input_cannot_forge_extra_log_lines(buffer: io.StringIO) -> None:
    logging.getLogger("test").info('ok\n{"level": "info", "msg": "forged admin login"}')
    assert len(_lines(buffer)) == 1


def test_A1_18_lone_surrogate_does_not_break_the_log_line(buffer: io.StringIO) -> None:
    log_event(logging.getLogger("test"), logging.WARNING, "bad input", reason="refund \ud800")
    lines = _lines(buffer)
    assert len(lines) == 1
    assert lines[0].isascii()
    json.loads(lines[0])


def test_fields_cannot_overwrite_the_core_keys(buffer: io.StringIO) -> None:
    log_event(logging.getLogger("test"), logging.INFO, "real", msg="fake", level="critical")
    record = json.loads(_lines(buffer)[0])
    assert record["msg"] == "real"
    assert record["level"] == "info"


def test_tracebacks_are_logged_with_secrets_blanked(buffer: io.StringIO) -> None:
    try:
        raise RuntimeError("failed with Bearer secret-value")
    except RuntimeError:
        logging.getLogger("test").exception("upstream call failed")
    record = json.loads(_lines(buffer)[0])
    assert "RuntimeError" in record["exc"]
    assert "secret-value" not in record["exc"]
