"""Decision records: one JSON line for every allow, deny or redact decision.

A record says who asked, for what, which rule decided and what happened. It never holds the
content itself (no prompts, no tool arguments, no model output), because more people read logs
than run the system.

Field names follow OCSF 1.9.0 "API Activity" (class_uid 6003) where OCSF has a field, and the
rest sits under "unmapped", OCSF's own place for extra data. A SIEM can ingest the records
without a custom parser.

Every record carries the hash of the record before it. Editing, reordering or deleting a line
breaks the chain, and verify_chain() reports the first line that no longer fits. Lines cut off
the end can't be detected from the lines alone; a production system would also copy the latest
hash somewhere the log writer can't change.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, TextIO

OCSF_VERSION: Final = "1.9.0"
API_ACTIVITY_CLASS_UID: Final = 6003
APPLICATION_CATEGORY_UID: Final = 6
ACTIVITY_OTHER: Final = 99
SEVERITY_INFORMATIONAL: Final = 1
GENESIS_HASH: Final = "0" * 64


class Decision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    REDACTED = "redacted"


# OCSF action_id: 1 Allowed, 2 Denied, 4 Modified (the request went through, but changed).
_ACTION_ID: Final = {Decision.ALLOWED: 1, Decision.DENIED: 2, Decision.REDACTED: 4}
# OCSF status_id, from the caller's point of view: 1 Success, 2 Failure.
_STATUS_ID: Final = {Decision.ALLOWED: 1, Decision.DENIED: 2, Decision.REDACTED: 1}


@dataclass(frozen=True)
class DecisionEvent:
    """What was decided, without the content. `counts` holds numbers only."""

    request_id: str
    lane: str
    operation: str
    decision: Decision
    reason: str
    policy_id: str
    subject: str | None = None
    role: str | None = None
    target: str | None = None
    trace_id: str | None = None
    latency_ms: float | None = None
    counts: Mapping[str, int] = field(default_factory=dict)


def _digest(prev_hash: str, record: Mapping[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256((prev_hash + canonical).encode("ascii")).hexdigest()


class AuditLog:
    """Writes decision records as JSON lines, each chained to the one before."""

    def __init__(
        self,
        sink: TextIO | None = None,
        policy_version: str = "1",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sink = sink if sink is not None else sys.stderr
        self._policy_version = policy_version
        self._clock = clock
        self._prev_hash = GENESIS_HASH
        # One writer at a time, so records from parallel requests still chain in order.
        self._lock = threading.Lock()

    def record(self, event: DecisionEvent) -> dict[str, Any]:
        with self._lock:
            body: dict[str, Any] = {
                "class_uid": API_ACTIVITY_CLASS_UID,
                "category_uid": APPLICATION_CATEGORY_UID,
                "activity_id": ACTIVITY_OTHER,
                "type_uid": API_ACTIVITY_CLASS_UID * 100 + ACTIVITY_OTHER,
                "severity_id": SEVERITY_INFORMATIONAL,
                "time": int(self._clock() * 1000),
                "action_id": _ACTION_ID[event.decision],
                "status_id": _STATUS_ID[event.decision],
                "metadata": {
                    "version": OCSF_VERSION,
                    "product": {"name": "quilr-gateway-assessment"},
                    "correlation_uid": event.request_id,
                },
                "api": {"operation": event.operation, "service": {"name": event.lane}},
                "policy": {"uid": event.policy_id, "version": self._policy_version},
                "unmapped": {
                    "decision": event.decision.value,
                    "reason": event.reason,
                    "role": event.role,
                    "target": event.target,
                    "latency_ms": event.latency_ms,
                    "counts": dict(event.counts),
                },
                "prev_hash": self._prev_hash,
            }
            if event.subject is not None:
                body["actor"] = {"user": {"uid": event.subject}}
            if event.trace_id is not None:
                body["trace"] = {"uid": event.trace_id}
            body["hash"] = _digest(self._prev_hash, body)
            self._prev_hash = body["hash"]
            self._sink.write(json.dumps(body, sort_keys=True, ensure_ascii=True) + "\n")
            self._sink.flush()
            return body


def verify_chain(lines: Iterable[str]) -> int | None:
    """Check decision records in order. Returns the index of the first bad line, or None."""
    prev_hash = GENESIS_HASH
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except ValueError:
            return index
        if not isinstance(record, dict):
            return index
        claimed = record.pop("hash", None)
        if record.get("prev_hash") != prev_hash or claimed != _digest(prev_hash, record):
            return index
        prev_hash = claimed
    return None
