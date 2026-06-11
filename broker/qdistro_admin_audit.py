"""Audit log for the qdistro admin broker.

Every permission decision (whether reached via admin prompt or via
cache hit) writes one row. Used by `qdistro-approvals audit` and any
future incident-investigation flow.

Schema (see  A):

    audit:
        id INTEGER PK
        ts INTEGER             -- unix epoch
        caller_uid INTEGER
        caller_pid INTEGER
        caller_exe TEXT
        action TEXT
        decision INTEGER       -- 1 allow, 0 deny
        scope TEXT             -- once|1h|24h|forever|forever_exe|
                               -- forever_argv|forever_basename|
                               -- forever_prefix|NULL
        source TEXT            -- 'prompt' | 'cache'
        approver_uid INTEGER   -- NULL when source='cache'

Indexed on ts DESC for "recent first" queries.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id           INTEGER PRIMARY KEY,
    ts           INTEGER NOT NULL,
    caller_uid   INTEGER NOT NULL,
    caller_pid   INTEGER NOT NULL,
    caller_exe   TEXT NOT NULL,
    action       TEXT NOT NULL,
    decision     INTEGER NOT NULL,
    scope        TEXT,
    source       TEXT NOT NULL,
    approver_uid INTEGER,
    rule_path    TEXT,
    request_id   INTEGER,
    argv         TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts DESC);
CREATE INDEX IF NOT EXISTS audit_caller ON audit(caller_uid);
CREATE INDEX IF NOT EXISTS audit_action ON audit(action);
"""


def _migrate(conn) -> None:
    """Additive migrations for existing audit DBs.

    `CREATE TABLE IF NOT EXISTS` leaves old column sets alone, so new
    columns need explicit ALTER TABLE. Each migration step is
    idempotent: checks the current columns, adds only what's missing."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(audit)")}
    if "rule_path" not in cols:
        conn.execute("ALTER TABLE audit ADD COLUMN rule_path TEXT")
    if "request_id" not in cols:
        conn.execute("ALTER TABLE audit ADD COLUMN request_id INTEGER")
    if "selinux_subj_type" not in cols:
        # spec/30 step 7. Set when an audispd-routed AVC denial is
        # forwarded into the audit log. NULL for prompt/cache rows.
        conn.execute("ALTER TABLE audit ADD COLUMN selinux_subj_type TEXT")
    if "argv" not in cols:
        # task(070): stamp argv on qsu / RequestPermission rows so the
        # audit history disambiguates `apt-get update` from
        # `apt-get install evil`. JSON-encoded list when set, NULL for
        # rows that don't carry argv (clipboard / handoff / activation).
        conn.execute("ALTER TABLE audit ADD COLUMN argv TEXT")


class AuditLog:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # The broker shares one connection across threads
        # (check_same_thread=False). sqlite3 forbids concurrent use of a
        # single connection from multiple threads — overlapping calls
        # raise sqlite3.InterfaceError ("bad parameter or other API
        # misuse", i.e. SQLITE_MISUSE). DecideRequest performs the audit
        # write OUTSIDE the broker's own lock, so concurrent decisions
        # would race here and the broker would fail closed (deny). Serialise
        # all connection access with our own lock.
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        # Restrictive umask so the db file is 0600 from byte 0
        old_umask = os.umask(0o077)
        try:
            self._conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        finally:
            os.umask(old_umask)
        self._conn.executescript(SCHEMA)
        _migrate(self._conn)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        os.chmod(db_path, 0o600)

    def log(self, *, caller_uid: int, caller_pid: int, caller_exe: str,
            action: str, decision: bool, scope: str | None,
            source: str, approver_uid: int | None,
            rule_path: str | None = None,
            request_id: int | None = None,
            selinux_subj_type: str | None = None,
            argv: list | tuple | None = None) -> None:
        argv_text: str | None
        if argv is None:
            argv_text = None
        else:
            import json
            argv_text = json.dumps(list(argv))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit
                  (ts, caller_uid, caller_pid, caller_exe, action,
                   decision, scope, source, approver_uid, rule_path,
                   request_id, selinux_subj_type, argv)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (int(time.time()), caller_uid, caller_pid, caller_exe, action,
                 1 if decision else 0, scope, source, approver_uid, rule_path,
                 request_id, selinux_subj_type, argv_text),
            )

    def recent(self, limit: int = 200) -> list[dict]:
        """Return the N most-recent audit rows as plain dicts.

        Used by the broker's ListHistory method to feed the
        admin-approval-app History tab. No filtering beyond the limit;
        caller paginates / filters client-side. ts comes back as epoch
        seconds; decision is 0/1; nullable columns come back as None.
        """
        limit = max(1, min(int(limit), 10000))
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT ts, caller_uid, caller_pid, caller_exe, action,
                       decision, scope, source, approver_uid, rule_path,
                       request_id, selinux_subj_type, argv
                FROM audit ORDER BY ts DESC, id DESC LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
        out = []
        for row in rows:
            (ts, uid, pid, exe, action, decision, scope, source,
             approver_uid, rule_path, request_id,
             selinux_subj_type, argv_text) = row
            argv = None
            if argv_text:
                try:
                    import json as _json
                    decoded = _json.loads(argv_text)
                    argv = list(decoded) if isinstance(decoded, (list, tuple)) else None
                except (ValueError, TypeError):
                    argv = None
            out.append({
                "ts": int(ts),
                "caller_uid": int(uid),
                "caller_pid": int(pid),
                "caller_exe": exe,
                "action": action,
                "decision": bool(decision),
                "scope": scope,
                "source": source,
                "approver_uid": approver_uid,
                "rule_path": rule_path,
                "request_id": request_id,
                "selinux_subj_type": selinux_subj_type,
                "argv": argv,
            })
        return out

    def gc(self, older_than_seconds: int) -> int:
        """Delete rows older than `older_than_seconds` ago. Return count.

        Retention is enforced at GC time, not at INSERT time — a recent
        burst of writes is never rejected. The caller decides the
        threshold; the broker picks a default and schedules a timer.
        """
        if older_than_seconds < 0:
            raise ValueError(f"older_than_seconds must be >= 0, got {older_than_seconds}")
        cutoff = int(time.time()) - int(older_than_seconds)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM audit WHERE ts < ?", (cutoff,))
            return cur.rowcount or 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
