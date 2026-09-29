from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id     TEXT PRIMARY KEY,
    public_key    TEXT NOT NULL,
    created_at    REAL NOT NULL,
    first_ip      TEXT,
    last_seen_at  REAL,
    status        TEXT NOT NULL DEFAULT 'active',
    terms_version TEXT,
    attestation   TEXT
);
CREATE TABLE IF NOT EXISTS identity_bindings (
    device_id   TEXT PRIMARY KEY,
    phone_hash  TEXT NOT NULL,
    phone_enc   TEXT NOT NULL,
    verified_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_bindings_phone ON identity_bindings(phone_hash);
CREATE TABLE IF NOT EXISTS otp_challenges (
    device_id  TEXT PRIMARY KEY,
    phone_hash TEXT NOT NULL,
    phone_enc  TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    expires_at REAL NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS threads (
    thread_id     TEXT PRIMARY KEY,
    device_id     TEXT NOT NULL,
    body          TEXT NOT NULL,
    created_at    REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'visible',
    comment_count INTEGER NOT NULL DEFAULT 0,
    report_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_threads_created ON threads(created_at DESC);
CREATE TABLE IF NOT EXISTS comments (
    comment_id   TEXT PRIMARY KEY,
    thread_id    TEXT NOT NULL,
    device_id    TEXT NOT NULL,
    alias_id     TEXT NOT NULL,
    body         TEXT NOT NULL,
    reply_to     TEXT,
    created_at   REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'visible',
    report_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_comments_thread ON comments(thread_id, created_at);
CREATE TABLE IF NOT EXISTS thread_aliases (
    alias_id   TEXT PRIMARY KEY,
    thread_id  TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    name       TEXT NOT NULL,
    hue        INTEGER NOT NULL,
    is_op      INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(thread_id, device_id),
    UNIQUE(thread_id, name)
);
CREATE TABLE IF NOT EXISTS reports (
    target_type TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    device_id   TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (target_type, target_id, device_id)
);
CREATE TABLE IF NOT EXISTS blocks (
    device_id  TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    alias_id   TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (device_id, thread_id, alias_id)
);
CREATE TABLE IF NOT EXISTS bans (
    subject    TEXT PRIMARY KEY,
    until      REAL NOT NULL,
    reason     TEXT NOT NULL,
    shadow     INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    device_id TEXT,
    ip        TEXT,
    action    TEXT NOT NULL,
    target_id TEXT,
    meta      TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_device ON events(device_id, action, ts);
CREATE INDEX IF NOT EXISTS ix_events_ip ON events(ip, action, ts);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    admin    TEXT NOT NULL,
    action   TEXT NOT NULL,
    target   TEXT NOT NULL,
    case_ref TEXT,
    reason   TEXT,
    result   TEXT
);
CREATE TABLE IF NOT EXISTS nonces (
    nonce TEXT PRIMARY KEY,
    ts    REAL NOT NULL
);
"""


class Database:
    """Thin wrapper over sqlite3 with a process-wide lock.

    Good enough for a prototype and for tests; swap for Postgres behind the
    same method names when the product needs more than one API process.
    """

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA foreign_keys=ON")
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | list = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else row[0]

    def close(self) -> None:
        with self.lock:
            self.conn.close()
