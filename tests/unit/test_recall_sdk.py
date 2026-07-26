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
#
# Plain import, NOT spec_from_file_location + a sys.modules assignment:
# recall/ is on pytest's pythonpath so the name already resolves, and
# cli/qdistro_recall_cli.py imports this module lazily at call time — so
# re-executing it here would leave two copies and any cross-module
# `except SomeError` resolving the wrong one. That exact bug cost five
# permanently-red tests via tests/unit/test_vault_recovery.py; see
# tests/unit/test_no_duplicate_module_identity.py.
import qdistro_recall_ingest as _eng  # noqa: E402,F401

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
        assert "recall.push" not in bb.DEFAULT_HANDLERS
        assert list(tmp_path.rglob("*.db")) == []

    def test_production_style_load_resolves_sibling_and_cuts_recall(
            self, monkeypatch):
        """Load the bridge the way the VM s67-recall-probe.sh does — as a
        script with only its OWN directory on ``sys.path[0]`` — and assert
        ``recall.push`` is cut.

        The rest of this module imports the bridge through pytest's
        ``pythonpath`` injection (``pyproject.toml`` puts ``browser_bridge``
        on ``sys.path``), which silently satisfies the bridge's sibling
        ``import qdistro_browser_allowlist``. The VM probe runs OUTSIDE
        pytest, so that injection is absent: there the import resolves only
        because the probe puts the bridge's install dir on ``sys.path[0]``,
        mirroring production where the bridge runs as
        ``/usr/libexec/qdistro/qdistro_browser_bridge.py``. This test
        removes ``browser_bridge`` from ``sys.path`` and from the module
        cache so it exercises that same own-dir resolution path, guarding
        against a regression of the ``ModuleNotFoundError:
        qdistro_browser_allowlist`` that the suppressed traceback hid in
        the pwd-print-recall lane.
        """
        # Drop the pytest-injected path + cached modules so the load can
        # only succeed via the bridge's own directory (production contract).
        bb_dir = str(BB_DIR)
        monkeypatch.setattr(
            sys, "path",
            [p for p in sys.path if os.path.abspath(p) != bb_dir])
        for name in ("qdistro_browser_bridge", "qdistro_browser_allowlist"):
            monkeypatch.delitem(sys.modules, name, raising=False)

        sys.path.insert(0, bb_dir)
        spec = importlib.util.spec_from_file_location(
            "qdistro_browser_bridge_prodload",
            BB_DIR / "qdistro_browser_bridge.py")
        fresh = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(fresh)
        except ModuleNotFoundError as e:
            import pytest
            pytest.fail(
                "bridge failed to resolve its sibling import from its own "
                f"directory (the s67-recall-probe / production load path): {e}")

        identity = {
            "ppid": 1, "parent_exe": "/usr/lib64/firefox/firefox",
            "parent_selinux": "", "allowed": True,
        }
        resp = fresh.dispatch({"op": "recall.push", "text": "x"}, identity)
        assert resp.get("ok") is False, resp
        assert resp.get("error") == "unknown_op", resp
        assert "recall.push" not in fresh.DEFAULT_HANDLERS


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
