"""Decision records: OCSF-shaped, no content, and any edit breaks the chain."""

import io
import json
import threading

import pytest

from shared.audit import (
    GENESIS_HASH,
    AuditLog,
    Decision,
    DecisionEvent,
    verify_chain,
)


def _event(n: int = 0, decision: Decision = Decision.DENIED) -> DecisionEvent:
    return DecisionEvent(
        request_id=f"req_{n}",
        lane="mcp",
        operation="tools/call",
        decision=decision,
        reason="admin_tool_requires_admin",
        policy_id="mcp.admin_prefix",
        subject="victor",
        role="viewer",
        target="admin_reset_key",
    )


def _write(count: int) -> list[str]:
    sink = io.StringIO()
    log = AuditLog(sink=sink, clock=lambda: 1_794_000_000.0)
    for n in range(count):
        log.record(_event(n))
    return sink.getvalue().splitlines()


def test_record_follows_ocsf_api_activity() -> None:
    record = json.loads(_write(1)[0])
    assert record["class_uid"] == 6003
    assert record["category_uid"] == 6
    assert record["type_uid"] == 600399
    assert record["metadata"]["version"] == "1.9.0"
    assert record["metadata"]["correlation_uid"] == "req_0"
    assert record["actor"] == {"user": {"uid": "victor"}}
    assert record["api"] == {"operation": "tools/call", "service": {"name": "mcp"}}
    assert record["policy"]["uid"] == "mcp.admin_prefix"
    assert record["time"] == 1_794_000_000_000
    assert record["prev_hash"] == GENESIS_HASH


@pytest.mark.parametrize(
    ("decision", "action_id", "status_id"),
    [(Decision.ALLOWED, 1, 1), (Decision.DENIED, 2, 2), (Decision.REDACTED, 4, 1)],
)
def test_decisions_map_to_ocsf_action_and_status(
    decision: Decision, action_id: int, status_id: int
) -> None:
    record = AuditLog(sink=io.StringIO()).record(_event(decision=decision))
    assert record["action_id"] == action_id
    assert record["status_id"] == status_id


def test_records_carry_decisions_and_counts_but_no_content() -> None:
    event = DecisionEvent(
        request_id="req_9",
        lane="llm",
        operation="chat.completions",
        decision=Decision.REDACTED,
        reason="pii_found",
        policy_id="llm.pii",
        counts={"email": 2, "ssn": 1},
    )
    record = AuditLog(sink=io.StringIO()).record(event)
    assert record["unmapped"]["counts"] == {"email": 2, "ssn": 1}
    allowed_keys = {
        "class_uid", "category_uid", "activity_id", "type_uid", "severity_id", "time",
        "action_id", "status_id", "metadata", "api", "policy", "unmapped", "prev_hash", "hash",
    }  # fmt: skip
    assert set(record) <= allowed_keys | {"actor", "trace"}


def test_an_untouched_chain_verifies() -> None:
    assert verify_chain(_write(5)) is None


def test_editing_a_line_breaks_the_chain_at_that_line() -> None:
    lines = _write(5)
    tampered = json.loads(lines[2])
    tampered["unmapped"]["decision"] = "allowed"
    lines[2] = json.dumps(tampered, sort_keys=True)
    assert verify_chain(lines) == 2


def test_deleting_a_middle_line_is_detected() -> None:
    lines = _write(5)
    del lines[2]
    assert verify_chain(lines) == 2


def test_reordering_lines_is_detected() -> None:
    lines = _write(5)
    lines[1], lines[3] = lines[3], lines[1]
    assert verify_chain(lines) == 1


def test_a_garbage_line_is_detected() -> None:
    lines = _write(3)
    lines.insert(1, "not json")
    assert verify_chain(lines) == 1


def test_records_from_parallel_threads_still_form_one_chain() -> None:
    sink = io.StringIO()
    log = AuditLog(sink=sink)
    threads = [threading.Thread(target=log.record, args=(_event(n),)) for n in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = sink.getvalue().splitlines()
    assert len(lines) == 50
    assert verify_chain(lines) is None
