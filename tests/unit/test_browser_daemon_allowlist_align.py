"""S8 P0-4 follow-up — daemon parent-exe gates align with the bridge opt-in.

Before this change the 9e daemon identity gate
(``qdistro_browser_daemon_identity``) and the pwd daemon both carried an
independent, default-ON Brave/Vivaldi/Chrome/Edge matrix while the bridge
*entry* gate had moved to a Firefox+Chromium baseline + root-owned admin
opt-in (P0-4). That let the defense-in-depth gates drift WIDER than the
entry gate. These tests pin that the daemon gates now resolve the trusted
parent set through the SAME shared module
(``qdistro_browser_allowlist``) so they cannot diverge, and that an optional
browser is rejected as a parent until an admin opts it in.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
import qdistro_browser_allowlist as alw
import qdistro_browser_daemon_identity as ident
import qdistro_pwd_daemon as pwd

_OPTIONAL_EXES = (
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    "/usr/bin/brave", "/usr/bin/brave-browser",
    "/usr/bin/vivaldi", "/usr/bin/vivaldi-stable",
    "/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable",
)
_FF = "/usr/lib64/firefox/firefox"
_CHROME = "/usr/bin/google-chrome"


def _load_bridge():
    """Load the bridge module by path (mirrors test_browser_bridge.py)."""
    mod = (Path(__file__).resolve().parents[2]
           / "browser_bridge" / "qdistro_browser_bridge.py")
    spec = importlib.util.spec_from_file_location(
        "qdistro_browser_bridge", mod)
    bb = importlib.util.module_from_spec(spec)
    sys.modules["qdistro_browser_bridge"] = bb
    spec.loader.exec_module(bb)
    return bb


# ---- shared module: the single source of truth ----------------------

class TestSharedResolver:
    def test_baseline_excludes_optionals(self):
        base = alw.resolve_parent_exes(
            config_path="/nonexistent/allowlist.conf")
        assert _FF in base
        assert "/usr/bin/chromium" in base
        for exe in _OPTIONAL_EXES:
            assert exe not in base

    def test_optin_adds_only_named_browser(self, tmp_path):
        cfg = tmp_path / "allow.conf"
        cfg.write_text("chrome\n", encoding="utf-8")
        cfg.chmod(0o644)
        out = alw.resolve_parent_exes(
            config_path=str(cfg), trusted_uid=os.geteuid())
        assert _CHROME in out
        assert "/usr/bin/google-chrome-stable" in out
        assert "/usr/bin/brave" not in out          # per-browser opt-in
        assert _FF in out                            # baseline preserved

    def test_non_root_owned_config_ignored(self, tmp_path):
        # trusted_uid that is NOT the file owner => fail closed to baseline.
        cfg = tmp_path / "allow.conf"
        cfg.write_text("chrome\n", encoding="utf-8")
        out = alw.resolve_parent_exes(
            config_path=str(cfg), trusted_uid=os.geteuid() + 12345)
        assert _CHROME not in out
        assert out == alw.DEFAULT_ALLOWED_PARENT_EXES


# ---- daemon identity gate (browser_daemons) -------------------------

class TestDaemonIdentityResolver:
    def test_default_is_baseline_not_full_matrix(self, monkeypatch):
        # No env override, shared module unavailable => baseline, fail-closed.
        monkeypatch.setattr(ident, "_PARENT_EXES_ENV_OVERRIDE", None)
        monkeypatch.setattr(ident, "_allowlist", None)
        out = ident.resolve_parent_exes()
        assert out == ident._BASELINE_PARENT_EXES
        for exe in _OPTIONAL_EXES:
            assert exe not in out

    def test_delegates_to_shared_module(self, monkeypatch):
        sentinel = ("/sentinel/browser",)
        monkeypatch.setattr(ident, "_PARENT_EXES_ENV_OVERRIDE", None)
        monkeypatch.setattr(
            ident, "_allowlist",
            type("F", (), {"resolve_parent_exes": staticmethod(
                lambda: sentinel)}))
        assert ident.resolve_parent_exes() == sentinel

    def test_env_override_replaces_entirely(self, monkeypatch):
        monkeypatch.setattr(
            ident, "_PARENT_EXES_ENV_OVERRIDE", ("/opt/custom/browser",))
        assert ident.resolve_parent_exes() == ("/opt/custom/browser",)


class TestDaemonIdentityGate:
    """browser_bridge_allowed honours the resolved set, not the old matrix."""

    BRIDGE = "/usr/libexec/qdistro/qdistro_browser_bridge.py"

    def _call(self, parent_exe, monkeypatch, allowlist=None):
        # Force the gate through the live resolver (no explicit parent_exes
        # arg) so we exercise resolve_parent_exes(), not a passed-in set.
        monkeypatch.setattr(ident, "_PARENT_EXES_ENV_OVERRIDE", None)
        if allowlist is None:
            monkeypatch.setattr(ident, "_allowlist", None)  # => baseline
        else:
            monkeypatch.setattr(
                ident, "_allowlist",
                type("F", (), {"resolve_parent_exes": staticmethod(
                    lambda: allowlist)}))
        return ident.browser_bridge_allowed(
            4242,
            bridge_script=self.BRIDGE,
            cmdline_reader=lambda pid: ["python3", self.BRIDGE, "ext://x/"],
            ppid_reader=lambda pid: 100,
            exe_reader=lambda pid: parent_exe,
        )

    def test_firefox_parent_accepted_by_default(self, monkeypatch):
        ok, reason = self._call(_FF, monkeypatch)
        assert ok and reason == "browser-bridge"

    def test_chrome_parent_rejected_by_default(self, monkeypatch):
        # The whole point of the follow-up: chrome is NOT a trusted parent
        # until opted in, even at the daemon defense-in-depth gate.
        ok, reason = self._call(_CHROME, monkeypatch)
        assert not ok and reason == "parent-not-browser"

    def test_chrome_parent_accepted_when_opted_in(self, monkeypatch):
        opted = ident._BASELINE_PARENT_EXES + (
            "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable")
        ok, reason = self._call(_CHROME, monkeypatch, allowlist=opted)
        assert ok and reason == "browser-bridge"


# ---- pwd daemon gate ------------------------------------------------

class TestPwdDaemonResolver:
    def test_default_is_baseline_not_full_matrix(self, monkeypatch):
        monkeypatch.setattr(pwd, "_BROWSER_PARENT_EXES_ENV_OVERRIDE", None)
        monkeypatch.setattr(pwd, "_browser_allowlist", None)
        out = pwd._resolve_browser_parent_exes()
        assert out == pwd._BROWSER_BASELINE_PARENT_EXES
        for exe in _OPTIONAL_EXES:
            assert exe not in out

    def test_delegates_to_shared_module(self, monkeypatch):
        sentinel = ("/sentinel/browser",)
        monkeypatch.setattr(pwd, "_BROWSER_PARENT_EXES_ENV_OVERRIDE", None)
        monkeypatch.setattr(
            pwd, "_browser_allowlist",
            type("F", (), {"resolve_parent_exes": staticmethod(
                lambda: sentinel)}))
        assert pwd._resolve_browser_parent_exes() == sentinel

    def test_retries_shared_module_import_after_startup(self, monkeypatch):
        monkeypatch.setattr(pwd, "_BROWSER_PARENT_EXES_ENV_OVERRIDE", None)
        monkeypatch.setattr(pwd, "_browser_allowlist", None)
        out = pwd._resolve_browser_parent_exes()
        assert out == alw.resolve_parent_exes()
        assert pwd._browser_allowlist is alw

    def test_env_override_replaces_entirely(self, monkeypatch):
        monkeypatch.setattr(
            pwd, "_BROWSER_PARENT_EXES_ENV_OVERRIDE", ("/opt/x",))
        assert pwd._resolve_browser_parent_exes() == ("/opt/x",)


# ---- drift guard: the entry gate and the daemon gates agree ---------

class TestNoDriftAcrossGates:
    def test_bridge_and_daemons_resolve_identically(self, tmp_path,
                                                    monkeypatch):
        bb = _load_bridge()
        # Same baseline-only state everywhere (no env override, no config).
        monkeypatch.delenv("QDISTRO_BROWSER_BRIDGE_ALLOWLIST", raising=False)
        monkeypatch.delenv(
            "QDISTRO_BROWSER_BRIDGE_ALLOWLIST_TEST", raising=False)
        absent = str(tmp_path / "absent.conf")
        bridge_base = bb._resolve_allowlist(config_path=absent)
        shared_base = alw.resolve_parent_exes(config_path=absent)
        assert bridge_base == shared_base == alw.DEFAULT_ALLOWED_PARENT_EXES

        # Same chrome opt-in everywhere => same resolved set.
        cfg = tmp_path / "allow.conf"
        cfg.write_text("chrome\n", encoding="utf-8")
        cfg.chmod(0o644)
        uid = os.geteuid()
        bridge_optin = bb._resolve_allowlist(
            config_path=str(cfg), trusted_uid=uid)
        shared_optin = alw.resolve_parent_exes(
            config_path=str(cfg), trusted_uid=uid)
        assert bridge_optin == shared_optin
        assert _CHROME in bridge_optin

    def test_daemon_wrappers_default_to_shared_baseline(self, monkeypatch):
        # With the real shared module and no env override, both daemon
        # resolvers return exactly what the shared module returns for the
        # default (production) path — i.e. they do not add their own set.
        monkeypatch.setattr(ident, "_PARENT_EXES_ENV_OVERRIDE", None)
        monkeypatch.setattr(ident, "_allowlist", alw)
        monkeypatch.setattr(pwd, "_BROWSER_PARENT_EXES_ENV_OVERRIDE", None)
        monkeypatch.setattr(pwd, "_browser_allowlist", alw)
        expected = alw.resolve_parent_exes()
        assert ident.resolve_parent_exes() == expected
        assert pwd._resolve_browser_parent_exes() == expected
