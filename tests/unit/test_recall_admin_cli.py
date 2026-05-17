"""Tests for the qdistro-recall-admin CLI.

The CLI sits on top of qdistro_recall_admin + qdistro_recall_ingest;
the modules themselves have their own coverage. These tests pin the
argparse dispatch, the human-readable output shape, the JSON output
shape, the exit codes, and the recall-DB walking integration.

The CLI requires root in production (no env-var escape hatch).
Tests bypass via a monkey-patch of :func:`CLI._require_root` so a
misconfigured environment can't silently disable the auth check.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

import qdistro_recall_admin_cli as CLI
import qdistro_recall_admin as RA
import qdistro_browser_bridge_client as _client


# Capture the real _require_root at import time so the TestRootCheck
# cases can restore it even after the autouse fixture replaced it.
_REAL_REQUIRE_ROOT = CLI._require_root


# Bypass the root check via a direct monkey-patch — no env var.
@pytest.fixture(autouse=True)
def _no_root_check(monkeypatch):
    monkeypatch.setattr(CLI, "_require_root", lambda: None)
    _client.set_dbus_client(None)
    yield
    _client.set_dbus_client(None)


class _FakeDBus(_client._BaseDBusClient):
    def __init__(self, *, replies: dict[tuple, str] | None = None):
        self.calls: list[dict] = []
        self._replies = replies or {}

    def list_names(self, bus):  # noqa: ARG002
        return []

    def call(self, bus, service, object_path, interface, method,
             signature, body):
        self.calls.append({
            "bus": bus, "service": service, "method": method,
            "body": body,
        })
        key = (bus, service, method)
        return self._replies.get(key, json.dumps({"ok": True}))


def _set_fake_relay(replies):
    _client.set_dbus_client(_FakeDBus(replies=replies))


# ---- containers -----------------------------------------------------

class TestContainersSubcommand:
    def test_text_output(self, capsys):
        _set_fake_relay({
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True,
                "containers": [
                    {"cookie_store_id": "firefox-container-1",
                     "name": "Personal", "color": "blue",
                     "icon": "fingerprint"},
                    {"cookie_store_id": "firefox-container-2",
                     "name": "Work", "color": "red",
                     "icon": "briefcase"},
                ],
            }),
        })
        rc = CLI.main(["containers", "--uid", "2000"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "firefox-container-1" in out
        assert "Personal" in out
        assert "[blue/fingerprint]" in out
        assert "Work" in out

    def test_json_output(self, capsys):
        _set_fake_relay({
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True, "containers": [],
            }),
        })
        rc = CLI.main(["containers", "--uid", "2000", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["ok"] is True
        assert parsed["containers"] == []

    def test_relay_failure_nonzero_exit(self, capsys):
        _set_fake_relay({
            ("SYSTEM", "org.qdistro.UserRelay.uid9999",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": False, "error": "no_bridge_found",
            }),
        })
        rc = CLI.main(["containers", "--uid", "9999"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no_bridge_found" in err

    def test_empty_list_prints_placeholder(self, capsys):
        _set_fake_relay({
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True, "containers": [],
            }),
        })
        CLI.main(["containers", "--uid", "2000"])
        assert "(no containers)" in capsys.readouterr().out


# ---- tabs -----------------------------------------------------------

class TestTabsSubcommand:
    def test_text_output_marks_active(self, capsys):
        _set_fake_relay({
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True,
                "tabs": [
                    {"id": 7, "url": "https://a/", "title": "A",
                     "active": True, "window_id": 1},
                    {"id": 8, "url": "https://b/", "title": "B",
                     "active": False, "window_id": 1},
                ],
            }),
        })
        CLI.main(["tabs", "--uid", "2000"])
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        assert lines[0].startswith("* ")  # active
        assert lines[1].startswith("  ")  # not active
        assert "https://a/" in lines[0]
        assert "https://b/" in lines[1]

    def test_empty_tabs_placeholder(self, capsys):
        _set_fake_relay({
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True, "tabs": [],
            }),
        })
        CLI.main(["tabs", "--uid", "2000"])
        assert "(no tabs)" in capsys.readouterr().out

    def test_calls_tabs_list_op(self):
        fake = _FakeDBus(replies={
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps(
                {"ok": True, "tabs": []}),
        })
        _client.set_dbus_client(fake)
        CLI.main(["tabs", "--uid", "2000"])
        op = fake.calls[0]["body"][0]
        assert op == "tabs.list"


# ---- search ---------------------------------------------------------

@pytest.fixture
def populated_recall_db(tmp_path):
    """Build a recall DB layout with one entry whose URL matches a
    live tab and one whose URL does not. Returns (root, query)."""
    import qdistro_recall_ingest as eng
    root = str(tmp_path / "recall")
    user = "work-user"
    db_path = eng.db_path_for(root, user)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = eng.open_db(db_path)
    try:
        eng.push_text(conn, user=user, text="foo bar baz",
                      source="bridge",
                      url="https://a.example/article",
                      title="Article A")
        eng.push_text(conn, user=user, text="foo extra context",
                      source="bridge",
                      url="https://b.example/other",
                      title="Other B")
    finally:
        conn.close()
    return root


class TestSearchSubcommand:
    def test_search_with_live_join(self, populated_recall_db, capsys):
        _set_fake_relay({
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True,
                "tabs": [
                    {"id": 7, "url": "https://a.example/article",
                     "title": "Article A", "active": True,
                     "window_id": 1},
                ],
            }),
        })
        rc = CLI.main([
            "--root", populated_recall_db,
            "search", "foo", "--uid", "2000",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        # The matching row carries the [live: ...] marker; the
        # non-matching row does not.
        lines = [l for l in out.splitlines() if "foo" in l or "[live" in l]
        joined = "\n".join(lines)
        assert "[live: w1 t7]" in joined
        # b.example is non-matching → no live marker on its line.
        b_line = next(l for l in out.splitlines()
                      if "https://b.example" in l)
        assert "[live" not in b_line

    def test_search_text_includes_url(self, populated_recall_db, capsys):
        """U3 fix: search text output prints the URL — that's what an
        admin grepping for 'is this URL open?' actually wants."""
        _set_fake_relay({
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True, "tabs": [],
            }),
        })
        CLI.main([
            "--root", populated_recall_db,
            "search", "foo", "--uid", "2000",
        ])
        out = capsys.readouterr().out
        assert "https://a.example/article" in out
        assert "https://b.example/other" in out

    def test_search_json_output_structured_live_status(
            self, populated_recall_db, capsys):
        """C1 fix: search --json includes a top-level `live` object
        with ok/error/detail so consumers can programmatically detect
        relay failures."""
        _set_fake_relay({
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True, "tabs": [
                    {"id": 7, "url": "https://a.example/article",
                     "title": "Article A", "window_id": 1},
                ],
            }),
        })
        rc = CLI.main([
            "--root", populated_recall_db,
            "search", "foo", "--uid", "2000", "--json",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["live"] == {"ok": True, "error": None,
                                   "detail": None}
        rows = payload["rows"]
        # Two rows; one annotated, one not.
        live = [r for r in rows if r.get("live_tab")]
        not_live = [r for r in rows if not r.get("live_tab")]
        assert len(live) == 1
        assert len(not_live) == 1
        assert live[0]["live_tab"]["id"] == 7

    def test_search_relay_failure_returns_rc_2_with_structured_live(
            self, populated_recall_db, capsys):
        """C2: relay failure is now an rc=2 — distinguishable from
        rc=0 (full success). Recall results still print. JSON shape
        carries live.ok=false + error code."""
        _set_fake_relay({
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": False, "error": "relay_call_failed",
                "detail": "no relay for uid 2000",
            }),
        })
        rc = CLI.main([
            "--root", populated_recall_db,
            "search", "foo", "--uid", "2000",
        ])
        assert rc == 2
        captured = capsys.readouterr()
        assert "[live:" not in captured.out
        assert "live-tab annotation unavailable" in captured.err

    def test_search_relay_failure_json_carries_error(
            self, populated_recall_db, capsys):
        _set_fake_relay({
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": False, "error": "no_bridge_found",
            }),
        })
        rc = CLI.main([
            "--root", populated_recall_db,
            "search", "foo", "--uid", "2000", "--json",
        ])
        assert rc == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True  # search itself succeeded
        assert payload["live"]["ok"] is False
        assert payload["live"]["error"] == "no_bridge_found"

    def test_search_no_dbs_yields_empty(self, tmp_path, capsys):
        """A root with no DBs is a clean no-op, not an error."""
        empty_root = str(tmp_path / "empty")
        os.makedirs(empty_root)
        rc = CLI.main([
            "--root", empty_root,
            "search", "foo", "--uid", "2000",
        ])
        assert rc == 0
        assert "(no recall DBs)" in capsys.readouterr().err

    def test_search_unknown_silo_returns_rc_2(self, tmp_path, capsys):
        """C3 fix: --silo that matches zero silos is rc=2 so an
        operator typo is distinguishable from 'no recall yet'."""
        empty_root = str(tmp_path / "recall")
        os.makedirs(empty_root)
        rc = CLI.main([
            "--root", empty_root,
            "search", "foo", "--uid", "2000",
            "--silo", "nonexistent-silo",
        ])
        assert rc == 2
        assert "nonexistent-silo" in capsys.readouterr().err

    def test_search_silo_with_traversal_rejected(
            self, populated_recall_db, capsys):
        """C4 fix: --silo with '/' or '..' is refused. Root-only
        blast radius but still worth a guard."""
        for bad in ("../../etc", "..", "foo/bar", ".hidden"):
            rc = CLI.main([
                "--root", populated_recall_db,
                "search", "foo", "--uid", "2000", "--silo", bad,
            ])
            assert rc == 1, f"silo {bad!r} should have been rejected"

    def test_search_malformed_fts_query_rc_2_single_error(
            self, populated_recall_db, capsys, monkeypatch):
        """S3 fix: a malformed FTS query produces ONE error line, not
        N warn lines per DB, and exits rc=2."""
        # Force the engine.search to raise an FTS-shaped error.
        import qdistro_recall_ingest as eng

        def _broken_search(conn, q, **_kw):
            raise eng.sqlite3.OperationalError(
                "fts5: syntax error near 'OR'")
        monkeypatch.setattr(eng, "search", _broken_search)

        _set_fake_relay({
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps(
                 {"ok": True, "tabs": []}),
        })
        rc = CLI.main([
            "--root", populated_recall_db,
            "search", "foo OR", "--uid", "2000",
        ])
        err = capsys.readouterr().err
        assert rc == 2
        # One "error: malformed FTS query" line, not multiple warn
        # lines per DB.
        assert err.count("malformed FTS query") == 1
        assert "warn:" not in err


# ---- argparse-level -------------------------------------------------

class TestArgparse:
    def test_unknown_subcommand_exits_nonzero(self, capsys):
        with pytest.raises(SystemExit) as ei:
            CLI.main(["notacommand"])
        assert ei.value.code != 0

    def test_missing_uid_on_containers(self, capsys):
        with pytest.raises(SystemExit) as ei:
            CLI.main(["containers"])
        assert ei.value.code != 0

    def test_missing_uid_on_search(self, capsys):
        with pytest.raises(SystemExit) as ei:
            CLI.main(["search", "foo"])
        assert ei.value.code != 0


# ---- root check -----------------------------------------------------

class TestRootCheck:
    def test_geteuid_nonzero_exits_with_message(
            self, monkeypatch, capsys):
        """Without root the CLI fails closed. No env-var escape hatch
        — tests must monkey-patch _require_root directly (and the
        autouse fixture does so for the rest of this file)."""
        # Undo the autouse monkey-patch so the real _require_root runs.
        monkeypatch.setattr(CLI, "_require_root", _REAL_REQUIRE_ROOT)
        monkeypatch.setattr(CLI.os, "geteuid", lambda: 1000)
        with pytest.raises(SystemExit) as ei:
            CLI.main(["containers", "--uid", "2000"])
        assert ei.value.code != 0
        assert "must be run as root" in capsys.readouterr().err

    def test_geteuid_zero_proceeds(self, monkeypatch):
        """The geteuid==0 branch is the production path; make sure
        it actually reaches the subcommand handler. (Most tests
        bypass _require_root entirely, so this branch wouldn't
        otherwise be exercised.)"""
        # Run the *real* root check, but pretend we're root.
        monkeypatch.setattr(CLI, "_require_root", _REAL_REQUIRE_ROOT)
        monkeypatch.setattr(CLI.os, "geteuid", lambda: 0)
        _set_fake_relay({
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True, "containers": [],
            }),
        })
        rc = CLI.main(["containers", "--uid", "2000"])
        assert rc == 0

    def test_no_env_var_escape_hatch(self, monkeypatch, capsys):
        """Setting QDISTRO_RECALL_ADMIN_SKIP_ROOT in the environment
        must NOT bypass the check — the var was a security hole in
        an earlier iteration and is gone now."""
        monkeypatch.setattr(CLI, "_require_root", _REAL_REQUIRE_ROOT)
        monkeypatch.setattr(CLI.os, "geteuid", lambda: 1000)
        monkeypatch.setenv("QDISTRO_RECALL_ADMIN_SKIP_ROOT", "1")
        with pytest.raises(SystemExit) as ei:
            CLI.main(["containers", "--uid", "2000"])
        assert ei.value.code != 0


class TestCallsRelayWithCorrectUid:
    """Pin that --uid N actually drives a call to
    org.qdistro.UserRelay.uidN — surprisingly absent from the
    initial test set."""

    def test_containers_routes_to_uid_specific_relay(self):
        fake = _FakeDBus(replies={
            ("SYSTEM", "org.qdistro.UserRelay.uid4242",
             "ForwardBrowserBridgeOp"): json.dumps(
                 {"ok": True, "containers": []}),
        })
        _client.set_dbus_client(fake)
        CLI.main(["containers", "--uid", "4242"])
        assert fake.calls[0]["service"] == "org.qdistro.UserRelay.uid4242"

    def test_tabs_routes_to_uid_specific_relay(self):
        fake = _FakeDBus(replies={
            ("SYSTEM", "org.qdistro.UserRelay.uid7777",
             "ForwardBrowserBridgeOp"): json.dumps(
                 {"ok": True, "tabs": []}),
        })
        _client.set_dbus_client(fake)
        CLI.main(["tabs", "--uid", "7777"])
        assert fake.calls[0]["service"] == "org.qdistro.UserRelay.uid7777"


class TestWalkDbsValidation:
    """Cover _walk_dbs edge cases (review T1)."""

    def test_missing_root_returns_empty(self):
        assert CLI._walk_dbs("/nonexistent/path", None) == []

    def test_traversal_silo_rejected_at_validator(self):
        with pytest.raises(ValueError):
            CLI._validate_silo("../etc")
        with pytest.raises(ValueError):
            CLI._validate_silo("foo/bar")
        with pytest.raises(ValueError):
            CLI._validate_silo(".hidden")
        with pytest.raises(ValueError):
            CLI._validate_silo("")
        # Valid names pass.
        CLI._validate_silo("work-user")
        CLI._validate_silo("work_2")

    def test_skips_non_dir_entries_at_silo_level(self, tmp_path):
        root = tmp_path / "recall"
        root.mkdir()
        # A stray file at the silo level (not a directory) is skipped.
        (root / "stray.txt").write_text("nope")
        # A real silo with a valid DB.
        import qdistro_recall_ingest as eng
        db = eng.db_path_for(str(root), "work-user")
        os.makedirs(os.path.dirname(db))
        eng.open_db(db).close()
        out = CLI._walk_dbs(str(root), None)
        assert any(p.endswith(".db") for p in out)

    def test_skips_non_db_files_in_ym_dir(self, tmp_path):
        root = tmp_path / "recall"
        ym = root / "work-user" / "2026-05"
        ym.mkdir(parents=True)
        (ym / "notes.txt").write_text("nope")
        (ym / "2026-05-16.db").write_text("not a real db, walker doesn't open")
        out = CLI._walk_dbs(str(root), None)
        assert len(out) == 1
        assert out[0].endswith("2026-05-16.db")
