"""A token-aware sliding-window rate limiter, stored in an on-disk SQLite database.

Each tenant may use at most ``limit_tokens`` tokens in any ``window_s`` seconds (50,000 per
60 s by default). Every request is one row: who, when, how many tokens. A tenant's usage is
the sum of its rows newer than ``now - window``, so the window slides continuously instead of
resetting on the minute, and there is no burst at a window boundary.

The life of a request:

1. ``reserve(tenant, estimate)``: one ``BEGIN IMMEDIATE`` transaction drops expired rows,
   sums the tenant's live rows and, only if the estimate fits, inserts a row for it.
   ``BEGIN IMMEDIATE`` takes SQLite's write lock before the first read, so two requests can
   never both read "45,000 used" and both decide they fit: checking and charging are one
   atomic step, across threads and across processes sharing the file.
2. ``settle(reservation, actual)``: once the provider reports real usage, the row is
   corrected to it, so the tenant pays what was used rather than the guess.
3. ``release(reservation)``: if no provider answered, the row is removed and costs nothing.

Failure handling is fail closed. If the database cannot be opened, read or written, every
call raises ``StoreUnavailable`` and the gateway answers 503. It never lets traffic through
uncounted, because an attacker who can break the limiter would otherwise turn it off.

Time comes from ``clock`` (wall-clock seconds by default) so the window survives restarts,
which a monotonic clock would not. Tests pass a fake clock.
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL,
    at REAL NOT NULL,
    tokens INTEGER NOT NULL CHECK (tokens >= 0)
);
CREATE INDEX IF NOT EXISTS usage_tenant_at ON usage (tenant, at);
CREATE INDEX IF NOT EXISTS usage_at ON usage (at);
"""


class RateLimited(Exception):
    """The tenant's budget for the current window is spent."""

    def __init__(self, retry_after_s: int) -> None:
        super().__init__("rate limited")
        self.retry_after_s = retry_after_s


class RequestTooLarge(Exception):
    """The request alone needs more than a whole window's budget, so it can never fit."""


class StoreUnavailable(Exception):
    """The database could not be used. Callers must refuse the request, never allow it."""


@dataclass(frozen=True)
class Reservation:
    id: int
    tenant: str
    tokens: int


class SlidingWindowLimiter:
    def __init__(
        self,
        path: str | Path,
        limit_tokens: int,
        window_s: float = 60.0,
        *,
        clock: Callable[[], float] = time.time,
        busy_timeout_s: float = 5.0,
    ) -> None:
        if limit_tokens < 1:
            raise ValueError("limit_tokens must be at least 1")
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        self.limit_tokens = limit_tokens
        self.window_s = window_s
        self._path = str(path)
        self._clock = clock
        self._busy_timeout_s = busy_timeout_s
        self._run(self._create_schema)

    # Every operation opens its own connection. sqlite3 connections must not be shared across
    # threads, and the gateway calls the limiter from a thread pool, so this keeps each call
    # independent and lets SQLite's own locking do the serializing.
    def _run[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        try:
            connection = sqlite3.connect(self._path, timeout=self._busy_timeout_s, autocommit=True)
        except (sqlite3.Error, OSError):
            raise StoreUnavailable from None
        try:
            return operation(connection)
        except (sqlite3.Error, OSError):
            raise StoreUnavailable from None
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        # WAL lets readers and the single writer work at the same time.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(_SCHEMA)

    @staticmethod
    def _transaction[T](
        connection: sqlite3.Connection, body: Callable[[sqlite3.Connection], T]
    ) -> T:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = body(connection)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
        return result

    def reserve(self, tenant: str, tokens: int) -> Reservation:
        """Charge ``tokens`` to ``tenant`` now, or raise RateLimited if they do not fit."""
        if tokens < 0:
            raise ValueError("tokens must not be negative")
        if tokens > self.limit_tokens:
            raise RequestTooLarge

        def body(connection: sqlite3.Connection) -> Reservation | int:
            now = self._clock()
            # Eviction: rows that have slid out of every tenant's window are gone for good.
            connection.execute("DELETE FROM usage WHERE at <= ?", (now - self.window_s,))
            rows = connection.execute(
                "SELECT at, tokens FROM usage WHERE tenant = ? ORDER BY at, id", (tenant,)
            ).fetchall()
            used = sum(row_tokens for _, row_tokens in rows)
            if used + tokens > self.limit_tokens:
                return self._retry_after(rows, used, tokens, now)
            cursor = connection.execute(
                "INSERT INTO usage (tenant, at, tokens) VALUES (?, ?, ?)", (tenant, now, tokens)
            )
            if cursor.lastrowid is None:  # an INSERT always sets it; refuse rather than guess
                raise sqlite3.OperationalError("insert returned no row id")
            return Reservation(cursor.lastrowid, tenant, tokens)

        # The transaction commits before RateLimited is raised, so the eviction is kept.
        result = self._run(lambda connection: self._transaction(connection, body))
        if isinstance(result, int):
            raise RateLimited(result)
        return result

    def _retry_after(
        self, rows: list[tuple[float, int]], used: int, tokens: int, now: float
    ) -> int:
        """Seconds until enough of the oldest rows expire for ``tokens`` to fit."""
        freed = 0
        for at, row_tokens in rows:
            freed += row_tokens
            if used - freed + tokens <= self.limit_tokens:
                return max(1, math.ceil(at + self.window_s - now))
        return max(1, math.ceil(self.window_s))  # unreachable while tokens <= limit

    def settle(self, reservation: Reservation, actual_tokens: int) -> None:
        """Replace the estimate with what the provider reported."""
        if actual_tokens < 0:
            raise ValueError("actual_tokens must not be negative")
        self._run(
            lambda connection: connection.execute(
                "UPDATE usage SET tokens = ? WHERE id = ?", (actual_tokens, reservation.id)
            )
        )

    def release(self, reservation: Reservation) -> None:
        """Refund a reservation in full, for a request no provider answered."""
        self._run(
            lambda connection: connection.execute(
                "DELETE FROM usage WHERE id = ?", (reservation.id,)
            )
        )

    def used(self, tenant: str) -> int:
        """Tokens ``tenant`` has used in the current window."""
        now = self._clock()
        row = self._run(
            lambda connection: connection.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE tenant = ? AND at > ?",
                (tenant, now - self.window_s),
            ).fetchone()
        )
        return int(row[0])
