"""Tests for the qdistro-approvals CLI.

Run the script as a subprocess with a fake euid==0 (we monkeypatch
os.geteuid via env at import time). Each test gets its own temp cache
+ audit db, monkeypatched into the module's globals.

These cover argparse wiring, output shape, and side effects on the dbs.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from qdistro_admin_cache import ApprovalCache
from qdistro_admin_audit import AuditLog
import qdistro_approvals as cli


@pytest.fixture
def populated(cache_db, audit_db, monkeypatch):
    """Cache with two rows, audit with three rows; CLI globals pointed at them."""
    monkeypatch.setattr(cli, "CACHE_DB", cache_db)
    monkeypatch.setattr(cli, "AUDIT_DB", audit_db)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)

    c = ApprovalCache(cache_db)
    c.store(2000, "test.action", "/usr/bin/python3", "1h", True, 1000,
            argv=["/usr/bin/python3"])
    c.store(2001, "other.action", "", "forever", True, 1000)

    a = AuditLog(audit_db)
    a.log(caller_uid=2000, caller_pid=1234, caller_exe="/usr/bin/python3",
          action="test.action", decision=True, scope="1h",
          source="prompt", approver_uid=1000)
    a.log(caller_uid=2000, caller_pid=1235, caller_exe="/usr/bin/python3",
          action="test.action", decision=True, scope="1h",
          source="cache", approver_uid=None)
    a.log(caller_uid=2001, caller_pid=42, caller_exe="/usr/bin/curl",
          action="other.action", decision=False, scope="once",
          source="prompt", approver_uid=1000)

    return cache_db, audit_db


def _run(argv, capsys):
    """Invoke the CLI's main() with sys.argv = argv, return (rc, stdout, stderr)."""
    old_argv = sys.argv
    sys.argv = ["qdistro-approvals"] + argv
    try:
        rc = cli.main()
    except SystemExit as e:
        rc = int(e.code) if e.code is not None else 0
    finally:
        sys.argv = old_argv
    out = capsys.readouterr()
    return rc, out.out, out.err


class _FakeBrokerIface:
    """Stand-in for the dbus.Interface returned by cli._broker().

    Mutates the test's own ApprovalCache / AuditLog instances so the
    broker-mediated writes land in the same dbs the fixture populated.
    Mirrors the broker's method semantics (bool / int, no exception on
    empty-set).
    """
    def __init__(self, cache_db: str, audit_db: str | None = None):
        from qdistro_admin_cache import ApprovalCache
        from qdistro_admin_audit import AuditLog
        self._cache = ApprovalCache(cache_db)
        self._audit = AuditLog(audit_db) if audit_db else None

    def RevokeApproval(self, approval_id: int) -> bool:
        return self._cache.delete_by_id(int(approval_id)) is not None

    def RevokeAllForUid(self, caller_uid: int) -> int:
        return len(self._cache.delete_by_uid(int(caller_uid)))

    def RunAuditGc(self, retention_days: int) -> int:
        if self._audit is None:
            raise RuntimeError("fake broker not wired for audit")
        return self._audit.gc(int(retention_days) * 86400)

    def RunCacheGc(self) -> int:
        return self._cache.gc()


class _FakeDbusModule:
    """Minimal stand-in for the dbus module; only DBusException is needed."""
    class DBusException(Exception):
        pass


@pytest.fixture
def fake_broker(monkeypatch):
    """Install a fake `_broker()` that returns a FakeBrokerIface bound to
    the CLI's current CACHE_DB + AUDIT_DB. Call after those are monkeypatched."""
    def _install():
        iface = _FakeBrokerIface(cli.CACHE_DB, cli.AUDIT_DB)
        monkeypatch.setattr(cli, "_broker", lambda: (iface, _FakeDbusModule))
        return iface
    return _install


# --- root check -----------------------------------------------------------

def test_refuses_non_root(monkeypatch, capsys):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    rc, _out, err = _run(["list"], capsys)
    assert rc == 1
    assert "must be run as root" in err


# --- list -----------------------------------------------------------------

def test_list_shows_cached_rows(populated, capsys):
    rc, out, _ = _run(["list"], capsys)
    assert rc == 0
    assert "test.action" in out
    assert "other.action" in out
    assert "argv_exact" in out
    assert "always" in out


def test_list_empty_cache_message(cache_db, audit_db, monkeypatch, capsys):
    monkeypatch.setattr(cli, "CACHE_DB", cache_db)
    monkeypatch.setattr(cli, "AUDIT_DB", audit_db)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    ApprovalCache(cache_db)  # create empty db
    AuditLog(audit_db)
    rc, out, _ = _run(["list"], capsys)
    assert rc == 0
    assert "no cached approvals" in out


def test_list_db_missing_exits_2(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "CACHE_DB", str(tmp_path / "nope.sqlite"))
    monkeypatch.setattr(cli, "AUDIT_DB", str(tmp_path / "nope2.sqlite"))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    rc, _out, err = _run(["list"], capsys)
    assert rc == 2
    assert "db not found" in err


# --- revoke ---------------------------------------------------------------

def test_revoke_by_id_deletes_row(populated, fake_broker, capsys):
    cache_db, _audit = populated
    fake_broker()
    rc, out, _ = _run(["revoke", "1"], capsys)
    assert rc == 0
    assert "revoked approval id=1" in out
    n = sqlite3.connect(cache_db).execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
    assert n == 1


def test_revoke_unknown_id_exits_1(populated, fake_broker, capsys):
    fake_broker()
    rc, _out, err = _run(["revoke", "999"], capsys)
    assert rc == 1
    assert "no cached approval with id=999" in err


def test_revoke_all_for_uid_bulk(populated, fake_broker, capsys):
    cache_db, _audit = populated
    fake_broker()
    rc, out, _ = _run(["revoke", "--all-for-uid", "2000"], capsys)
    assert rc == 0
    assert "revoked 1 approval(s) for uid=2000" in out
    remaining = sqlite3.connect(cache_db).execute(
        "SELECT caller_uid FROM approvals"
    ).fetchall()
    assert remaining == [(2001,)]


def test_revoke_with_no_args_exits_2(populated, capsys):
    rc, _out, err = _run(["revoke"], capsys)
    assert rc == 2
    # argparse's mutually_exclusive_group(required=True) message
    assert "one of" in err or "required" in err


def test_revoke_id_and_uid_together_rejected(populated, capsys):
    """Mutually-exclusive group: passing both should fail at argparse."""
    rc, _out, err = _run(["revoke", "1", "--all-for-uid", "2000"], capsys)
    assert rc == 2
    assert "not allowed" in err.lower() or "argument" in err.lower()


# --- audit-gc -------------------------------------------------------------

def test_audit_gc_deletes_old_and_prints_count(populated, fake_broker, capsys):
    _cache, audit_db = populated
    # Backdate one of the three fixture audit rows to 100 days ago.
    with sqlite3.connect(audit_db) as db:
        db.execute(
            "UPDATE audit SET ts = ? WHERE caller_uid = 2001",
            (int(time.time()) - 100 * 86400,),
        )
        db.commit()
    fake_broker()
    rc, out, _ = _run(["audit-gc", "--retention-days", "30"], capsys)
    assert rc == 0
    assert "deleted 1 row(s) older than 30d" in out
    remaining = sqlite3.connect(audit_db).execute(
        "SELECT COUNT(*) FROM audit").fetchone()[0]
    assert remaining == 2


def test_audit_gc_default_retention_is_90(populated, fake_broker, capsys):
    fake_broker()
    rc, out, _ = _run(["audit-gc"], capsys)
    assert rc == 0
    assert "older than 90d" in out


def test_open_rejects_symlink_to_other_file(tmp_path, monkeypatch, capsys):
    """CLI _open must refuse to follow a symlink (P1: defends against
    an attacker swapping the db for a symlink to /etc/shadow)."""
    fake_target = tmp_path / "innocent.sqlite"
    fake_target.write_bytes(b"")
    os.chown(str(fake_target), 0, 0) if os.geteuid() == 0 else None
    link = tmp_path / "approvals.sqlite"
    link.symlink_to(fake_target)
    monkeypatch.setattr(cli, "CACHE_DB", str(link))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    rc, _out, err = _run(["list"], capsys)
    assert rc == 3
    assert "not a regular file" in err


# --- audit ----------------------------------------------------------------

def test_audit_default_shows_all(populated, capsys):
    rc, out, _ = _run(["audit"], capsys)
    assert rc == 0
    # newest-first; uid 2001 deny was most recent, then two uid 2000 rows
    assert out.count("test.action") >= 2
    assert "other.action" in out
    assert "prompt" in out and "cache" in out


def test_audit_uid_filter(populated, capsys):
    rc, out, _ = _run(["audit", "--uid", "2001"], capsys)
    assert "other.action" in out
    assert "test.action" not in out


def test_audit_action_filter(populated, capsys):
    rc, out, _ = _run(["audit", "--action", "other.action"], capsys)
    assert "other.action" in out
    assert "test.action" not in out


def test_audit_limit_caps_rows(populated, capsys):
    rc, out, _ = _run(["audit", "--limit", "1"], capsys)
    # one data row = one main line + one continuation 'exe=' line
    data_lines = [l for l in out.splitlines()
                  if l and not l.startswith("when") and not l.startswith("-")
                  and not l.startswith("  ")]
    assert len(data_lines) == 1


def test_audit_no_match_message(populated, capsys):
    rc, out, _ = _run(["audit", "--uid", "9999"], capsys)
    assert rc == 0
    assert "no matching audit rows" in out


# --- gc -------------------------------------------------------------------

def test_gc_deletes_expired_rows_only(cache_db, audit_db, monkeypatch,
                                       fake_broker, capsys):
    monkeypatch.setattr(cli, "CACHE_DB", cache_db)
    monkeypatch.setattr(cli, "AUDIT_DB", audit_db)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    AuditLog(audit_db)
    c = ApprovalCache(cache_db)
    # one expires-soon (already expired by hand), one persistent
    now = int(time.time())
    c._conn.execute(
        "INSERT INTO approvals (caller_uid, action, match_kind, match_value, "
        "decision, expires_at, created_at, approver_uid) VALUES "
        "(?, ?, 'argv_exact', '/p', 1, ?, ?, 1000)",
        (2000, "expired", now - 60, now - 3660),
    )
    c.store(2000, "kept", "/p", "forever", True, 1000)
    fake_broker()  # wire the CLI's _broker() to our fake cache/audit

    rc, out, _ = _run(["gc"], capsys)
    assert rc == 0
    assert "deleted 1 expired" in out
    remaining = sqlite3.connect(cache_db).execute(
        "SELECT action FROM approvals").fetchall()
    assert remaining == [("kept",)]
