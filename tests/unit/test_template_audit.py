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


def test_decision_classifies_by_semantics():
    # Successful security actions forward as decision=True; only refused/failed
    # are denials. A GC deletion (result="deleted") is a deliberate enforced
    # action, not a denial — it must NOT classify as decision=False.
    assert audit._decision("applied") is True
    assert audit._decision("deleted") is True
    assert audit._decision("refused") is False
    assert audit._decision("failed") is False


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


def test_store_unwritable_classification():
    # The pwd-owned shared store surfaces as PermissionError or a sqlite
    # "unable to open"/readonly OperationalError — those are the journal-first
    # downgrade. A genuine fault (corruption, disk full) must stay loud.
    import sqlite3
    assert audit._is_store_unwritable(PermissionError(13, "Permission denied"))
    assert audit._is_store_unwritable(
        sqlite3.OperationalError("unable to open database file"))
    assert audit._is_store_unwritable(
        sqlite3.OperationalError("attempt to write a readonly database"))
    assert not audit._is_store_unwritable(
        sqlite3.OperationalError("database disk image is malformed"))
    # sqlite commonly raises a disk I/O fault as OperationalError, not just
    # DatabaseError — neither must be silenced as journal-first.
    assert not audit._is_store_unwritable(sqlite3.OperationalError("disk I/O error"))
    assert not audit._is_store_unwritable(sqlite3.DatabaseError("disk I/O error"))


def test_emit_journal_first_when_store_unwritable(tmp_path, monkeypatch, capsys):
    # On a real install the admin-context emitter cannot open the pwd-owned
    # shared mirror. emit() must NOT raise, must NOT print a scary "DB write
    # failed" WARN, must still reach the journal, and should notice the
    # downgrade exactly once per db path.
    import sqlite3
    journaled = []
    monkeypatch.setattr(audit, "_to_journald",
                        lambda event, fields: journaled.append((event, dict(fields))))

    def _deny(_db_path):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(audit, "TemplateAuditLog", _deny)
    monkeypatch.setattr(audit, "_JOURNAL_FIRST_NOTED", set())

    db = _db(tmp_path)
    audit.emit("template.binding.activated", db_path=db, broker=False,
               silo="dev-silo", template="tier2-dev",
               generation="sha256:" + "e" * 64, result="activated")
    audit.emit("template.state_snapshot.created", db_path=db, broker=False,
               silo="dev-silo", template="tier2-dev",
               generation="sha256:" + "f" * 64, result="created")

    # Both events reached the journal (the authoritative sink).
    assert [e for e, _ in journaled] == ["template.binding.activated",
                                         "template.state_snapshot.created"]
    assert journaled[0][1]["silo"] == "dev-silo"
    err = capsys.readouterr().err
    # No alarming WARN; a single journal-first notice for the path.
    assert "WARN: DB write failed" not in err
    assert err.count("journal-first") == 1


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="root ignores directory write bits")
def test_emit_real_unwritable_dir_is_journal_first(tmp_path, monkeypatch, capsys):
    # Prove the REAL errno path (not a monkeypatched message): a write-denied
    # audit dir — the way the qdistro-pwd:0700 shared store presents to an
    # admin-context emitter — must classify as journal-first, not a WARN.
    journaled = []
    monkeypatch.setattr(audit, "_to_journald",
                        lambda event, fields: journaled.append(event))
    monkeypatch.setattr(audit, "_JOURNAL_FIRST_NOTED", set())
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    os.chmod(audit_dir, 0o500)  # r-x: cannot create/open the DB file inside
    try:
        audit.emit("template.binding.activated",
                   db_path=str(audit_dir / "template_audit.sqlite"),
                   broker=False, silo="dev-silo", template="tier2-dev",
                   generation="sha256:" + "a" * 64, result="activated")
    finally:
        os.chmod(audit_dir, 0o700)  # let tmp_path cleanup remove it
    assert journaled == ["template.binding.activated"]
    err = capsys.readouterr().err
    assert "WARN: DB write failed" not in err
    assert err.count("journal-first") == 1


def test_emit_genuine_db_fault_stays_loud(tmp_path, monkeypatch, capsys):
    # A real DB fault on a writable store must NOT be silenced as journal-first.
    import sqlite3
    monkeypatch.setattr(audit, "_to_journald", lambda event, fields: None)

    def _corrupt(_db_path):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(audit, "TemplateAuditLog", _corrupt)
    audit.emit("template.gc.deleted", db_path=_db(tmp_path), broker=False,
               template="t", generation="sha256:" + "a" * 64, result="deleted")
    err = capsys.readouterr().err
    assert "WARN: DB write failed" in err
    assert "journal-first" not in err


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
