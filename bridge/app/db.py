"""SQLite storage for peers, settings, events and bandwidth samples.

Only the standard library is used. The database lives on the persistent volume
and is the single source of truth for everything the panel remembers.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from app.config import SETTINGS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS peers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    private_key     TEXT    NOT NULL,
    public_key      TEXT    NOT NULL,
    preshared_key   TEXT    NOT NULL,
    address         TEXT    NOT NULL UNIQUE,
    behind_networks TEXT    NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    kind    TEXT    NOT NULL,
    level   TEXT    NOT NULL,
    message TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts DESC);

-- peer_id 0 means "the bridge as a whole".
CREATE TABLE IF NOT EXISTS samples (
    ts         INTEGER NOT NULL,
    resolution TEXT    NOT NULL,
    peer_id    INTEGER NOT NULL,
    rx         INTEGER NOT NULL,
    tx         INTEGER NOT NULL,
    PRIMARY KEY (resolution, ts, peer_id)
) WITHOUT ROWID;

-- Cumulative counters, kept across container restarts.
CREATE TABLE IF NOT EXISTS peer_totals (
    peer_id        INTEGER PRIMARY KEY,
    rx             INTEGER NOT NULL DEFAULT 0,
    tx             INTEGER NOT NULL DEFAULT 0,
    last_rx        INTEGER NOT NULL DEFAULT 0,
    last_tx        INTEGER NOT NULL DEFAULT 0,
    last_handshake INTEGER NOT NULL DEFAULT 0,
    endpoint       TEXT    NOT NULL DEFAULT ''
);
"""

_local = threading.local()


def connect() -> sqlite3.Connection:
    """Return this thread's connection, creating and migrating it on demand."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        SETTINGS.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(SETTINGS.db_path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        _local.conn = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def get_setting(key: str, default: str | None = None) -> str | None:
    row = connect().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    connect().execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
