"""qdistro-pwd audit log — sqlite-backed, item value never logged.

Schema (one row per request, allowed or denied):

    pwd_audit:
        id           INTEGER PRIMARY KEY
        ts           INTEGER NOT NULL              -- unix epoch
        op           TEXT    NOT NULL              -- get | unlock | lock | add | delete | list
        vault        TEXT    NOT NULL
        item_tag     TEXT                          -- NULL for vault-level ops
        decision     TEXT    NOT NULL              -- allow | deny | error
        reason       TEXT    NOT NULL              -- short human-readable
        caller_uid   INTEGER
        caller_pid   INTEGER
        caller_exe   TEXT
        caller_sha   TEXT                          -- exe sha256 (12-char prefix is plenty)
        caller_selinux TEXT
        caller_cgroup  TEXT
        origin       TEXT                            -- autofill/browser request origin URL (scheme://host[:port])
        app_id       TEXT                            -- bridge-attested extension/browser identity
        app_context  TEXT                            -- silo / app context, when known

The value payload is never persisted — only metadata. Item tags are
stored in cleartext (matches broker audit semantics: an admin
investigating a leak needs to know what was asked for).

The `origin`, `app_id`, and `app_context` columns give the autofill
(Fill / FillConfirm / Save) decision point a place to record the FULL
decision context — request origin URL, the bridge-attested
extension/browser identity, and the silo/app context — on BOTH allow
and deny rows, so a denial always carries an investigatable reason +
context. These NEVER hold credential material (see assertions in
tests/unit/test_pwd_autofill_audit.py).

Per spec/13 §"Audit": admin can opt into payload logging for
debugging; that's a future opt-in flag, not in MVP.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any

DB_PATH_DEFAULT = "/var/lib/qdistro/audit/pwd_audit.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pwd_audit (
    id             INTEGER PRIMARY KEY,
    ts             INTEGER NOT NULL,
    op             TEXT    NOT NULL,
    vault          TEXT    NOT NULL,
    item_tag       TEXT,
    decision       TEXT    NOT NULL,
    reason         TEXT    NOT NULL,
    caller_uid     INTEGER,
    caller_pid     INTEGER,
    caller_exe     TEXT,
    caller_sha     TEXT,
    caller_selinux TEXT,
    caller_cgroup  TEXT,
    origin         TEXT,
    app_id         TEXT,
    app_context    TEXT
);
CREATE INDEX IF NOT EXISTS pwd_audit_ts_idx ON pwd_audit(ts);
CREATE INDEX IF NOT EXISTS pwd_audit_vault_idx ON pwd_audit(vault, item_tag);
"""


def _migrate(conn) -> None:
    """Additive migrations for pre-existing audit DBs.

    ``CREATE TABLE IF NOT EXISTS`` leaves an older column set untouched,
    so columns added after the initial schema need an explicit, idempotent
    ALTER TABLE. Mirrors broker/qdistro_admin_audit.py::_migrate."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pwd_audit)")}
    if "origin" not in cols:
        conn.execute("ALTER TABLE pwd_audit ADD COLUMN origin TEXT")
    if "app_id" not in cols:
        conn.execute("ALTER TABLE pwd_audit ADD COLUMN app_id TEXT")
    if "app_context" not in cols:
        conn.execute("ALTER TABLE pwd_audit ADD COLUMN app_context TEXT")


class PwdAuditLog:
    def __init__(self, path: str = DB_PATH_DEFAULT):
        self.path = path
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.executescript(SCHEMA)
        _migrate(self._conn)

    def record(self, op: str, vault: str, *, item_tag: str | None = None,
               decision: str = "allow", reason: str = "",
               caller: dict[str, Any] | None = None,
               origin: str | None = None, app_id: str | None = None,
               app_context: str | None = None) -> int:
        c = caller or {}
        sha = (c.get("exe_sha256") or "")[:12]
        cur = self._conn.execute(
            "INSERT INTO pwd_audit "
            "(ts, op, vault, item_tag, decision, reason, "
            " caller_uid, caller_pid, caller_exe, caller_sha, "
            " caller_selinux, caller_cgroup, origin, app_id, app_context) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(time.time()), op, vault, item_tag, decision, reason,
             c.get("uid"), c.get("pid"), c.get("exe", ""),
             sha, c.get("selinux_label", ""), c.get("cgroup", ""),
             origin, app_id, app_context))
        return cur.lastrowid

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, ts, op, vault, item_tag, decision, reason, "
            "       caller_uid, caller_pid, caller_exe, caller_sha, "
            "       caller_selinux, caller_cgroup, origin, app_id, app_context "
            "FROM pwd_audit ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()
        cols = ("id", "ts", "op", "vault", "item_tag", "decision", "reason",
                "caller_uid", "caller_pid", "caller_exe", "caller_sha",
                "caller_selinux", "caller_cgroup", "origin", "app_id",
                "app_context")
        return [dict(zip(cols, r)) for r in rows]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
