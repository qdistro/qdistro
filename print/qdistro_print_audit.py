"""qdistro-print-proxy audit log — sqlite-backed.

Mirrors the broker / pwd audit shape (PwdAuditLog from
pwd/qdistro_pwd_audit.py): per-event row with timestamp,
caller identity, decision, and a free-form reason. The IPP byte
payloads are NEVER persisted — the audit only records the
connection lifecycle and the gate decision.

Default DB path: ``/var/lib/qdistro/audit/print_audit.sqlite``.
Override via ``QDISTRO_PRINT_AUDIT_DB``. Schema is created
on first record. Inserts are committed inline (no batching) —
print connections are infrequent enough that fsync() per record
is fine. The `tail` helper supports the admin UI's "Printing
history" page.

Spec: spec/20 Phase-9 §step 2 §"Per-job audit".
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any

DB_PATH_DEFAULT = "/var/lib/qdistro/audit/print_audit.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS print_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    op            TEXT    NOT NULL,
    decision      TEXT    NOT NULL,
    reason        TEXT,
    caller_uid    INTEGER,
    caller_pid    INTEGER,
    caller_exe    TEXT,
    backend       TEXT,
    bytes_to_be   INTEGER,
    bytes_from_be INTEGER
);
CREATE INDEX IF NOT EXISTS print_audit_ts_idx ON print_audit(ts);
"""


class PrintAuditLog:
    """Tiny sqlite wrapper. Thread-safe via per-call connection."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = (db_path
                        or os.environ.get("QDISTRO_PRINT_AUDIT_DB")
                        or DB_PATH_DEFAULT)
        os.makedirs(os.path.dirname(self.db_path) or ".", mode=0o700,
                    exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=5.0)

    def _init_schema(self) -> None:
        con = self._connect()
        try:
            con.executescript(_SCHEMA)
            con.commit()
        finally:
            con.close()

    def record(self, op: str, *,
               decision: str = "allow",
               reason: str = "",
               caller_uid: int | None = None,
               caller_pid: int | None = None,
               caller_exe: str = "",
               backend: str = "",
               bytes_to_be: int | None = None,
               bytes_from_be: int | None = None) -> int:
        con = self._connect()
        try:
            cur = con.execute(
                "INSERT INTO print_audit "
                "(ts, op, decision, reason, caller_uid, caller_pid, "
                " caller_exe, backend, bytes_to_be, bytes_from_be) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (int(time.time()), str(op), str(decision), str(reason),
                 caller_uid, caller_pid, str(caller_exe), str(backend),
                 bytes_to_be, bytes_from_be),
            )
            con.commit()
            return int(cur.lastrowid or 0)
        finally:
            con.close()

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        con = self._connect()
        try:
            cur = con.execute(
                "SELECT id, ts, op, decision, reason, caller_uid, "
                "caller_pid, caller_exe, backend, bytes_to_be, bytes_from_be "
                "FROM print_audit ORDER BY id DESC LIMIT ?",
                (int(limit),))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            con.close()

    def count(self) -> int:
        con = self._connect()
        try:
            cur = con.execute("SELECT COUNT(*) FROM print_audit")
            return int(cur.fetchone()[0])
        finally:
            con.close()
