"""SQLite persistence layer.

The database is shared by two API processes.  Correctness relies on three
SQLite guarantees combined with the helpers below:

* WAL journal mode plus ``PRAGMA busy_timeout`` so readers never block
  writers and contending processes wait for the write lock instead of
  failing immediately.
* Every mutating operation runs inside an explicit ``BEGIN IMMEDIATE``
  transaction, which acquires the reserved write lock up front.  SQLite
  serialises write transactions database-wide, so corrections, publishes
  and withdrawals are linearised as if executed one at a time.
* Rows are always re-read inside the same write transaction, hence a
  publish cannot miss a correction that acquired the lock first.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import BUSY_TIMEOUT_MS, database_path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS results (
    result_id   TEXT PRIMARY KEY,
    sample_code TEXT NOT NULL,
    item        TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS result_revisions (
    result_id   TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    value       TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (result_id, revision),
    FOREIGN KEY (result_id) REFERENCES results(result_id)
);

CREATE TABLE IF NOT EXISTS reports (
    report_id       TEXT PRIMARY KEY,
    sample_code     TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('valid', 'withdrawn')),
    etag_version    INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    published_at    TEXT NOT NULL,
    withdrawn_at    TEXT,
    withdraw_reason TEXT
);

CREATE TABLE IF NOT EXISTS report_items (
    report_id      TEXT NOT NULL,
    result_id      TEXT NOT NULL,
    item_index     INTEGER NOT NULL,
    sample_code    TEXT NOT NULL,
    item           TEXT NOT NULL,
    value          TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    revision_created_at TEXT NOT NULL,
    PRIMARY KEY (report_id, item_index),
    FOREIGN KEY (report_id) REFERENCES reports(report_id)
);

CREATE INDEX IF NOT EXISTS idx_report_items_result
    ON report_items(result_id);
CREATE INDEX IF NOT EXISTS idx_reports_status
    ON reports(status);
"""


def connect() -> sqlite3.Connection:
    """Open a connection tuned for the multi-process WAL deployment."""

    conn = sqlite3.connect(
        database_path(),
        timeout=BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # transactions are managed explicitly below
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def initialize() -> None:
    """Create the schema (idempotent; safe to run on every startup)."""

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


@contextmanager
def write_transaction() -> Iterator[sqlite3.Connection]:
    """Yield a connection inside ``BEGIN IMMEDIATE``.

    Acquiring the reserved lock at transaction start makes all write
    transactions globally serial across processes — the core mechanism that
    makes concurrent correct/publish/withdraw equivalent to some serial
    ordering.
    """

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


@contextmanager
def read_connection() -> Iterator[sqlite3.Connection]:
    """Yield a connection for a consistent read (WAL snapshot semantics)."""

    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
