"""Per-workflow audit trail.

Writes workflow run records either to the broker's existing audit
system (via AuditLog) or to a dedicated local SQLite database
when running standalone. Follows the same SQLite patterns as
qdistro_admin_audit.py: WAL mode, busy timeout, restrictive perms.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

WORKFLOW_AUDIT_DB_DEFAULT = "/var/lib/qdistro/audit/workflow_audit.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id         TEXT PRIMARY KEY,
    workflow_name  TEXT NOT NULL,
    state          TEXT NOT NULL,
    started_at     REAL,
    completed_at   REAL,
    trigger_context TEXT,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS wf_runs_name ON workflow_runs(workflow_name);
CREATE INDEX IF NOT EXISTS wf_runs_started ON workflow_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS workflow_steps (
    id             INTEGER PRIMARY KEY,
    run_id         TEXT NOT NULL,
    step_name      TEXT NOT NULL,
    step_type      TEXT NOT NULL,
    success        INTEGER NOT NULL,
    started_at     REAL,
    completed_at   REAL,
    error          TEXT,
    details        TEXT,
    FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id)
);
CREATE INDEX IF NOT EXISTS wf_steps_run ON workflow_steps(run_id);

CREATE TABLE IF NOT EXISTS workflow_audit (
    id             INTEGER PRIMARY KEY,
    ts             REAL NOT NULL,
    run_id         TEXT NOT NULL,
    workflow_name  TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    message        TEXT,
    details        TEXT,
    FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id)
);
CREATE INDEX IF NOT EXISTS wf_audit_ts ON workflow_audit(ts DESC);
CREATE INDEX IF NOT EXISTS wf_audit_run ON workflow_audit(run_id);
"""


class WorkflowAuditLogger:
    """Audit logger for workflow runs.

    Writes to a local SQLite database. Can also forward entries to the
    broker's AuditLog if one is provided.
    """

    def __init__(
        self,
        db_path: str = WORKFLOW_AUDIT_DB_DEFAULT,
        broker_audit: Any | None = None,
    ):
        self.db_path = db_path
        self._broker_audit = broker_audit
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        old_umask = os.umask(0o077)
        try:
            self._conn = sqlite3.connect(
                db_path, isolation_level=None, check_same_thread=False,
            )
        finally:
            os.umask(old_umask)
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass

    def log_run_start(self, run_id: str, workflow_name: str,
                      trigger_context: dict[str, Any]) -> None:
        """Record the start of a workflow run."""
        now = time.time()
        self._conn.execute(
            """INSERT INTO workflow_runs
               (run_id, workflow_name, state, started_at, trigger_context)
               VALUES (?, ?, 'running', ?, ?)""",
            (run_id, workflow_name, now,
             json.dumps(trigger_context, default=str)),
        )
        self._log_event(run_id, workflow_name, "run_start",
                        f"workflow {workflow_name} started")
        self._forward_to_broker(
            action=f"qdistro.workflow.run_start:{workflow_name}",
            source=f"run_id={run_id}",
        )

    def log_run_complete(self, run_id: str, workflow_name: str) -> None:
        """Record the successful completion of a workflow run."""
        now = time.time()
        self._conn.execute(
            """UPDATE workflow_runs
               SET state='completed', completed_at=?
               WHERE run_id=?""",
            (now, run_id),
        )
        self._log_event(run_id, workflow_name, "run_complete",
                        f"workflow {workflow_name} completed")
        self._forward_to_broker(
            action=f"qdistro.workflow.run_complete:{workflow_name}",
            source=f"run_id={run_id}",
        )

    def log_run_failed(self, run_id: str, workflow_name: str,
                       error: str) -> None:
        """Record a failed workflow run."""
        now = time.time()
        self._conn.execute(
            """UPDATE workflow_runs
               SET state='failed', completed_at=?, error=?
               WHERE run_id=?""",
            (now, error, run_id),
        )
        self._log_event(run_id, workflow_name, "run_failed",
                        f"workflow {workflow_name} failed: {error}")
        self._forward_to_broker(
            action=f"qdistro.workflow.run_failed:{workflow_name}",
            source=f"run_id={run_id} error={error[:256]}",
            decision=False,
        )

    def log_step(self, run_id: str, step_name: str, step_type: str,
                 success: bool, started_at: float, completed_at: float,
                 error: str = "",
                 details: dict[str, Any] | None = None) -> None:
        """Record the outcome of a single step execution."""
        self._conn.execute(
            """INSERT INTO workflow_steps
               (run_id, step_name, step_type, success, started_at,
                completed_at, error, details)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, step_name, step_type, 1 if success else 0,
             started_at, completed_at, error,
             json.dumps(details or {}, default=str)),
        )

    def log_secret_scrub(self, run_id: str, workflow_name: str,
                         secret_item: str) -> None:
        """Record that a delivered secret was scrubbed."""
        self._log_event(run_id, workflow_name, "secret_scrub",
                        f"scrubbed secret: {secret_item}")

    def recent_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the N most-recent workflow runs."""
        limit = max(1, min(int(limit), 10000))
        cur = self._conn.execute(
            """SELECT run_id, workflow_name, state, started_at,
                      completed_at, trigger_context, error
               FROM workflow_runs ORDER BY started_at DESC LIMIT ?""",
            (limit,),
        )
        return [
            {
                "run_id": r[0],
                "workflow_name": r[1],
                "state": r[2],
                "started_at": r[3],
                "completed_at": r[4],
                "trigger_context": r[5],
                "error": r[6],
            }
            for r in cur.fetchall()
        ]

    def run_steps(self, run_id: str) -> list[dict[str, Any]]:
        """Return all steps for a given run."""
        cur = self._conn.execute(
            """SELECT step_name, step_type, success, started_at,
                      completed_at, error, details
               FROM workflow_steps WHERE run_id=?
               ORDER BY id""",
            (run_id,),
        )
        return [
            {
                "step_name": r[0],
                "step_type": r[1],
                "success": bool(r[2]),
                "started_at": r[3],
                "completed_at": r[4],
                "error": r[5],
                "details": r[6],
            }
            for r in cur.fetchall()
        ]

    def gc(self, older_than_seconds: int) -> int:
        """Delete run records older than the given threshold."""
        cutoff = time.time() - older_than_seconds
        # Delete steps for old runs first.
        self._conn.execute(
            """DELETE FROM workflow_steps
               WHERE run_id IN (
                   SELECT run_id FROM workflow_runs WHERE started_at < ?
               )""",
            (cutoff,),
        )
        self._conn.execute(
            """DELETE FROM workflow_audit
               WHERE run_id IN (
                   SELECT run_id FROM workflow_runs WHERE started_at < ?
               )""",
            (cutoff,),
        )
        cur = self._conn.execute(
            "DELETE FROM workflow_runs WHERE started_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0

    def close(self) -> None:
        self._conn.close()

    def _log_event(self, run_id: str, workflow_name: str,
                   event_type: str, message: str,
                   details: dict[str, Any] | None = None) -> None:
        """Write one row to the workflow_audit table."""
        self._conn.execute(
            """INSERT INTO workflow_audit
               (ts, run_id, workflow_name, event_type, message, details)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (time.time(), run_id, workflow_name, event_type, message,
             json.dumps(details or {}, default=str)),
        )

    def _forward_to_broker(self, *, action: str, source: str,
                           decision: bool = True) -> None:
        """Best-effort forward to broker audit if available."""
        if self._broker_audit is None:
            return
        try:
            self._broker_audit.log(
                caller_uid=0, caller_pid=os.getpid(),
                caller_exe="qdistro-workflow-engine",
                action=action, decision=decision, scope=None,
                source=source, approver_uid=None,
            )
        except Exception:  # noqa: BLE001
            pass
