"""Tests for snapshots/qdistro_snapshots — task(115), spec/19 §MVP.

Pure-python wrapper around org.opensuse.Snapper. Tests inject a
fake transport callable that records calls + returns scripted
results, so we never touch dbus-python or a live Snapper.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Plain import, NOT spec_from_file_location + sys.modules assignment:
# qdistro_snapshots is importable by name (its dir is on pytest's pythonpath) AND is
# lazily imported at call time by product code, so re-executing it here
# would create a second copy of every class and any cross-module
# `except SomeError` would silently stop catching. That exact bug cost
# five permanently-red tests via tests/unit/test_vault_recovery.py; see
# tests/unit/test_no_duplicate_module_identity.py.
import qdistro_snapshots as sn  # noqa: E402


class FakeTransport:
    """Records calls + dispatches scripted return values."""

    def __init__(self, returns=None, raises=None):
        self.calls: list[tuple] = []
        self._returns = returns or {}
        self._raises = raises or {}

    def __call__(self, method, *args):
        self.calls.append((method, args))
        if method in self._raises:
            raise self._raises[method]
        if method in self._returns:
            ret = self._returns[method]
            if callable(ret):
                return ret(*args)
            return ret
        return None


# ---- creates ----

class TestCreate:
    def test_single(self):
        t = FakeTransport(returns={"CreateSingleSnapshot": 42})
        c = sn.SnapperClient(t)
        n = c.create_single("root", "mid-flight",
                            userdata={"foo": "bar"})
        assert n == 42
        method, args = t.calls[0]
        assert method == "CreateSingleSnapshot"
        cfg, desc, cleanup, ud = args
        assert cfg == "root"
        assert desc == "mid-flight"
        assert cleanup == "number"
        assert ud["qdistro.origin"] == "1"
        assert ud["foo"] == "bar"

    def test_pre(self):
        t = FakeTransport(returns={"CreatePreSnapshot": 7})
        c = sn.SnapperClient(t)
        n = c.create_pre("root", "before zypper")
        assert n == 7
        assert t.calls[0][0] == "CreatePreSnapshot"

    def test_post(self):
        t = FakeTransport(returns={"CreatePostSnapshot": 8})
        c = sn.SnapperClient(t)
        n = c.create_post("root", 7, "after zypper")
        assert n == 8
        method, args = t.calls[0]
        assert method == "CreatePostSnapshot"
        # pre-number is the second arg
        assert args[0] == "root"
        assert args[1] == 7

    def test_create_failure_wrapped(self):
        t = FakeTransport(
            raises={"CreateSingleSnapshot":
                    PermissionError("auth failed")})
        c = sn.SnapperClient(t)
        try:
            c.create_single("root", "x")
        except sn.SnapshotError as e:
            assert "CreateSingleSnapshot" in str(e)
            assert "auth failed" in e.detail
            assert isinstance(e.__cause__, PermissionError)
        else:
            raise AssertionError("expected SnapshotError")


# ---- list / get_files ----

class TestList:
    def test_list_normalisation(self):
        sample = [
            (1, "single", 0, 1700000000.0, 0, "first", "number",
             {"qdistro.origin": "1", "foo": "bar"}),
            (2, "pre", 0, 1700001000.0, 1000, "before zypper",
             "number", {}),
        ]
        t = FakeTransport(returns={"ListSnapshots": sample})
        c = sn.SnapperClient(t)
        rows = c.list("root")
        assert len(rows) == 2
        assert rows[0]["num"] == 1
        assert rows[0]["description"] == "first"
        assert rows[0]["userdata"]["foo"] == "bar"
        assert rows[0]["qdistro_origin"] is True
        assert rows[1]["qdistro_origin"] is False

    def test_get_files(self):
        sample = [
            ("/var/lib/qdistro/vaults/work-user.json", "M"),
            ("/var/lib/qdistro/vaults/work-user.bak", "+"),
        ]
        t = FakeTransport(returns={"GetFiles": sample})
        c = sn.SnapperClient(t)
        rows = c.get_files("qdistro_vaults", 1, 2)
        assert rows[0]["path"].endswith("work-user.json")
        assert rows[0]["status"] == "M"
        assert rows[1]["status"] == "+"

    def test_delete(self):
        t = FakeTransport()
        c = sn.SnapperClient(t)
        c.delete_snapshots("root", [3, 4, 5])
        method, args = t.calls[0]
        assert method == "DeleteSnapshots"
        assert args == ("root", [3, 4, 5])


# ---- helpers ----

class TestSnapshotBefore:
    def test_carries_caller_identity(self):
        t = FakeTransport(returns={"CreateSingleSnapshot": 99})
        c = sn.SnapperClient(t)
        n = sn.snapshot_before(c, "root", "before mass-update",
                                caller_uid=1000,
                                caller_exe="/usr/bin/qdistro-pwd")
        assert n == 99
        ud = t.calls[0][1][3]
        assert ud["qdistro.caller_uid"] == "1000"
        assert ud["qdistro.caller_exe"] == "/usr/bin/qdistro-pwd"
        assert ud["qdistro.action"] == "before"

    def test_works_without_caller_info(self):
        t = FakeTransport(returns={"CreateSingleSnapshot": 1})
        c = sn.SnapperClient(t)
        n = sn.snapshot_before(c, "root", "ad-hoc")
        assert n == 1


class TestVaultSnapshot:
    def test_userdata_shape(self):
        t = FakeTransport(returns={"CreateSingleSnapshot": 5})
        c = sn.SnapperClient(t)
        n = sn.vault_snapshot(c, "add", "Bank Login")
        assert n == 5
        method, args = t.calls[0]
        cfg, desc, cleanup, ud = args
        assert cfg == "qdistro_vaults"
        assert "Bank Login" in desc
        assert ud["qdistro.action"] == "add"
        assert ud["qdistro.item"] == "Bank Login"
        assert "qdistro.ts" in ud


# ---- recipients parsing ----

class TestParseRecipients:
    def test_basic(self):
        text = (
            "# qdistro backup recipients\n"
            "age1abc...\n"
            "\n"
            "age1def...\n"
        )
        assert sn.parse_backup_recipients(text) == [
            "age1abc...", "age1def..."]

    def test_skips_non_age(self):
        text = "ssh-rsa AAA...\nage1foo...\n"
        assert sn.parse_backup_recipients(text) == ["age1foo..."]

    def test_dedup_preserves_order(self):
        text = "age1a\nage1b\nage1a\n"
        assert sn.parse_backup_recipients(text) == ["age1a", "age1b"]

    def test_empty(self):
        assert sn.parse_backup_recipients("") == []
        assert sn.parse_backup_recipients(None) == []  # type: ignore


# The legacy ``render_backup_command`` shell-pipeline builder and its tests
# were removed (opus-security-review HIGH #4 — command injection). The
# scheduled backup path is the signed-manifest engine; see
# ``test_backup_manifest.py`` / ``test_backup_cli.py``.
