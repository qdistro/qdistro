"""Tests for qdistro_app.recall + qdistro_recall_cli + recall.push
bridge dispatch.

The SDK module routes through the engine module that lives at
recall/qdistro_recall_ingest.py. We rebind the engine import
path manually so tests don't depend on /usr/libexec/qdistro being
populated.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

# Make recall importable.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RECALL_DIR = REPO_ROOT / "recall"
SDK_DIR = REPO_ROOT / "sdk"
CLI_DIR = REPO_ROOT / "cli"
BB_DIR = REPO_ROOT / "browser_bridge"

# Pre-import the engine so `import qdistro_recall_ingest` resolves
# without going through /usr/libexec/qdistro.
_eng_path = RECALL_DIR / "qdistro_recall_ingest.py"
_spec = importlib.util.spec_from_file_location(
    "qdistro_recall_ingest", _eng_path)
_eng = importlib.util.module_from_spec(_spec)
sys.modules["qdistro_recall_ingest"] = _eng
_spec.loader.exec_module(_eng)

# Make the SDK importable.
if str(SDK_DIR) not in sys.path:
    sys.path.insert(0, str(SDK_DIR))


def _load_module(label: str, path: Path):
    spec = importlib.util.spec_from_file_location(label, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[label] = mod
    spec.loader.exec_module(mod)
    return mod


# Load the recall SDK directly (the qdistro_app package is a name
# clash with the in-tree __init__.py that imports dbus eagerly).
recall_sdk = _load_module(
    "qdistro_app_recall_test", SDK_DIR / "qdistro_app" / "recall.py")
cli_mod = _load_module(
    "qdistro_recall_cli", CLI_DIR / "qdistro_recall_cli.py")
bb = _load_module(
    "qdistro_browser_bridge", BB_DIR / "qdistro_browser_bridge.py")


# ---- SDK push_text_snapshot --------------------------------------

class TestSdk:
    def test_push_writes_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QDISTRO_RECALL_ROOT", str(tmp_path))
        rowid = recall_sdk.push_text_snapshot(
            "hello world from sdk", user="admin",
            url="https://qdistro.example/x")
        assert rowid >= 1
        # Confirm the file landed in the expected per-day path.
        dbs = list(tmp_path.rglob("*.db"))
        assert len(dbs) == 1
        # Open + count.
        import sqlite3
        conn = sqlite3.connect(str(dbs[0]))
        n = conn.execute(
            "SELECT COUNT(*) FROM recall_entries").fetchone()[0]
        assert n == 1

    def test_push_pwd_domain_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QDISTRO_RECALL_ROOT", str(tmp_path))
        try:
            recall_sdk.push_text_snapshot(
                "pwd UI text",
                user="admin", secctx="qdistro:pwd:ui")
        except _eng.PwdDomainRefused:
            pass
        else:
            raise AssertionError("expected PwdDomainRefused")

    def test_push_empty_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QDISTRO_RECALL_ROOT", str(tmp_path))
        try:
            recall_sdk.push_text_snapshot("   ", user="admin")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")

    def test_exclude_fields_stub(self):
        out = recall_sdk.exclude_fields(["pwd-input", "card-cvv"])
        assert out == ["pwd-input", "card-cvv"]


# ---- CLI ----------------------------------------------------------

class TestCli:
    def _run(self, *args, env=None, capsys=None):
        argv = list(args)
        rc = cli_mod.main(argv)
        return rc

    def test_push_then_search_roundtrip(self, tmp_path, monkeypatch,
                                        capsys):
        monkeypatch.setenv("QDISTRO_RECALL_ROOT", str(tmp_path))
        rc = cli_mod.main(["push", "the quick brown fox", "--user", "admin"])
        assert rc == 0
        capsys.readouterr()  # drain push output
        rc = cli_mod.main(["search", "quick", "--user", "admin"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "the quick brown fox" in out

    def test_search_no_dbs(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QDISTRO_RECALL_ROOT", str(tmp_path))
        rc = cli_mod.main(["search", "anything"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "no recall DBs" in err

    def test_info_reports_paths(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QDISTRO_RECALL_ROOT", str(tmp_path))
        rc = cli_mod.main(["info", "--user", "admin"])
        assert rc == 0
        out = capsys.readouterr().out
        assert f"root={tmp_path}" in out
        assert "user=admin" in out
        assert "today_count=0" in out

    def test_pwd_domain_push_rc(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QDISTRO_RECALL_ROOT", str(tmp_path))
        rc = cli_mod.main([
            "push", "secret pwd UI text",
            "--user", "admin",
            "--secctx", "qdistro:pwd:fill",
        ])
        assert rc == 3
        err = capsys.readouterr().err
        assert "refused" in err

    def test_purge_older_than(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QDISTRO_RECALL_ROOT", str(tmp_path))
        # Insert one row with ts in the past.
        path = _eng.db_path_for(str(tmp_path), "admin")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = _eng.open_db(path)
        try:
            _eng.push_text(conn, user="admin", text="old",
                           source="sdk", ts=0.0)
            _eng.push_text(conn, user="admin", text="new",
                           source="sdk")
        finally:
            conn.close()
        capsys.readouterr()
        rc = cli_mod.main(["purge", "--older-than-days", "1",
                           "--user", "admin"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "purged=1" in out


# ---- Bridge recall.push dispatch ---------------------------------

class TestBridgeRecallPush:
    def test_recall_push_inserts_via_default_impl(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("QDISTRO_RECALL_ROOT", str(tmp_path))
        identity = {
            "ppid": 1, "parent_exe": "/usr/lib64/firefox/firefox",
            "parent_selinux": "", "allowed": True,
        }
        # P0-3: bridge no longer accepts msg.user; destination is
        # derived from the bridge process's own UID
        # (getpass.getuser()).
        msg = {"op": "recall.push",
               "text": "page snapshot text",
               "url": "https://qdistro.example/page"}
        # Reset any test-injected impl.
        bb._recall_push_impl = None
        resp = bb.dispatch(msg, identity)
        assert resp.get("ok") is True, resp
        assert resp.get("op") == "recall.push", resp
        assert resp.get("row_id") >= 1, resp
        # The row lands under the test runner's user — proving the
        # bridge ignored any potential msg.user spoofing.
        import getpass
        assert resp.get("user") == getpass.getuser(), resp
        import sqlite3
        dbs = list(tmp_path.rglob("*.db"))
        assert len(dbs) == 1
        conn = sqlite3.connect(str(dbs[0]))
        n = conn.execute(
            "SELECT COUNT(*) FROM recall_entries").fetchone()[0]
        assert n == 1

    def test_recall_push_ignores_spoofed_user_field(
            self, tmp_path, monkeypatch):
        """P0-3 regression: a stdio-supplied user field must not steer
        the destination directory. The bridge MUST derive user from
        its own kernel-attested UID, not from extension JSON."""
        import getpass
        monkeypatch.setenv("QDISTRO_RECALL_ROOT", str(tmp_path))
        identity = {
            "ppid": 1, "parent_exe": "/usr/lib64/firefox/firefox",
            "parent_selinux": "", "allowed": True,
        }
        bb._recall_push_impl = None
        resp = bb.dispatch(
            {"op": "recall.push",
             "text": "spoof attempt",
             # A compromised extension cannot steer the row into
             # another user's recall directory.
             "user": "victim-user"},
            identity)
        assert resp.get("ok") is True, resp
        assert resp.get("user") == getpass.getuser(), resp
        assert "victim-user" not in str(resp.get("db", ""))

    def test_recall_push_pwd_domain_refused_via_dispatch(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("QDISTRO_RECALL_ROOT", str(tmp_path))
        identity = {
            "ppid": 1, "parent_exe": "/usr/lib64/firefox/firefox",
            "parent_selinux": "", "allowed": True,
        }
        msg = {"op": "recall.push",
               "text": "pwd field text",
               "secctx": "qdistro:pwd:fill"}
        bb._recall_push_impl = None
        resp = bb.dispatch(msg, identity)
        assert resp.get("ok") is False, resp
        assert resp.get("error") == "pwd_domain_refused", resp

    def test_recall_push_missing_text(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QDISTRO_RECALL_ROOT", str(tmp_path))
        identity = {"ppid": 1, "parent_exe": "/usr/bin/chromium",
                    "parent_selinux": "", "allowed": True}
        bb._recall_push_impl = None
        resp = bb.dispatch({"op": "recall.push"}, identity)
        assert resp.get("ok") is False, resp
        assert resp.get("error") == "missing_text", resp

    def test_recall_push_test_impl_override(self):
        identity = {"ppid": 1, "parent_exe": "/x", "parent_selinux": "",
                    "allowed": True}
        called = []

        def stub(msg, ident, text):
            called.append((msg, ident, text))
            return {"ok": True, "stub": True}

        bb._recall_push_impl = stub
        try:
            resp = bb.dispatch(
                {"op": "recall.push", "text": "x"}, identity)
        finally:
            bb._recall_push_impl = None
        assert resp.get("stub") is True, resp
        assert called and called[0][2] == "x"

    def test_recall_push_denied_when_parent_not_allowed(self):
        identity = {"ppid": 1, "parent_exe": "/no/such",
                    "parent_selinux": "", "allowed": False}
        bb._recall_push_impl = None
        resp = bb.dispatch(
            {"op": "recall.push", "text": "x"}, identity)
        assert resp.get("ok") is False, resp
        assert resp.get("error") == "parent_not_allowed", resp


# ---- Daemon (TTL reaper) ------------------------------------------

class TestReaper:
    def test_reap_purges_old_and_drops_empty_dbs(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("QDISTRO_RECALL_ROOT", str(tmp_path))
        path = _eng.db_path_for(str(tmp_path), "admin")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = _eng.open_db(path)
        try:
            _eng.push_text(conn, user="admin", text="old",
                           source="sdk", ts=0.0)
        finally:
            conn.close()
        # Reaper module — load by file path.
        reaper = _load_module(
            "qdistro_recall_daemon",
            RECALL_DIR / "qdistro_recall_daemon.py")
        stats = reaper.reap(str(tmp_path), "admin", ttl_days=1)
        assert stats["purged"] == 1
        assert stats["dbs_deleted"] == 1
        # Empty month dir should have been removed.
        # The day file (and WAL/SHM) under the month dir is gone, so
        # the month dir is empty → reaper removes it too.
        for root_, dirs, files in os.walk(str(tmp_path / "admin")):
            assert ".db" not in " ".join(files), \
                f"unexpected leftover: {files}"
