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
        c.store(2000, "a", "/p1", "1h", True, 1000, argv=["/p1"])
        c.store(3000, "b", "/p2", "24h", True, 1000, argv=["/p2"])
        rows = c.list_all()
        assert len(rows) == 2
        # id DESC -> most recently-inserted first
        assert rows[0]["action"] == "b"
        assert rows[1]["action"] == "a"

    def test_row_fields_complete(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "1h", True, 1000, argv=["/p"])
        row = c.list_all()[0]
        for k in ("id", "caller_uid", "action", "match_kind",
                  "match_value", "argv", "decision", "scope",
                  "approver_uid", "expires_at", "created_at"):
            assert k in row


class TestRevoke:
    def test_delete_by_id_returns_row_and_removes(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/usr/bin/p", "1h", True, 1000,
                argv=["/usr/bin/p"])
        row = c.lookup_detail(2000, "act", "/usr/bin/p",
                              argv=["/usr/bin/p"])
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
        c.store(2000, "a", "/p1", "1h", True, 1000, argv=["/p1"])
        c.store(2000, "b", "/p2", "24h", True, 1000, argv=["/p2"])
        c.store(3000, "a", "/p1", "1h", True, 1000,
                argv=["/p1"])  # different uid
        rows = c.delete_by_uid(2000)
        assert len(rows) == 2
        assert {r["action"] for r in rows} == {"a", "b"}
        # uid 3000's row untouched
        assert c.lookup_detail(3000, "a", "/p1", argv=["/p1"]) is not None
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
        wrote = c.store(2000, "test.action", "/usr/bin/python3", "1h", True, 1000,
                        argv=["/usr/bin/python3"])
        assert wrote is True
        assert c.lookup(2000, "test.action", "/usr/bin/python3",
                        argv=["/usr/bin/python3"]) is True

    def test_store_once_writes_no_row(self, cache_db):
        c = ApprovalCache(cache_db)
        wrote = c.store(2000, "test.action", "/usr/bin/python3", "once", True, 1000)
        assert wrote is False
        assert c.lookup(2000, "test.action", "/usr/bin/python3") is None

    def test_argv_exact_does_not_match_other_exe(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "test.action", "/usr/bin/python3", "1h", True, 1000,
                argv=["/usr/bin/python3"])
        assert c.lookup(2000, "test.action", "/usr/bin/curl",
                        argv=["/usr/bin/python3"]) is None

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
        c.store(2000, "test.action", "/usr/bin/python3", "1h", True, 1000,
                argv=["/usr/bin/python3"])
        future = int(time.time()) + 7200  # 2h later
        monkeypatch.setattr("qdistro_admin_cache.time.time", lambda: future)
        assert c.lookup(2000, "test.action", "/usr/bin/python3",
                        argv=["/usr/bin/python3"]) is None

    def test_gc_deletes_only_expired(self, cache_db, monkeypatch):
        c = ApprovalCache(cache_db)
        c.store(2000, "a", "/p", "1h", True, 1000, argv=["/p"])
        c.store(2000, "b", "/p", "forever", True, 1000)
        c.store(2000, "c", "/p", "24h", True, 1000, argv=["/p"])
        future = int(time.time()) + 7200  # 2h later
        monkeypatch.setattr("qdistro_admin_cache.time.time", lambda: future)
        deleted = c.gc()
        assert deleted == 1  # only the 1h row
        # remaining ones still present
        assert c.lookup(2000, "b", "/p") is True
        assert c.lookup(2000, "c", "/p", argv=["/p"]) is True

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
        c.store(2000, "a", "/p", "1h", True, 1000, argv=["/p"])
        row = c.lookup_detail(2000, "a", "/p", argv=["/p"])
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
        # Missing argv must not match argv-aware rows.
        assert c.lookup(2000, "qdistro.sudo.exec", "/usr/bin/apt-get") is None
        assert c.lookup(2000, "qdistro.sudo.exec", "/usr/bin/apt-get",
                        argv=[]) is None

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
        assert c.lookup(2000, "qdistro.sudo.exec", "/usr/bin/python3") is None

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
        assert c.lookup(2000, "qdistro.sudo.exec", "/usr/bin/systemctl") is None

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

    def test_timed_scope_without_argv_falls_back_to_exe_only(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "1h", True, 1000)  # no argv
        row = c.lookup_detail(2000, "act", "/p")
        assert row is not None
        assert row["match_kind"] == "exe_only"
        assert row["scope"] == "1h"

    def test_forever_argv_without_argv_writes_no_row(self, cache_db):
        """Explicit argv-pinned scopes with argv=None fail closed."""
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "forever_argv", True, 1000)
        assert c.lookup_detail(2000, "act", "/p") is None

    def test_prefix_without_argv_writes_no_row(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "forever_prefix", True, 1000)
        assert c.lookup_detail(2000, "act", "/p") is None

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


# --- delegated-request argv-blind guard ----------------------------------

class TestDelegatedArgvBlindGuard:
    """A delegated request must NOT be auto-satisfied program-blind by a
    pre-existing argv-blind row (exe_only / always). This is the
    decision-time mirror of the broker's _DELEGATED_FORBIDDEN_SCOPES
    store-path guard: a delegated peer was never authenticated by the
    broker, so it can't inherit another process's blanket trust. The
    argv-pinned kinds (argv_exact / basename / prefix) remain valid for
    delegated requests because they fix argv."""

    def test_exe_only_row_skipped_for_delegated(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/usr/bin/apt-get", "forever_exe", True, 1000)
        argv = ["/usr/bin/apt-get", "install", "evil"]
        # Non-delegated: argv-blind exe_only satisfies.
        assert c.lookup(2000, "act", "/usr/bin/apt-get", argv=argv) is True
        # Delegated: same row must NOT satisfy → re-prompt.
        assert c.lookup(2000, "act", "/usr/bin/apt-get", argv=argv,
                        delegated=True) is None
        assert c.lookup_detail(2000, "act", "/usr/bin/apt-get", argv=argv,
                               delegated=True) is None

    def test_always_row_skipped_for_delegated(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "", "forever", True, 1000)
        # Non-delegated: 'always' satisfies any exe.
        assert c.lookup(2000, "act", "/usr/bin/python3") is True
        # Delegated: skipped.
        assert c.lookup(2000, "act", "/usr/bin/python3",
                        delegated=True) is None

    def test_argv_exact_row_still_matches_for_delegated(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/usr/bin/apt-get", "forever_argv", True, 1000,
                argv=["/usr/bin/apt-get", "update"])
        argv = ["/usr/bin/apt-get", "update"]
        assert c.lookup(2000, "act", "/usr/bin/apt-get", argv=argv,
                        delegated=True) is True

    def test_delegated_falls_back_past_blind_row_to_argv_row(self, cache_db):
        """With both an argv-blind allow and an argv-pinned deny present,
        a delegated request ignores the blind allow and lands on the
        argv-pinned row when its argv matches."""
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "forever", True, 1000)            # always allow
        c.store(2000, "act", "/p", "forever_argv", False, 1000,      # argv_exact deny
                argv=["/p", "x"])
        # Delegated, argv matches the pinned deny → deny (blind allow skipped).
        assert c.lookup(2000, "act", "/p", argv=["/p", "x"],
                        delegated=True) is False
        # Delegated, argv doesn't match the pinned row → blind allow is
        # skipped too, so no decision (re-prompt).
        assert c.lookup(2000, "act", "/p", argv=["/p", "y"],
                        delegated=True) is None
        # Non-delegated, non-matching argv → blind always allow applies.
        assert c.lookup(2000, "act", "/p", argv=["/p", "y"]) is True

    def test_default_is_non_delegated(self, cache_db):
        """delegated defaults to False so existing callers are unchanged."""
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "forever_exe", True, 1000)
        assert c.lookup(2000, "act", "/p") is True
        assert c.lookup_detail(2000, "act", "/p") is not None

    # --- argv-AWARE kinds must NOT be skipped for delegated ----------

    def test_prefix_row_still_matches_for_delegated(self, cache_db):
        """prefix is argv-aware (pins argv[0..n]) so a delegated request
        must still be satisfied by it — it is NOT in
        _DELEGATED_FORBIDDEN_KINDS."""
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/usr/bin/systemctl", "forever_prefix", True,
                1000, argv=["/usr/bin/systemctl", "restart"])
        argv = ["/usr/bin/systemctl", "restart", "nginx"]
        assert c.lookup(2000, "act", "/usr/bin/systemctl", argv=argv,
                        delegated=True) is True
        row = c.lookup_detail(2000, "act", "/usr/bin/systemctl", argv=argv,
                              delegated=True)
        assert row is not None and row["match_kind"] == "prefix"
        # A prefix DENY is likewise honoured for delegated (argv-pinned).
        c.store(2000, "act", "/usr/bin/systemctl", "forever_prefix", False,
                1000, argv=["/usr/bin/systemctl", "stop"])
        assert c.lookup(2000, "act", "/usr/bin/systemctl",
                        argv=["/usr/bin/systemctl", "stop", "nginx"],
                        delegated=True) is False

    def test_basename_row_still_matches_for_delegated(self, cache_db):
        """basename is argv-aware (pins basename(argv[0])) so it must
        still satisfy a delegated request, even across exe paths."""
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/usr/bin/python3", "forever_basename", True,
                1000, argv=["/usr/bin/python3", "-V"])
        # Same basename via a different path still matches when delegated.
        assert c.lookup(2000, "act", "/usr/local/bin/python3",
                        argv=["/usr/local/bin/python3", "-c", "print(1)"],
                        delegated=True) is True

    # --- blind DENY rows are also skipped for delegated -------------

    def test_exe_only_deny_row_skipped_for_delegated(self, cache_db):
        """The skip is symmetric across the verdict: a delegated request
        must not be auto-DENIED by a blind exe_only deny row either —
        the broker never authenticated the peer, so it re-prompts rather
        than inheriting another process's blanket deny. Non-delegated
        still gets the cached deny."""
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "forever_exe", False, 1000)
        argv = ["/p", "install", "x"]
        assert c.lookup(2000, "act", "/p", argv=argv) is False
        assert c.lookup(2000, "act", "/p", argv=argv,
                        delegated=True) is None
        assert c.lookup_detail(2000, "act", "/p", argv=argv,
                               delegated=True) is None

    def test_always_deny_row_skipped_for_delegated(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "", "forever", False, 1000)
        assert c.lookup(2000, "act", "/anything") is False
        assert c.lookup(2000, "act", "/anything", delegated=True) is None

    # --- priority-ordering interaction with the skip ---------------

    def test_delegated_skips_blind_allow_falls_to_argv_deny(self, cache_db):
        """A blind 'always' allow (priority 4) sits below an argv_exact
        deny (priority 0). For a delegated request the blind allow is
        removed from consideration entirely; the argv_exact deny still
        applies when its argv matches. (Mirrors the existing
        test_delegated_falls_back_past_blind_row_to_argv_row but for the
        full _PRIORITY span / lookup_detail row identity.)"""
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "forever", True, 1000)           # always allow
        c.store(2000, "act", "/p", "forever_argv", False, 1000,     # argv_exact deny
                argv=["/p", "rm"])
        row = c.lookup_detail(2000, "act", "/p", argv=["/p", "rm"],
                              delegated=True)
        assert row is not None
        assert row["match_kind"] == "argv_exact"
        assert row["decision"] == 0

    def test_only_matching_row_is_blind_delegated_reprompts(self, cache_db):
        """Multiple competing rows, but the only one whose argv/exe match
        the request is blind (exe_only). Non-delegated gets the allow;
        delegated gets no hit (re-prompt)."""
        c = ApprovalCache(cache_db)
        # An argv_exact deny that does NOT match the request's argv.
        c.store(2000, "act", "/p", "forever_argv", False, 1000,
                argv=["/p", "other"])
        # A blind exe_only allow that DOES match (argv-blind).
        c.store(2000, "act", "/p", "forever_exe", True, 1000)
        argv = ["/p", "actual"]
        # Non-delegated: argv_exact doesn't match argv, exe_only does → allow.
        assert c.lookup(2000, "act", "/p", argv=argv) is True
        # Delegated: exe_only removed; argv_exact doesn't match → re-prompt.
        assert c.lookup(2000, "act", "/p", argv=argv,
                        delegated=True) is None

    def test_mixed_blind_kinds_both_skipped_for_delegated(self, cache_db):
        """When BOTH an exe_only and an always row match, a delegated
        request skips both and re-prompts; non-delegated takes the
        higher-priority exe_only."""
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "", "forever", True, 1000)          # always allow
        c.store(2000, "act", "/p", "forever_exe", False, 1000)   # exe_only deny
        # Non-delegated: exe_only (priority 3) beats always (4) → deny.
        assert c.lookup(2000, "act", "/p") is False
        # Delegated: both blind → no decision.
        assert c.lookup(2000, "act", "/p", delegated=True) is None

    # --- expiry interaction ----------------------------------------

    def test_expired_blind_row_with_delegated(self, cache_db, monkeypatch):
        """An expired blind row is already filtered by the expiry clause;
        the delegated flag is orthogonal. Assert the combination: expired
        + delegated → None, and that an unexpired blind row would also be
        None purely from the delegated skip (belt-and-suspenders)."""
        c = ApprovalCache(cache_db)
        # 1h (timed) with no argv → stored as a timed exe_only (blind) row.
        c.store(2000, "act", "/p", "1h", True, 1000)
        # Before expiry: non-delegated allow, delegated skipped.
        assert c.lookup(2000, "act", "/p") is True
        assert c.lookup(2000, "act", "/p", delegated=True) is None
        # After expiry: both None (expiry filter for non-delegated;
        # expiry AND skip for delegated).
        future = int(time.time()) + 7200
        monkeypatch.setattr("qdistro_admin_cache.time.time", lambda: future)
        assert c.lookup(2000, "act", "/p") is None
        assert c.lookup(2000, "act", "/p", delegated=True) is None


# --- sandboxed-caller argv-blind guard -----------------------------------

class TestSandboxedArgvBlindGuard:
    """An authenticated sandboxed (tier-2) caller must NOT inherit a
    uid-wide argv-blind ``forever`` (always) / ``forever_exe`` (exe_only)
    grant minted for a different exe/argv/tier at the same uid (issue
    broker-forever-cache-scope). This mirrors the delegated guard but
    keys off the broker-supplied ``sandboxed`` flag on the
    *non-delegated* RequestPermission / CheckPermission path. The
    argv-pinned kinds (argv_exact / basename / prefix) stay valid."""

    def test_always_row_skipped_for_sandboxed(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "", "forever", True, 1000)
        # Non-sandboxed: 'always' satisfies any exe.
        assert c.lookup(2000, "act", "/usr/bin/python3") is True
        # Sandboxed: skipped → re-prompt.
        assert c.lookup(2000, "act", "/usr/bin/python3",
                        sandboxed=True) is None
        assert c.lookup_detail(2000, "act", "/usr/bin/python3",
                               sandboxed=True) is None

    def test_exe_only_row_skipped_for_sandboxed(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/usr/bin/apt-get", "forever_exe", True, 1000)
        argv = ["/usr/bin/apt-get", "install", "evil"]
        # Non-sandboxed: argv-blind exe_only satisfies.
        assert c.lookup(2000, "act", "/usr/bin/apt-get", argv=argv) is True
        # Sandboxed: must NOT satisfy.
        assert c.lookup(2000, "act", "/usr/bin/apt-get", argv=argv,
                        sandboxed=True) is None

    def test_always_deny_row_skipped_for_sandboxed(self, cache_db):
        """Symmetric across verdict: a sandboxed caller does not inherit a
        blind DENY either — it re-prompts rather than carrying another
        process's blanket deny."""
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "", "forever", False, 1000)
        assert c.lookup(2000, "act", "/anything") is False
        assert c.lookup(2000, "act", "/anything", sandboxed=True) is None

    def test_argv_exact_row_still_matches_for_sandboxed(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/usr/bin/apt-get", "forever_argv", True, 1000,
                argv=["/usr/bin/apt-get", "update"])
        argv = ["/usr/bin/apt-get", "update"]
        assert c.lookup(2000, "act", "/usr/bin/apt-get", argv=argv,
                        sandboxed=True) is True

    def test_prefix_and_basename_still_match_for_sandboxed(self, cache_db):
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/usr/bin/systemctl", "forever_prefix", True,
                1000, argv=["/usr/bin/systemctl", "restart"])
        c.store(2000, "actb", "/usr/bin/python3", "forever_basename", True,
                1000, argv=["/usr/bin/python3", "-V"])
        assert c.lookup(2000, "act", "/usr/bin/systemctl",
                        argv=["/usr/bin/systemctl", "restart", "nginx"],
                        sandboxed=True) is True
        assert c.lookup(2000, "actb", "/usr/local/bin/python3",
                        argv=["/usr/local/bin/python3", "-c", "print(1)"],
                        sandboxed=True) is True

    def test_sandboxed_skips_blind_allow_falls_to_argv_deny(self, cache_db):
        """A blind 'always' allow is skipped for a sandboxed caller; an
        argv-pinned deny still applies when its argv matches."""
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "forever", True, 1000)            # always allow
        c.store(2000, "act", "/p", "forever_argv", False, 1000,      # argv_exact deny
                argv=["/p", "rm"])
        row = c.lookup_detail(2000, "act", "/p", argv=["/p", "rm"],
                              sandboxed=True)
        assert row is not None
        assert row["match_kind"] == "argv_exact"
        assert row["decision"] == 0
        # argv doesn't match the pinned row → blind allow skipped → re-prompt.
        assert c.lookup(2000, "act", "/p", argv=["/p", "ls"],
                        sandboxed=True) is None
        # Non-sandboxed, non-matching argv → blind always allow applies.
        assert c.lookup(2000, "act", "/p", argv=["/p", "ls"]) is True

    def test_default_is_non_sandboxed(self, cache_db):
        """sandboxed defaults to False so existing callers are unchanged."""
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "forever_exe", True, 1000)
        assert c.lookup(2000, "act", "/p") is True
        assert c.lookup_detail(2000, "act", "/p") is not None

    def test_delegated_or_sandboxed_both_skip(self, cache_db):
        """delegated and sandboxed compose: either one skips blind rows."""
        c = ApprovalCache(cache_db)
        c.store(2000, "act", "/p", "forever_exe", True, 1000)
        assert c.lookup(2000, "act", "/p", delegated=True,
                        sandboxed=False) is None
        assert c.lookup(2000, "act", "/p", delegated=False,
                        sandboxed=True) is None
        assert c.lookup(2000, "act", "/p", delegated=True,
                        sandboxed=True) is None
