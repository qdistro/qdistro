"""Tests for the qdistro-recall-admin CLI.

The CLI sits on top of qdistro_recall_admin + qdistro_recall_ingest;
the modules themselves have their own coverage. These tests pin the
argparse dispatch, the human-readable output shape, the JSON output
shape, the exit codes, and the recall-DB walking integration.

We bypass the root check via the QDISTRO_RECALL_ADMIN_SKIP_ROOT=1
env var; CI agents and these tests never run as root.
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


# Force-disable the root check for every test in this file.
@pytest.fixture(autouse=True)
def _no_root_check(monkeypatch):
    monkeypatch.setenv("QDISTRO_RECALL_ADMIN_SKIP_ROOT", "1")
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
            ("SYSTEM", "com.qdistro.UserRelay.uid2000",
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
            ("SYSTEM", "com.qdistro.UserRelay.uid2000",
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
            ("SYSTEM", "com.qdistro.UserRelay.uid9999",
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
            ("SYSTEM", "com.qdistro.UserRelay.uid2000",
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
            ("SYSTEM", "com.qdistro.UserRelay.uid2000",
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
            ("SYSTEM", "com.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True, "tabs": [],
            }),
        })
        CLI.main(["tabs", "--uid", "2000"])
        assert "(no tabs)" in capsys.readouterr().out

    def test_calls_tabs_list_op(self):
        fake = _FakeDBus(replies={
            ("SYSTEM", "com.qdistro.UserRelay.uid2000",
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
            ("SYSTEM", "com.qdistro.UserRelay.uid2000",
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
                      if "https://b.example" in l or "foo extra context" in l)
        assert "[live" not in b_line

    def test_search_json_output(self, populated_recall_db, capsys):
        _set_fake_relay({
            ("SYSTEM", "com.qdistro.UserRelay.uid2000",
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
        rows = payload["rows"]
        # Two rows; one annotated, one not.
        live = [r for r in rows if r.get("live_tab")]
        not_live = [r for r in rows if not r.get("live_tab")]
        assert len(live) == 1
        assert len(not_live) == 1
        assert live[0]["live_tab"]["id"] == 7

    def test_search_relay_failure_falls_back_to_unannotated(
            self, populated_recall_db, capsys):
        """If the relay can't be reached we still want to see the
        recall results — just without the live-tab join."""
        _set_fake_relay({
            ("SYSTEM", "com.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": False, "error": "relay_call_failed",
            }),
        })
        rc = CLI.main([
            "--root", populated_recall_db,
            "search", "foo", "--uid", "2000",
        ])
        # Search still succeeds — relay failure is a warning.
        assert rc == 0
        captured = capsys.readouterr()
        # Both rows show up; no live markers.
        assert "[live:" not in captured.out
        assert "live-tab annotation unavailable" in captured.err

    def test_search_no_dbs_yields_empty(self, tmp_path, capsys):
        """A root with no DBs is a clean no-op, not an error."""
        empty_root = str(tmp_path / "empty")
        # The walker handles a non-existent root by returning [];
        # an empty existing root returns the same. Cover both.
        os.makedirs(empty_root)
        rc = CLI.main([
            "--root", empty_root,
            "search", "foo", "--uid", "2000",
        ])
        assert rc == 0
        assert "(no recall DBs)" in capsys.readouterr().err


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
    def test_skip_root_env_var_bypasses_check(self, capsys):
        """The escape hatch tests rely on must actually work."""
        _set_fake_relay({
            ("SYSTEM", "com.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True, "containers": [],
            }),
        })
        # QDISTRO_RECALL_ADMIN_SKIP_ROOT=1 is already set by the
        # autouse fixture; this asserts the bypass is effective.
        rc = CLI.main(["containers", "--uid", "2000"])
        assert rc == 0

    def test_root_check_calls_geteuid_when_not_skipped(
            self, monkeypatch, capsys):
        """If the bypass env var is absent, _require_root() is called
        and exits when geteuid() != 0."""
        monkeypatch.delenv("QDISTRO_RECALL_ADMIN_SKIP_ROOT",
                           raising=False)
        monkeypatch.setattr(CLI.os, "geteuid", lambda: 1000)
        with pytest.raises(SystemExit) as ei:
            CLI.main(["containers", "--uid", "2000"])
        assert ei.value.code != 0
        assert "must be run as root" in capsys.readouterr().err
