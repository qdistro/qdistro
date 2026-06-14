"""Audit trail for the template lifecycle (todo/fableplan task 06).

**Journal-first.** The authoritative, always-present audit sink is the
system journal: every lifecycle event is emitted as structured journald
fields (`doc/templates.md` — "decisions append to the existing audit
journal"), and the on-disk evidence/ tree plus the binding activation
marker are the durable record of what happened. A queryable SQLite mirror
(`/var/lib/qdistro/audit/template_audit.sqlite`, alongside the broker
permission audit and the workflow/print audit DBs) is written *best-effort*
on top of that, so `journalctl` and the admin UI both see the events.

The SQLite mirror is only writable from a privileged/root-bypass context (or
a test/admin-owned `QDISTRO_VAR_DIR`). On a real install the shared audit
store is `qdistro-pwd:0700` — it is deliberately closed to admin so an
unprivileged user cannot tamper with the root/pwd audit trails (see
`scripts/install/install-templates-for-vm.sh`). The admin-context lifecycle
emitters (the launch anchor `qdistro-resolve-binding`, promote, state
snapshot, and GC, all run under rootless podman / as admin) therefore cannot
open the mirror; that is an expected **journal-first downgrade**, not data
loss — the event is already in the journal. Security-relevant decisions
(promote applied/refused, GC deletions) are additionally forwarded
best-effort into the broker's audit table when that store is reachable.

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
    # fableplan2 task 05: pre-activation state snapshots + crash-consistent
    # rollback restore. created fires at the launch anchor (before the new
    # generation's first write); restored fires when promote --rollback
    # --restore-state swaps a snapshot back into the live state.
    "template.state_snapshot.created",
    "template.state_snapshot.restored",
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
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()


def _best_effort_stderr(msg: str) -> None:
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — even logging the log failure is best-effort
        pass


# Paths already reported as journal-first this process, so a long-lived
# emitter does not print the downgrade notice on every single event.
_JOURNAL_FIRST_NOTED: set[str] = set()


def _is_store_unwritable(exc: Exception) -> bool:
    """True when the SQLite mirror could not be opened because the shared
    audit store is not writable from this context (admin into the pwd-owned
    0700 dir) — as opposed to a genuine DB fault (corruption, disk full) that
    should stay loud. PermissionError is unambiguous; sqlite surfaces the same
    condition as an OperationalError whose message names an open/readonly
    failure."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        return ("unable to open database file" in msg
                or "readonly database" in msg
                or "attempt to write a readonly database" in msg
                or "permission denied" in msg)
    return False


def _note_journal_first(db_path: str, exc: Exception) -> None:
    """Record the journal-first downgrade once per DB path per process. The
    event itself already reached the journal (emitted before the mirror write),
    so this is an informational notice, not a data-loss warning."""
    if db_path in _JOURNAL_FIRST_NOTED:
        return
    _JOURNAL_FIRST_NOTED.add(db_path)
    _best_effort_stderr(
        f"[qdistro-template-audit] journal-first: {db_path} not writable from "
        f"this context ({exc}); template lifecycle events are recorded to the "
        f"journal + on-disk evidence, the authoritative audit sink.")


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


def _decision(result: str | None) -> bool:
    # Classify by semantics, not string accident: refused/failed are denials;
    # applied and deleted are successful, deliberate security actions (a GC
    # deletion is an enforced retention action, not a denial).
    return result not in ("refused", "failed")


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
        decision = _decision(fields.get("result"))
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
    """Record a template lifecycle event: journald (the authoritative sink) +
    a best-effort SQLite mirror (+ the broker audit table for security
    decisions). Best-effort — a logging failure must never break the lifecycle
    operation it records, and an unwritable shared mirror is a journal-first
    downgrade, not an error (see module docstring)."""
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
        if _is_store_unwritable(exc):
            _note_journal_first(db_path or _default_db_path(), exc)
        else:
            _best_effort_stderr(f"[qdistro-template-audit] WARN: DB write failed: {exc}")
    if broker and event in _BROKER_FORWARD:
        try:
            _forward_to_broker(event, fields)
        except Exception:  # noqa: BLE001
            pass
