"""Step 0 checks: the environment the rest of the repo depends on."""

import sqlite3
import sys


def test_python_is_at_least_3_12() -> None:
    # sqlite3's autocommit setting, which Task 4 relies on, arrived in 3.12.
    assert sys.version_info >= (3, 12)


def test_sqlite_allows_explicit_begin_immediate() -> None:
    # Task 4 opens SQLite with autocommit=True and runs BEGIN IMMEDIATE itself.
    conn = sqlite3.connect(":memory:", autocommit=True)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("COMMIT")
    conn.close()


def test_shared_package_imports() -> None:
    import shared

    assert shared.__doc__
