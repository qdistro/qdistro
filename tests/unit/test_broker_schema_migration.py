"""Schema-migration / backward-compat tests for the broker SQLite stores.

`test_cache.py` and `test_audit.py` already cover the lookup/store/gc
behavior and the one already-tested cache migration case
(legacy `argv_exact` rows → `exe_only`). This file fills the gap called
out in todo/codex-testing/under-tested-areas.md §6: schema CREATION,
idempotent RE-OPEN, and forward/backward COMPATIBILITY of the two
stores' on-disk schema.

What the code actually implements (read from
broker/qdistro_admin_cache.py + broker/qdistro_admin_audit.py):

  * Both open via `executescript(SCHEMA)` with `CREATE TABLE IF NOT
    EXISTS`, then run an additive `_migrate()` that ALTERs in any
    missing columns by introspecting `PRAGMA table_info`.
  * Neither store uses `PRAGMA user_version` / any schema_version
    marker — migration keys off the present column set, so user_version
    stays at its sqlite default of 0. These tests assert that real
    contract rather than an aspirational version counter.
  * The audit `_migrate` adds rule_path, request_id, selinux_subj_type,
    argv if absent. The cache `_migrate` adds scope, then argv (and
    remaps legacy argv_exact rows).
  * Extra/unknown columns are tolerated: `CREATE TABLE IF NOT EXISTS`
    is a no-op on an existing table and the column-presence checks only
    ADD missing columns, never DROP, so forward-compat dbs open fine.
"""
from __future__ import annotations

import sqlite3

import pytest

from qdistro_admin_audit import AuditLog
from qdistro_admin_cache import ApprovalCache


# --- helpers --------------------------------------------------------------

def _cols(db_path: str, table: str) -> dict[str, dict]:
    """Return {column_name: pragma_row_dict} for a table."""
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        con.close()
    # PRAGMA table_info cols: cid, name, type, notnull, dflt_value, pk
    return {
        r[1]: {"type": r[2], "notnull": r[3], "default": r[4], "pk": r[5]}
        for r in rows
    }


def _indexes(db_path: str) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_autoindex_%'"
            ).fetchall()
        }
    finally:
        con.close()


def _user_version(db_path: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


# Full expected column sets per the current SCHEMA + _migrate additions.
CACHE_COLS = {
    "id", "caller_uid", "action", "match_kind", "match_value", "argv",
    "decision", "expires_at", "created_at", "approver_uid", "scope",
}
AUDIT_COLS = {
    "id", "ts", "caller_uid", "caller_pid", "caller_exe", "action",
    "decision", "scope", "source", "approver_uid", "rule_path",
    "request_id", "selinux_subj_type", "argv",
}


# --- fresh creation -------------------------------------------------------

class TestFreshCreation:
    def test_cache_fresh_path_creates_full_schema(self, cache_db):
        ApprovalCache(cache_db)
        cols = _cols(cache_db, "approvals")
        assert set(cols) == CACHE_COLS
        # The argv column carries a NOT NULL DEFAULT '' so legacy/argv-less
        # inserts don't violate the constraint.
        assert cols["argv"]["notnull"] == 1
        assert cols["argv"]["default"] == "''"
        # Lookup index from SCHEMA must exist.
        assert "approvals_lookup" in _indexes(cache_db)

    def test_audit_fresh_path_creates_full_schema(self, audit_db):
        AuditLog(audit_db)
        cols = _cols(audit_db, "audit")
        assert set(cols) == AUDIT_COLS
        assert _indexes(audit_db) >= {"audit_ts", "audit_caller", "audit_action"}

    def test_cache_does_not_use_user_version(self, cache_db):
        """No schema_version marker is used; sqlite default stays 0."""
        ApprovalCache(cache_db)
        assert _user_version(cache_db) == 0

    def test_audit_does_not_use_user_version(self, audit_db):
        AuditLog(audit_db)
        assert _user_version(audit_db) == 0


# --- idempotent re-open ---------------------------------------------------

class TestIdempotentReopen:
    def test_cache_reopen_preserves_schema_and_data(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/usr/bin/p", "forever_exe", True, 1000)
        cols_before = _cols(cache_db, "approvals")
        idx_before = _indexes(cache_db)
        c.close()

        c2 = ApprovalCache(cache_db)  # must not raise / must not wipe
        assert _cols(cache_db, "approvals") == cols_before
        assert _indexes(cache_db) == idx_before
        # Data survived the reopen.
        rows = c2.list_all()
        assert len(rows) == 1
        assert rows[0]["match_kind"] == "exe_only"
        assert c2.lookup(2000, "act", "/usr/bin/p") is True
        c2.close()

    def test_audit_reopen_preserves_schema_and_data(self, audit_db):
        a = AuditLog(audit_db)
        a.log(caller_uid=2000, caller_pid=1, caller_exe="/p", action="x",
              decision=True, scope="1h", source="prompt", approver_uid=1000)
        cols_before = _cols(audit_db, "audit")
        idx_before = _indexes(audit_db)
        a.close()

        a2 = AuditLog(audit_db)
        assert _cols(audit_db, "audit") == cols_before
        assert _indexes(audit_db) == idx_before
        assert len(a2.recent()) == 1
        a2.close()

    def test_cache_double_reopen_no_duplicate_columns(self, cache_db):
        """Re-running _migrate over an already-migrated db is a no-op:
        the column count stays constant across repeated opens."""
        for _ in range(3):
            ApprovalCache(cache_db).close()
        assert set(_cols(cache_db, "approvals")) == CACHE_COLS

    def test_audit_double_reopen_no_duplicate_columns(self, audit_db):
        for _ in range(3):
            AuditLog(audit_db).close()
        assert set(_cols(audit_db, "audit")) == AUDIT_COLS


# --- backward-compat: opening an OLD / minimal schema ---------------------

class TestCacheBackwardCompat:
    def _make_minimal_cache(self, db_path: str) -> None:
        """Pre-scope / pre-argv cache schema: the table existed before
        the `scope` and `argv` columns were added, so _migrate must add
        both. (test_cache.py only covers the argv-remap path; this
        covers the additive ALTER of the scope column too.)"""
        con = sqlite3.connect(db_path)
        con.execute("""
            CREATE TABLE approvals (
                id INTEGER PRIMARY KEY,
                caller_uid INTEGER NOT NULL,
                action TEXT NOT NULL,
                match_kind TEXT NOT NULL,
                match_value TEXT NOT NULL,
                decision INTEGER NOT NULL,
                expires_at INTEGER,
                created_at INTEGER NOT NULL,
                approver_uid INTEGER NOT NULL
            )
        """)
        con.commit()
        con.close()

    def test_minimal_schema_gains_scope_and_argv(self, cache_db):
        self._make_minimal_cache(cache_db)
        before = set(_cols(cache_db, "approvals"))
        assert "scope" not in before and "argv" not in before

        ApprovalCache(cache_db)  # triggers _migrate
        after = set(_cols(cache_db, "approvals"))
        assert "scope" in after
        assert "argv" in after
        assert after == CACHE_COLS

    def test_minimal_schema_preexisting_row_still_readable(self, cache_db):
        """A pre-migration row survives the additive ALTERs and reads
        back through the public API: the new argv column is backfilled
        with its NOT NULL DEFAULT '' and the new scope column is NULL.

        (The legacy argv_exact->exe_only *remap* is already covered by
        test_cache.py::test_legacy_argv_exact_rows_migrate_to_exe_only;
        this case is specifically about the additive scope+argv columns,
        so it inserts an already-exe_only row to keep the two distinct.)"""
        self._make_minimal_cache(cache_db)
        con = sqlite3.connect(cache_db)
        con.execute(
            "INSERT INTO approvals (caller_uid, action, match_kind, "
            "match_value, decision, expires_at, created_at, approver_uid) "
            "VALUES (2000, 'act', 'exe_only', '/usr/bin/p', 1, NULL, 0, 1000)"
        )
        con.commit()
        con.close()

        c = ApprovalCache(cache_db)
        rows = c.list_all()
        assert len(rows) == 1
        assert rows[0]["argv"] == ""        # NOT NULL DEFAULT '' applied
        assert rows[0]["scope"] is None     # added column, no backfill
        # exe_only row matches by exe regardless of argv.
        assert c.lookup(2000, "act", "/usr/bin/p") is True
        # New writes work against the migrated schema.
        assert c.store(3000, "act2", "/q", "forever", True, 1000) is True
        assert c.lookup(3000, "act2", "/anything") is True

    def test_migrated_db_survives_close_and_reopen(self, cache_db):
        """After migrating a minimal db, a second open is the idempotent
        path again — no further schema change, data intact."""
        self._make_minimal_cache(cache_db)
        c1 = ApprovalCache(cache_db)
        c1.store(2000, "act", "/usr/bin/p", "forever_exe", True, 1000)
        c1.close()
        cols_after_migrate = _cols(cache_db, "approvals")
        # The first open must have actually performed the full migration,
        # else the "idempotent reopen" claim is vacuous.
        assert set(cols_after_migrate) == CACHE_COLS

        c2 = ApprovalCache(cache_db)
        assert _cols(cache_db, "approvals") == cols_after_migrate
        # Data written before the reopen is still there.
        rows = c2.list_all()
        assert len(rows) == 1
        assert rows[0]["match_kind"] == "exe_only"
        assert c2.lookup(2000, "act", "/usr/bin/p") is True
        c2.close()


class TestAuditBackwardCompat:
    def _make_minimal_audit(self, db_path: str) -> None:
        """Pre-spec/30, pre-task(070) audit schema: lacks rule_path,
        request_id, selinux_subj_type, argv. _migrate adds all four.
        (Not covered by test_audit.py, which only opens fresh dbs.)"""
        con = sqlite3.connect(db_path)
        con.execute("""
            CREATE TABLE audit (
                id INTEGER PRIMARY KEY,
                ts INTEGER NOT NULL,
                caller_uid INTEGER NOT NULL,
                caller_pid INTEGER NOT NULL,
                caller_exe TEXT NOT NULL,
                action TEXT NOT NULL,
                decision INTEGER NOT NULL,
                scope TEXT,
                source TEXT NOT NULL,
                approver_uid INTEGER
            )
        """)
        con.commit()
        con.close()

    def test_minimal_schema_gains_all_new_columns(self, audit_db):
        self._make_minimal_audit(audit_db)
        before = set(_cols(audit_db, "audit"))
        for c in ("rule_path", "request_id", "selinux_subj_type", "argv"):
            assert c not in before

        AuditLog(audit_db)  # triggers _migrate
        after = set(_cols(audit_db, "audit"))
        assert after == AUDIT_COLS

    def test_minimal_schema_preexisting_row_readable_after_migrate(self, audit_db):
        self._make_minimal_audit(audit_db)
        con = sqlite3.connect(audit_db)
        con.execute(
            "INSERT INTO audit (ts, caller_uid, caller_pid, caller_exe, "
            "action, decision, scope, source, approver_uid) "
            "VALUES (1000, 2000, 5, '/usr/bin/p', 'act', 1, '1h', "
            "'prompt', 1000)"
        )
        con.commit()
        con.close()

        a = AuditLog(audit_db)
        rows = a.recent()
        assert len(rows) == 1
        r = rows[0]
        # Old data preserved.
        assert (r["caller_uid"], r["caller_exe"], r["action"]) == (
            2000, "/usr/bin/p", "act")
        # New columns are NULL on the pre-migration row.
        assert r["rule_path"] is None
        assert r["request_id"] is None
        assert r["selinux_subj_type"] is None
        assert r["argv"] is None
        # And new writes that USE the new columns work post-migration.
        a.log(caller_uid=2001, caller_pid=6, caller_exe="/usr/bin/apt-get",
              action="qdistro.sudo.exec", decision=True, scope="forever_argv",
              source="prompt", approver_uid=1000,
              argv=["/usr/bin/apt-get", "update"], request_id=7)
        newest = a.recent()[0]
        assert newest["argv"] == ["/usr/bin/apt-get", "update"]
        assert newest["request_id"] == 7

    def test_partial_schema_adds_only_missing_columns(self, audit_db):
        """Forward-stepping migration: a db that already has SOME of the
        added columns (rule_path) only gets the still-missing ones."""
        con = sqlite3.connect(audit_db)
        con.execute("""
            CREATE TABLE audit (
                id INTEGER PRIMARY KEY,
                ts INTEGER NOT NULL,
                caller_uid INTEGER NOT NULL,
                caller_pid INTEGER NOT NULL,
                caller_exe TEXT NOT NULL,
                action TEXT NOT NULL,
                decision INTEGER NOT NULL,
                scope TEXT,
                source TEXT NOT NULL,
                approver_uid INTEGER,
                rule_path TEXT
            )
        """)
        con.commit()
        con.close()

        AuditLog(audit_db)
        assert set(_cols(audit_db, "audit")) == AUDIT_COLS


# --- forward-compat: opening a db with EXTRA / unknown columns ------------

class TestForwardCompat:
    def test_cache_extra_column_opens_and_round_trips(self, cache_db):
        """A future db version added an extra column that carries a
        default (so the current code's known-column INSERTs stay valid).
        The current code must open it — CREATE TABLE IF NOT EXISTS is a
        no-op and _migrate only ADDs missing known columns, never drops —
        and keep working. (Contract limit: a future NOT NULL column
        WITHOUT a default would break store(); that's out of scope here.)"""
        con = sqlite3.connect(cache_db)
        con.execute("""
            CREATE TABLE approvals (
                id INTEGER PRIMARY KEY,
                caller_uid INTEGER NOT NULL,
                action TEXT NOT NULL,
                match_kind TEXT NOT NULL,
                match_value TEXT NOT NULL,
                argv TEXT NOT NULL DEFAULT '',
                decision INTEGER NOT NULL,
                expires_at INTEGER,
                created_at INTEGER NOT NULL,
                approver_uid INTEGER NOT NULL,
                scope TEXT,
                future_flag TEXT DEFAULT 'x'
            )
        """)
        con.commit()
        con.close()

        c = ApprovalCache(cache_db)  # must not raise
        cols = set(_cols(cache_db, "approvals"))
        assert "future_flag" in cols          # unknown column preserved
        assert CACHE_COLS <= cols             # all known columns present
        # Public API works against the forward-compat db.
        assert c.store(2000, "act", "/p", "forever_exe", True, 1000) is True
        assert c.lookup(2000, "act", "/p") is True

    def test_audit_extra_column_opens_and_round_trips(self, audit_db):
        con = sqlite3.connect(audit_db)
        con.execute("""
            CREATE TABLE audit (
                id INTEGER PRIMARY KEY,
                ts INTEGER NOT NULL,
                caller_uid INTEGER NOT NULL,
                caller_pid INTEGER NOT NULL,
                caller_exe TEXT NOT NULL,
                action TEXT NOT NULL,
                decision INTEGER NOT NULL,
                scope TEXT,
                source TEXT NOT NULL,
                approver_uid INTEGER,
                rule_path TEXT,
                request_id INTEGER,
                selinux_subj_type TEXT,
                argv TEXT,
                future_flag TEXT DEFAULT 'x'
            )
        """)
        con.commit()
        con.close()

        a = AuditLog(audit_db)  # must not raise
        cols = set(_cols(audit_db, "audit"))
        assert "future_flag" in cols
        assert AUDIT_COLS <= cols
        a.log(caller_uid=2000, caller_pid=1, caller_exe="/p", action="x",
              decision=True, scope="1h", source="prompt", approver_uid=1000)
        assert len(a.recent()) == 1
