"""Audit trail for the template lifecycle (todo/fableplan task 06).

Template lifecycle decisions are appended to the same audit store as the
rest of qdistro (a dedicated SQLite DB under /var/lib/qdistro/audit/,
alongside the broker permission audit and the workflow/print audit DBs)
*and* emitted as structured journald fields, so `journalctl` and the admin
UI both see them. Security-relevant decisions (promote applied/refused, GC
deletions) are also forwarded best-effort into the broker's audit table.

The cardinal rule: **evidence outlives payload.** A deletion event records
the evidence path that remains; there is no event deletion path here at
all.

Field discipline matches silo health logging: every event carries (where
applicable) silo, template, generation digest, run-id, action/event,
result, duration, and reason.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

# Lives in the shared audit store. Overridable for tests via the same
# QDISTRO_VAR_DIR the Layout uses.
def _default_db_path() -> str:
    var = os.environ.get("QDISTRO_VAR_DIR", "/var/lib/qdistro")
    return os.path.join(var, "audit", "template_audit.sqlite")


BROKER_AUDIT_PATH = "/var/lib/qdistro/audit/audit.sqlite"

EVENTS = (
    "template.build.started",
    "template.build.finished",
    "template.validate.finished",
    "template.promote.requested",
    "template.promote.applied",
    "template.promote.refused",
    "template.binding.activated",
    "template.gc.deleted",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS template_audit (
    id               INTEGER PRIMARY KEY,
    ts               REAL NOT NULL,
    event            TEXT NOT NULL,
    silo             TEXT,
    template         TEXT,
    generation       TEXT,
    old_generation   TEXT,
    new_generation   TEXT,
    identity_revision INTEGER,
    run_id           TEXT,
    result           TEXT,
    duration_seconds REAL,
    reason           TEXT,
    evidence_path    TEXT,
    network_mode     TEXT,
    extra            TEXT
);
CREATE INDEX IF NOT EXISTS template_audit_ts ON template_audit(ts DESC);
CREATE INDEX IF NOT EXISTS template_audit_event ON template_audit(event);
CREATE INDEX IF NOT EXISTS template_audit_silo ON template_audit(silo);
"""

# Promote semantics want old/new generation + identity revision as
# disciplined queryable columns rather than buried in extra.
_COLUMN_FIELDS = ("old_generation", "new_generation", "identity_revision")


def _migrate(conn) -> None:
    """Additive migrations for audit DBs created before a column existed.

    `CREATE TABLE IF NOT EXISTS` leaves an old column set alone, so columns
    added later need an explicit ALTER TABLE — mirrors the broker audit
    migration. Each step is idempotent."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(template_audit)")}
    for col, decl in (("old_generation", "TEXT"),
                      ("new_generation", "TEXT"),
                      ("identity_revision", "INTEGER")):
        if col not in cols:
            conn.execute(f"ALTER TABLE template_audit ADD COLUMN {col} {decl}")


class TemplateAuditLog:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or _default_db_path()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        old_umask = os.umask(0o077)
        try:
            self._conn = sqlite3.connect(self.db_path, isolation_level=None,
                                         check_same_thread=False)
        finally:
            os.umask(old_umask)
        self._conn.executescript(SCHEMA)
        _migrate(self._conn)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def log(self, event: str, *, silo=None, template=None, generation=None,
            old_generation=None, new_generation=None, identity_revision=None,
            run_id=None, result=None, duration=None, reason=None,
            evidence_path=None, network_mode=None, **extra) -> None:
        if event not in EVENTS:
            raise ValueError(f"unknown template audit event {event!r}")
        self._conn.execute(
            """INSERT INTO template_audit
               (ts, event, silo, template, generation, old_generation,
                new_generation, identity_revision, run_id, result,
                duration_seconds, reason, evidence_path, network_mode, extra)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), event, silo, template, generation, old_generation,
             new_generation, identity_revision, run_id, result,
             duration, reason, evidence_path, network_mode,
             json.dumps(extra, default=str) if extra else None),
        )

    def recent(self, limit: int = 200) -> list[dict]:
        limit = max(1, min(int(limit), 10000))
        cols = ["ts", "event", "silo", "template", "generation",
                "old_generation", "new_generation", "identity_revision",
                "run_id", "result", "duration_seconds", "reason",
                "evidence_path", "network_mode", "extra"]
        cur = self._conn.execute(
            f"SELECT {', '.join(cols)} FROM template_audit "
            f"ORDER BY ts DESC, id DESC LIMIT ?",
            (limit,),
        )
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()


def _best_effort_stderr(msg: str) -> None:
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — even logging the log failure is best-effort
        pass


def _to_journald(event: str, fields: dict) -> None:
    """Emit one structured journal entry. Falls back to a structured stderr
    line when python-systemd is unavailable (systemd still captures it).
    Never raises."""
    struct = {f"QDISTRO_{k.upper()}": str(v)
              for k, v in fields.items() if v is not None}
    summary = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    message = f"{event} {summary}".strip()
    try:
        from systemd import journal  # type: ignore[import-not-found]
        journal.send(message, QDISTRO_EVENT=event,
                     SYSLOG_IDENTIFIER="qdistro-template", **struct)
    except Exception:  # noqa: BLE001
        _best_effort_stderr(f"[qdistro-template-audit] {message}")


def _forward_to_broker(event: str, fields: dict) -> None:
    """Best-effort forward of a security-relevant decision into the broker
    audit table, mirroring the workflow audit forwarder. Never raises, and
    never permanently mutates sys.path."""
    if not os.path.isfile(BROKER_AUDIT_PATH):
        return
    broker_dir = "/usr/lib/qdistro"  # installed broker module dir
    inserted = broker_dir not in sys.path
    if inserted:
        sys.path.insert(0, broker_dir)
    try:
        from qdistro_admin_audit import AuditLog  # type: ignore[import-not-found]
        decision = fields.get("result") not in ("refused", "failed", "deleted")
        gen = fields.get("generation") or ""
        tmpl = fields.get("template") or ""
        AuditLog(BROKER_AUDIT_PATH).log(
            caller_uid=os.getuid(), caller_pid=os.getpid(),
            caller_exe="qdistro-template", action=f"qdistro.{event}:{tmpl}/{gen}",
            decision=decision, scope=None, source="template-lifecycle",
            approver_uid=None,
        )
    except Exception:  # noqa: BLE001
        pass
    finally:
        if inserted:
            try:
                sys.path.remove(broker_dir)
            except ValueError:
                pass


# Security-relevant decisions also land in the broker audit table.
_BROKER_FORWARD = {
    "template.promote.applied",
    "template.promote.refused",
    "template.gc.deleted",
}


def emit(event: str, *, db_path: str | None = None, broker: bool = True,
         **fields) -> None:
    """Record a template lifecycle event: journald + the template audit DB
    (+ the broker audit table for security decisions). Best-effort — a
    logging failure must never break the lifecycle operation it records."""
    try:
        _to_journald(event, fields)
    except Exception:  # noqa: BLE001
        pass
    try:
        log = TemplateAuditLog(db_path)
        try:
            log.log(event, **fields)
        finally:
            log.close()
    except Exception as exc:  # noqa: BLE001
        _best_effort_stderr(f"[qdistro-template-audit] WARN: DB write failed: {exc}")
    if broker and event in _BROKER_FORWARD:
        try:
            _forward_to_broker(event, fields)
        except Exception:  # noqa: BLE001
            pass
