"""Tests for qdistro_admin_cache.ApprovalCache."""
from __future__ import annotations

import os
import time

import pytest

from qdistro_admin_cache import ApprovalCache, scope_to_row


# --- scope_to_row ---------------------------------------------------------

class TestScopeToRow:
    def test_once_returns_none(self):
        assert scope_to_row("once") is None

    def test_unknown_scope_returns_none(self):
        assert scope_to_row("nonsense") is None

    @pytest.mark.parametrize("scope,kind,ttl", [
        ("1h",               "argv_exact", 3600),
        ("24h",              "argv_exact", 86_400),
        ("forever",          "always",     None),
        ("forever_exe",      "exe_only",   None),
        ("forever_argv",     "argv_exact", None),
        ("forever_basename", "basename",   None),
        ("forever_prefix",   "prefix",     None),
    ])
    def test_known_scopes(self, scope, kind, ttl):
        assert scope_to_row(scope) == (kind, ttl)


# --- ApprovalCache --------------------------------------------------------

class TestListAll:
    def test_empty_cache(self, cache_db):
        c = ApprovalCache(cache_db)
        assert c.list_all() == []

    def test_returns_rows_newest_first(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "a", "/p1", "1h", True, 1000)
        c.store(3000, "b", "/p2", "24h", True, 1000)
        rows = c.list_all()
        assert len(rows) == 2
        # id DESC -> most recently-inserted first
        assert rows[0]["action"] == "b"
        assert rows[1]["action"] == "a"

    def test_row_fields_complete(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "1h", True, 1000)
        row = c.list_all()[0]
        for k in ("id", "caller_uid", "action", "match_kind",
                  "match_value", "argv", "decision", "scope",
                  "approver_uid", "expires_at", "created_at"):
            assert k in row


class TestRevoke:
    def test_delete_by_id_returns_row_and_removes(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/usr/bin/p", "1h", True, 1000)
        row = c.lookup_detail(2000, "act", "/usr/bin/p")
        assert row is not None
        deleted = c.delete_by_id(row["id"])
        assert deleted is not None
        assert deleted["caller_uid"] == 2000
        assert deleted["action"] == "act"
        assert c.lookup_detail(2000, "act", "/usr/bin/p") is None

    def test_delete_by_id_absent_returns_none(self, cache_db):
        c = ApprovalCache(cache_db)
        assert c.delete_by_id(99999) is None

    def test_delete_by_uid_removes_all_and_returns_rows(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "a", "/p1", "1h", True, 1000)
        c.store(2000, "b", "/p2", "24h", True, 1000)
        c.store(3000, "a", "/p1", "1h", True, 1000)  # different uid
        rows = c.delete_by_uid(2000)
        assert len(rows) == 2
        assert {r["action"] for r in rows} == {"a", "b"}
        # uid 3000's row untouched
        assert c.lookup_detail(3000, "a", "/p1") is not None
        assert c.lookup_detail(2000, "a", "/p1") is None

    def test_delete_by_uid_empty_returns_empty_list(self, cache_db):
        c = ApprovalCache(cache_db)
        assert c.delete_by_uid(9999) == []


class TestApprovalCache:
    def test_lookup_empty_returns_none(self, cache_db):
        c = ApprovalCache(cache_db)
        assert c.lookup(2000, "x", "/usr/bin/p") is None
        assert c.lookup_detail(2000, "x", "/usr/bin/p") is None

    def test_db_file_mode_0600(self, cache_db):
        ApprovalCache(cache_db)
        mode = os.stat(cache_db).st_mode & 0o777
        assert mode == 0o600

    def test_db_dir_created(self, tmp_path):
        nested = tmp_path / "nested" / "deeper" / "approvals.sqlite"
        ApprovalCache(str(nested))
        assert nested.exists()

    def test_store_then_lookup_roundtrip_argv_exact(self, cache_db):
        c = ApprovalCache(cache_db)
        wrote = c.store(2000, "test.action", "/usr/bin/python3", "1h", True, 1000)
        assert wrote is True
        assert c.lookup(2000, "test.action", "/usr/bin/python3") is True

    def test_store_once_writes_no_row(self, cache_db):
        c = ApprovalCache(cache_db)
        wrote = c.store(2000, "test.action", "/usr/bin/python3", "once", True, 1000)
        assert wrote is False
        assert c.lookup(2000, "test.action", "/usr/bin/python3") is None

    def test_argv_exact_does_not_match_other_exe(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "test.action", "/usr/bin/python3", "1h", True, 1000)
        assert c.lookup(2000, "test.action", "/usr/bin/curl") is None

    def test_always_matches_any_exe(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "test.action", "", "forever", True, 1000)
        assert c.lookup(2000, "test.action", "/usr/bin/python3") is True
        assert c.lookup(2000, "test.action", "/usr/bin/curl") is True

    def test_always_does_not_cross_uids(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "test.action", "", "forever", True, 1000)
        assert c.lookup(2001, "test.action", "/x") is None

    def test_always_does_not_cross_actions(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "test.action", "", "forever", True, 1000)
        assert c.lookup(2000, "other.action", "/x") is None

    def test_specific_takes_precedence_over_always(self, cache_db):
        c = ApprovalCache(cache_db)
        # 'always' allow first
        c.store(2000, "test.action", "", "forever", True, 1000)
        # then a more-specific deny on a particular exe (forever_exe →
        # exe_only kind, beats always in lookup priority)
        c.store(2000, "test.action", "/usr/bin/python3", "forever_exe",
                False, 1000)
        # specific match wins
        assert c.lookup(2000, "test.action", "/usr/bin/python3") is False
        # other exe still allowed via 'always'
        assert c.lookup(2000, "test.action", "/usr/bin/curl") is True

    def test_expired_row_not_returned(self, cache_db, monkeypatch):
        c = ApprovalCache(cache_db)
        # Use real wall clock to write, then advance via monkeypatch
        c.store(2000, "test.action", "/usr/bin/python3", "1h", True, 1000)
        future = int(time.time()) + 7200  # 2h later
        monkeypatch.setattr("qdistro_admin_cache.time.time", lambda: future)
        assert c.lookup(2000, "test.action", "/usr/bin/python3") is None

    def test_gc_deletes_only_expired(self, cache_db, monkeypatch):
        c = ApprovalCache(cache_db)
        c.store(2000, "a", "/p", "1h", True, 1000)
        c.store(2000, "b", "/p", "forever", True, 1000)
        c.store(2000, "c", "/p", "24h", True, 1000)
        future = int(time.time()) + 7200  # 2h later
        monkeypatch.setattr("qdistro_admin_cache.time.time", lambda: future)
        deleted = c.gc()
        assert deleted == 1  # only the 1h row
        # remaining ones still present
        assert c.lookup(2000, "b", "/p") is True
        assert c.lookup(2000, "c", "/p") is True

    def test_gc_with_no_expired_returns_zero(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "a", "/p", "forever", True, 1000)
        assert c.gc() == 0

    def test_lookup_detail_returns_full_row_keys(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "test.action", "/usr/bin/python3", "1h", True, 1000,
                argv=["/usr/bin/python3", "-c", "print(1)"])
        row = c.lookup_detail(2000, "test.action", "/usr/bin/python3",
                              argv=["/usr/bin/python3", "-c", "print(1)"])
        assert row is not None
        assert set(row.keys()) == {
            "id", "match_kind", "match_value", "argv", "decision",
            "expires_at", "created_at", "approver_uid", "scope",
        }
        assert row["match_kind"] == "argv_exact"
        assert row["match_value"] == "/usr/bin/python3"
        assert row["decision"] == 1
        assert row["approver_uid"] == 1000
        assert row["scope"] == "1h"

    def test_forever_exe_is_exe_only_no_expiry(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "a", "/p", "forever_exe", True, 1000)
        row = c.lookup_detail(2000, "a", "/p")
        assert row["match_kind"] == "exe_only"
        assert row["expires_at"] is None

    def test_forever_is_always_no_expiry(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "a", "", "forever", True, 1000)
        row = c.lookup_detail(2000, "a", "/anything")
        assert row["match_kind"] == "always"
        assert row["expires_at"] is None

    def test_most_recent_id_wins_when_match_kind_ties(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "a", "/p", "forever_exe", True, 1000)   # id=1 exe_only
        c.store(2000, "a", "/p", "forever_exe", False, 1000)  # id=2 exe_only deny
        # both match exe_only; tie-broken by id DESC (newest wins)
        assert c.lookup(2000, "a", "/p") is False

    def test_lookup_detail_returns_original_scope(self, cache_db):
        """Cache stores the UI scope verbatim — no ambiguous TTL heuristic."""
        c = ApprovalCache(cache_db)
        c.store(2000, "a", "/p", "1h", True, 1000)
        row = c.lookup_detail(2000, "a", "/p")
        assert row["scope"] == "1h"

    def test_scope_persisted_for_each_kind(self, cache_db):
        c = ApprovalCache(cache_db)
        for scope in ("1h", "24h", "forever", "forever_exe"):
            exe = "" if scope == "forever" else f"/bin/{scope}"
            c.store(2000, scope, exe, scope, True, 1000,
                    argv=[exe] if exe else None)
            row = c.lookup_detail(2000, scope, exe or "/anything",
                                  argv=[exe] if exe else None)
            assert row["scope"] == scope, f"scope round-trip broken for {scope}"

    def test_wal_enabled(self, cache_db):
        ApprovalCache(cache_db)
        # Open a fresh connection to ask sqlite about journal mode
        import sqlite3
        mode = sqlite3.connect(cache_db).execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


# --- task(069) argv-aware approvals --------------------------------------

class TestArgvAware:
    """Spec/21 + task(069): match_kind ∈ argv_exact|prefix|basename|exe_only|always.
    The pre-69 'argv_exact' was misnamed and only matched exe; that
    semantic is preserved as the new 'exe_only' kind. Argv-tracked
    kinds tighten the qsu cache so a forever-allow for one argv tuple
    doesn't leak into siblings under the same exe."""

    def test_argv_exact_matches_exact_argv(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "qdistro.sudo.exec", "/usr/bin/apt-get",
                "forever_argv", True, 1000,
                argv=["/usr/bin/apt-get", "update"])
        assert c.lookup(2000, "qdistro.sudo.exec", "/usr/bin/apt-get",
                        argv=["/usr/bin/apt-get", "update"]) is True
        # Different argv under same exe — must miss.
        assert c.lookup(2000, "qdistro.sudo.exec", "/usr/bin/apt-get",
                        argv=["/usr/bin/apt-get", "install", "evil"]) is None

    def test_exe_only_matches_any_argv_under_same_exe(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "qdistro.sudo.exec", "/usr/bin/apt-get",
                "forever_exe", True, 1000)
        for argv in (["/usr/bin/apt-get", "update"],
                     ["/usr/bin/apt-get", "install", "evil"],
                     None):
            assert c.lookup(2000, "qdistro.sudo.exec", "/usr/bin/apt-get",
                            argv=argv) is True
        # Different exe — must miss even with same basename intent.
        assert c.lookup(2000, "qdistro.sudo.exec", "/usr/local/bin/apt-get",
                        argv=["/usr/local/bin/apt-get", "update"]) is None

    def test_basename_matches_any_path_with_same_basename(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "qdistro.sudo.exec", "/usr/bin/python3",
                "forever_basename", True, 1000,
                argv=["/usr/bin/python3", "-V"])
        # Any /usr/bin/python3 invocation hits.
        assert c.lookup(2000, "qdistro.sudo.exec", "/usr/bin/python3",
                        argv=["/usr/bin/python3", "-V"]) is True
        # Different exe path but same basename — also matches (basename's
        # whole point: not pinned to install location).
        assert c.lookup(2000, "qdistro.sudo.exec", "/usr/local/bin/python3",
                        argv=["/usr/local/bin/python3", "-c", "print(1)"]) is True
        # Different basename — miss.
        assert c.lookup(2000, "qdistro.sudo.exec", "/usr/bin/python3.13",
                        argv=["/usr/bin/python3.13"]) is None

    def test_prefix_matches_argv_prefix(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "qdistro.sudo.exec", "/usr/bin/systemctl",
                "forever_prefix", True, 1000,
                argv=["/usr/bin/systemctl", "restart"])
        # Exact prefix + extra args.
        assert c.lookup(2000, "qdistro.sudo.exec", "/usr/bin/systemctl",
                        argv=["/usr/bin/systemctl", "restart", "nginx"]) is True
        assert c.lookup(2000, "qdistro.sudo.exec", "/usr/bin/systemctl",
                        argv=["/usr/bin/systemctl", "restart", "qdistro-admin-broker"]) is True
        # Different verb — miss.
        assert c.lookup(2000, "qdistro.sudo.exec", "/usr/bin/systemctl",
                        argv=["/usr/bin/systemctl", "stop", "nginx"]) is None
        # Empty argv — miss (no program even).
        assert c.lookup(2000, "qdistro.sudo.exec", "/usr/bin/systemctl",
                        argv=[]) is None

    def test_argv_exact_beats_exe_only_beats_always(self, cache_db):
        """Most-specific match wins, even when stored newest-first.
        Order: argv_exact > prefix > basename > exe_only > always."""
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "forever", True, 1000)            # always
        c.store(2000, "act", "/p", "forever_exe", False, 1000)       # exe_only deny
        c.store(2000, "act", "/p", "forever_argv", True, 1000,       # argv_exact allow
                argv=["/p", "x"])
        # All three rows present. argv_exact for "[/p,x]" beats exe_only
        # which beats always.
        row = c.lookup_detail(2000, "act", "/p", argv=["/p", "x"])
        assert row["match_kind"] == "argv_exact"
        assert row["decision"] == 1
        # Different argv → fallback to exe_only deny.
        row2 = c.lookup_detail(2000, "act", "/p", argv=["/p", "other"])
        assert row2["match_kind"] == "exe_only"
        assert row2["decision"] == 0
        # Different exe → fallback to always allow.
        row3 = c.lookup_detail(2000, "act", "/other-exe",
                               argv=["/other-exe"])
        assert row3["match_kind"] == "always"
        assert row3["decision"] == 1

    def test_argv_exact_without_argv_falls_back_to_exe_only(self, cache_db):
        """store() with argv_exact-style scope but argv=None degrades
        gracefully: writes an exe_only row instead so we never silently
        store an unmatchable argv_exact row."""
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "1h", True, 1000)  # no argv
        row = c.lookup_detail(2000, "act", "/p")
        assert row is not None
        assert row["match_kind"] == "exe_only"

    def test_prefix_without_argv_falls_back_to_exe_only(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "forever_prefix", True, 1000)
        row = c.lookup_detail(2000, "act", "/p")
        assert row is not None
        assert row["match_kind"] == "exe_only"

    def test_legacy_argv_exact_rows_migrate_to_exe_only(self, tmp_path):
        """In-place migration: pre-69 dbs had match_kind='argv_exact' with
        argv='' meaning "match-by-exe". The migration renames those
        to 'exe_only' so the new argv_exact kind is reserved for true
        argv-tuple matching."""
        import sqlite3
        db = str(tmp_path / "legacy.sqlite")
        # Build a pre-69 schema by hand.
        con = sqlite3.connect(db)
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
                approver_uid INTEGER NOT NULL,
                scope TEXT
            )
        """)
        con.execute(
            "INSERT INTO approvals (caller_uid, action, match_kind, "
            "match_value, decision, expires_at, created_at, approver_uid, "
            "scope) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (2000, "act", "argv_exact", "/usr/bin/apt-get", 1, None,
             0, 1000, "forever_exe"),
        )
        con.commit()
        con.close()
        # Open via ApprovalCache → migration runs.
        c = ApprovalCache(db)
        rows = c.list_all()
        assert len(rows) == 1
        assert rows[0]["match_kind"] == "exe_only"
        assert rows[0]["argv"] == ""
        # And the row still matches what forever_exe semantics expects.
        assert c.lookup(2000, "act", "/usr/bin/apt-get",
                        argv=["/usr/bin/apt-get", "anything"]) is True
