"""qdistro_print_audit — sqlite audit log tests."""
from __future__ import annotations

import os

import pytest

from qdistro_print_audit import PrintAuditLog


@pytest.fixture
def audit(tmp_path):
    return PrintAuditLog(str(tmp_path / "print_audit.sqlite"))


def test_creates_db(audit, tmp_path):
    assert os.path.exists(str(tmp_path / "print_audit.sqlite"))
    assert audit.count() == 0


def test_record_returns_id(audit):
    rid = audit.record("connect", decision="allow", reason="gate-disabled",
                       caller_uid=1000, caller_pid=4242,
                       caller_exe="/usr/bin/lp", backend="vsock",
                       bytes_to_be=1024, bytes_from_be=512)
    assert rid > 0
    assert audit.count() == 1


def test_tail_returns_rows_in_lifo(audit):
    audit.record("connect", caller_uid=1, decision="allow", reason="r1")
    audit.record("connect", caller_uid=2, decision="allow", reason="r2")
    audit.record("connect", caller_uid=3, decision="allow", reason="r3")
    rows = audit.tail(10)
    assert len(rows) == 3
    assert [r["caller_uid"] for r in rows] == [3, 2, 1]


def test_tail_limit_respected(audit):
    for i in range(5):
        audit.record("connect", caller_uid=i, decision="allow", reason="x")
    rows = audit.tail(2)
    assert len(rows) == 2


def test_optional_fields_default_to_none_or_zero(audit):
    audit.record("close", decision="allow", reason="end")
    rows = audit.tail(1)
    r = rows[0]
    assert r["op"] == "close"
    assert r["decision"] == "allow"
    assert r["reason"] == "end"
    assert r["caller_uid"] is None
    assert r["caller_pid"] is None
    assert r["caller_exe"] == ""
    assert r["backend"] == ""
    assert r["bytes_to_be"] is None
    assert r["bytes_from_be"] is None


def test_default_path_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("QDISTRO_PRINT_AUDIT_DB",
                       str(tmp_path / "from_env.sqlite"))
    a = PrintAuditLog()
    assert a.db_path == str(tmp_path / "from_env.sqlite")
    assert os.path.exists(a.db_path)


def test_indexes_created(audit):
    """Sanity-check that the ts index exists for tail() perf."""
    import sqlite3
    con = sqlite3.connect(audit.db_path)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='print_audit'"
        ).fetchall()
        names = {r[0] for r in rows}
        assert "print_audit_ts_idx" in names
    finally:
        con.close()
