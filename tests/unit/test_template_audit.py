"""Unit tests for the template lifecycle audit log (todo/fableplan task 06)."""
from __future__ import annotations

import os

import pytest

import qdistro_template_audit as audit


def _db(tmp_path):
    return str(tmp_path / "audit" / "template_audit.sqlite")


def test_log_and_recent_roundtrip(tmp_path):
    log = audit.TemplateAuditLog(_db(tmp_path))
    try:
        log.log("template.build.finished", template="tier2-dev",
                run_id="r1", generation="sha256:" + "a" * 64,
                result="success", duration=12.5, network_mode="unrestricted")
        log.log("template.promote.applied", silo="dev-silo",
                template="tier2-dev", generation="sha256:" + "b" * 64,
                result="applied")
        rows = log.recent()
    finally:
        log.close()
    assert rows[0]["event"] == "template.promote.applied"
    assert rows[0]["silo"] == "dev-silo"
    assert rows[1]["event"] == "template.build.finished"
    assert rows[1]["duration_seconds"] == 12.5
    assert rows[1]["network_mode"] == "unrestricted"


def test_db_is_owner_only(tmp_path):
    path = _db(tmp_path)
    log = audit.TemplateAuditLog(path)
    log.close()
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_migration_adds_columns_to_old_db(tmp_path):
    # A DB created before the old/new-generation columns existed must gain
    # them on open (additive ALTER TABLE), not start failing inserts.
    import sqlite3
    path = _db(tmp_path)
    os.makedirs(os.path.dirname(path))
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE template_audit (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL, event TEXT NOT NULL,
            silo TEXT, template TEXT, generation TEXT, run_id TEXT,
            result TEXT, duration_seconds REAL, reason TEXT,
            evidence_path TEXT, network_mode TEXT, extra TEXT);
    """)
    conn.commit()
    conn.close()
    # Opening through TemplateAuditLog migrates; a write using a new column works.
    log = audit.TemplateAuditLog(path)
    try:
        log.log("template.promote.applied", template="tier2-dev",
                old_generation="sha256:" + "a" * 64,
                new_generation="sha256:" + "b" * 64, identity_revision=2,
                result="applied")
        row = log.recent()[0]
    finally:
        log.close()
    assert row["old_generation"] == "sha256:" + "a" * 64
    assert row["new_generation"] == "sha256:" + "b" * 64
    assert row["identity_revision"] == 2


def test_unknown_event_rejected(tmp_path):
    log = audit.TemplateAuditLog(_db(tmp_path))
    try:
        with pytest.raises(ValueError):
            log.log("template.bogus.event", template="t")
    finally:
        log.close()


def test_emit_best_effort_does_not_raise(tmp_path, monkeypatch):
    # emit must never raise even if journald is unavailable; it writes the DB.
    db = _db(tmp_path)
    audit.emit("template.gc.deleted", db_path=db, broker=False,
               template="tier2-dev", generation="sha256:" + "c" * 64,
               result="deleted", reason="retention",
               evidence_path="/var/lib/qdistro/templates/tier2-dev/generations/x/evidence")
    log = audit.TemplateAuditLog(db)
    try:
        rows = log.recent()
    finally:
        log.close()
    assert rows[0]["event"] == "template.gc.deleted"
    # evidence path survives in the deletion event
    assert rows[0]["evidence_path"].endswith("/evidence")


def test_emit_deletion_records_surviving_evidence(tmp_path):
    # Evidence outlives payload: a deletion event must reference the
    # evidence that remains.
    db = _db(tmp_path)
    audit.emit("template.gc.deleted", db_path=db, broker=False,
               template="tier2-dev", generation="sha256:" + "d" * 64,
               result="deleted", reason="failed-candidate-expired",
               evidence_path="/path/to/evidence")
    log = audit.TemplateAuditLog(db)
    try:
        row = log.recent()[0]
    finally:
        log.close()
    assert row["evidence_path"] == "/path/to/evidence"
    assert row["reason"] == "failed-candidate-expired"
