"""The SQLite sliding-window limiter: window math, eviction, atomicity, fail closed."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from task4_rate_limit_failover.limiter import (
    RateLimited,
    RequestTooLarge,
    SlidingWindowLimiter,
    StoreUnavailable,
)


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "limits.sqlite3"


def make(db: Path, clock: FakeClock, limit: int = 100) -> SlidingWindowLimiter:
    return SlidingWindowLimiter(db, limit, 60.0, clock=clock)


def test_allows_up_to_the_limit_then_refuses(db: Path, clock: FakeClock) -> None:
    limiter = make(db, clock)
    limiter.reserve("a", 60)
    limiter.reserve("a", 40)
    assert limiter.used("a") == 100
    with pytest.raises(RateLimited):
        limiter.reserve("a", 1)


def test_tenants_are_independent(db: Path, clock: FakeClock) -> None:
    limiter = make(db, clock)
    limiter.reserve("a", 100)
    limiter.reserve("b", 100)
    assert limiter.used("a") == limiter.used("b") == 100


def test_window_slides_rather_than_resetting(db: Path, clock: FakeClock) -> None:
    limiter = make(db, clock)
    limiter.reserve("a", 50)  # t = 0
    clock.now += 30
    limiter.reserve("a", 50)  # t = 30
    clock.now += 29.9  # t = 59.9: both rows still count
    with pytest.raises(RateLimited):
        limiter.reserve("a", 1)
    clock.now += 0.1  # t = 60: the first row has slid out, the second has not
    assert limiter.used("a") == 50
    limiter.reserve("a", 50)
    with pytest.raises(RateLimited):
        limiter.reserve("a", 1)


def test_retry_after_points_at_the_row_that_frees_enough(db: Path, clock: FakeClock) -> None:
    limiter = make(db, clock)
    limiter.reserve("a", 30)  # t = 0, expires at 60
    clock.now += 10
    limiter.reserve("a", 70)  # t = 10, expires at 70
    clock.now += 5  # t = 15
    with pytest.raises(RateLimited) as small:
        limiter.reserve("a", 20)  # fits once the 30 at t = 0 expires: 45 s
    assert small.value.retry_after_s == 45
    with pytest.raises(RateLimited) as big:
        limiter.reserve("a", 80)  # needs both rows gone: 55 s
    assert big.value.retry_after_s == 55


def test_retry_after_is_at_least_one_second(db: Path, clock: FakeClock) -> None:
    limiter = make(db, clock)
    limiter.reserve("a", 100)
    clock.now += 59.999
    with pytest.raises(RateLimited) as refused:
        limiter.reserve("a", 1)
    assert refused.value.retry_after_s == 1


def test_expired_rows_are_deleted_from_disk(db: Path, clock: FakeClock) -> None:
    limiter = make(db, clock)
    for tenant in ("a", "b", "c"):
        limiter.reserve(tenant, 10)
    clock.now += 61
    limiter.reserve("d", 1)  # any reservation evicts every tenant's expired rows
    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT tenant FROM usage").fetchall()
    assert rows == [("d",)]


def test_a_refused_request_still_evicts(db: Path, clock: FakeClock) -> None:
    limiter = make(db, clock)
    limiter.reserve("old", 10)  # t = 0
    clock.now += 30
    limiter.reserve("a", 100)  # t = 30
    clock.now += 31  # t = 61: "old" has expired, "a" has not
    with pytest.raises(RateLimited):
        limiter.reserve("a", 1)
    # The refusal still committed its eviction.
    with sqlite3.connect(db) as connection:
        count = connection.execute("SELECT COUNT(*) FROM usage WHERE tenant='old'").fetchone()
    assert count == (0,)


def test_request_bigger_than_the_whole_budget(db: Path, clock: FakeClock) -> None:
    limiter = make(db, clock)
    with pytest.raises(RequestTooLarge):
        limiter.reserve("a", 101)
    assert limiter.used("a") == 0


def test_settle_corrects_the_charge_to_actual_usage(db: Path, clock: FakeClock) -> None:
    limiter = make(db, clock)
    reservation = limiter.reserve("a", 90)
    limiter.settle(reservation, 25)
    assert limiter.used("a") == 25
    limiter.reserve("a", 75)


def test_settle_can_charge_more_than_the_estimate(db: Path, clock: FakeClock) -> None:
    limiter = make(db, clock)
    reservation = limiter.reserve("a", 10)
    limiter.settle(reservation, 150)  # the provider used more than we guessed
    assert limiter.used("a") == 150
    with pytest.raises(RateLimited):
        limiter.reserve("a", 1)


def test_release_refunds_in_full(db: Path, clock: FakeClock) -> None:
    limiter = make(db, clock)
    reservation = limiter.reserve("a", 100)
    limiter.release(reservation)
    assert limiter.used("a") == 0


def test_state_survives_a_restart(db: Path, clock: FakeClock) -> None:
    make(db, clock).reserve("a", 80)
    again = make(db, clock)  # a new process would open the same file
    assert again.used("a") == 80
    with pytest.raises(RateLimited):
        again.reserve("a", 21)


def test_fifty_concurrent_requests_never_overshoot(db: Path, clock: FakeClock) -> None:
    # 50 threads, each with its own connection, race for a 50,000-token budget at 2,000 each.
    # Exactly 25 fit. Without BEGIN IMMEDIATE several would read the same total and all pass.
    limiter = SlidingWindowLimiter(db, 50_000, 60.0, clock=clock)
    start = threading.Barrier(50)

    def attempt(_: int) -> bool:
        start.wait()
        try:
            limiter.reserve("tenant", 2_000)
        except RateLimited:
            return False
        return True

    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(attempt, range(50)))
    assert results.count(True) == 25
    assert limiter.used("tenant") == 50_000


def test_two_limiters_on_one_file_share_the_budget(db: Path, clock: FakeClock) -> None:
    # Two gateway workers (or processes) pointed at the same database file.
    first, second = make(db, clock), make(db, clock)
    first.reserve("a", 70)
    with pytest.raises(RateLimited):
        second.reserve("a", 31)


def test_unopenable_database_fails_closed(tmp_path: Path, clock: FakeClock) -> None:
    with pytest.raises(StoreUnavailable):
        SlidingWindowLimiter(tmp_path / "missing-dir" / "x.sqlite3", 100, clock=clock)


def test_corrupt_database_fails_closed(db: Path, clock: FakeClock) -> None:
    limiter = make(db, clock)
    db.write_bytes(b"this is not a database" * 100)
    for name in ("limits.sqlite3-wal", "limits.sqlite3-shm"):
        (db.parent / name).unlink(missing_ok=True)
    with pytest.raises(StoreUnavailable):
        limiter.reserve("a", 1)


def test_tampered_schema_fails_closed(db: Path, clock: FakeClock) -> None:
    limiter = make(db, clock)
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE usage")
    with pytest.raises(StoreUnavailable):
        limiter.reserve("a", 1)


def test_locked_database_fails_closed_after_the_busy_timeout(db: Path, clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(db, 100, clock=clock, busy_timeout_s=0.05)
    holder = sqlite3.connect(db, autocommit=True)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StoreUnavailable):
            limiter.reserve("a", 1)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    limiter.reserve("a", 1)  # works again once the lock is gone


@pytest.mark.parametrize(("limit", "window"), [(0, 60.0), (100, 0.0)])
def test_rejects_nonsense_settings(db: Path, limit: int, window: float) -> None:
    with pytest.raises(ValueError, match="must"):
        SlidingWindowLimiter(db, limit, window)


def test_rejects_negative_amounts(db: Path, clock: FakeClock) -> None:
    limiter = make(db, clock)
    with pytest.raises(ValueError, match="negative"):
        limiter.reserve("a", -1)
    reservation = limiter.reserve("a", 1)
    with pytest.raises(ValueError, match="negative"):
        limiter.settle(reservation, -1)
