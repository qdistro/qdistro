"""Tests for dormant Recall internals and the v1 capture cut.

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
    def test_push_disabled_for_v1(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QDISTRO_RECALL_ROOT", str(tmp_path))
        try:
            recall_sdk.push_text_snapshot(
                "hello world from sdk", user="admin",
                url="https://qdistro.example/x")
        except recall_sdk.RecallDisabled:
            pass
        else:
            raise AssertionError("expected RecallDisabled")
        assert list(tmp_path.rglob("*.db")) == []

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
    def test_recall_push_is_not_registered_for_v1(self, tmp_path):
        identity = {
            "ppid": 1, "parent_exe": "/usr/lib64/firefox/firefox",
            "parent_selinux": "", "allowed": True,
        }
        resp = bb.dispatch(
            {"op": "recall.push", "text": "x"}, identity)
        assert resp.get("ok") is False, resp
        assert resp.get("error") == "unknown_op", resp
        assert list(tmp_path.rglob("*.db")) == []


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
