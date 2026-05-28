"""qdistro-pwd daemon ExportCookies unit tests (Bridge Phase 9d).

Drives the daemon's ExportCookies method directly (no D-Bus bring-up),
mirroring the Fill/Save/FillConfirm suite: bypass
dbus.service.Object.__init__, poke the method with a fake `sender`, and
stub _peer_info / snapshot_caller / _browser_bridge_allowed.

The intent-token / replay defense lives in the bridge, so the daemon
never sees a token; here we verify the daemon's own responsibilities:
kernel-attested caller identity, http(s)-only origins, fail-closed input
validation, and audit of every export (origin + count, never values).
"""
from __future__ import annotations

import json
import os
import sys

import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pwd"))

import qdistro_pwd_daemon as d
from qdistro_pwd_audit import PwdAuditLog  # type: ignore
from qdistro_pwd_vault import create_vault  # type: ignore


BROWSER_EXE = "/usr/lib64/firefox/firefox"

CALLER = {
    "uid": 1500,
    "pid": 12345,
    "exe": BROWSER_EXE,
    "exe_sha256": "aabbccdd",
    "selinux_label": "",
    "cgroup": "",
}


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """Daemon with a fresh audit db and a stubbed bridge-caller check."""
    vd = str(tmp_path / "vaults")
    audit_path = str(tmp_path / "audit.sqlite")
    monkeypatch.setattr(d, "VAULT_DIR", vd)
    monkeypatch.setattr(d, "AUDIT_DB", audit_path)
    monkeypatch.setattr(d, "BROWSER_PWD_VAULT", "passwords")
    monkeypatch.setattr(d, "_browser_bridge_allowed",
                        lambda _pid: (True, "test-bridge"))
    create_vault(vd, "passwords", b"vault-pass")
    daemon = d.PwdDaemon.__new__(d.PwdDaemon)
    daemon._unlocked = {}
    daemon._audit = PwdAuditLog(audit_path)
    return daemon, vd, audit_path


def _export(daemon, body, caller=CALLER):
    with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
         patch("qdistro_pwd_daemon.snapshot_caller", return_value=caller):
        return json.loads(daemon.ExportCookies(json.dumps(body),
                                               sender=":1.42"))


# ---------------------------------------------------------------------------
# Cookie-origin normalisation helper
# ---------------------------------------------------------------------------

class TestNormalizeCookieOrigin:
    def test_bare_host(self):
        assert d._normalize_cookie_origin("example.com") == "https://example.com"

    def test_full_https_url(self):
        assert d._normalize_cookie_origin(
            "https://example.com/path") == "https://example.com"

    def test_full_http_url(self):
        assert d._normalize_cookie_origin(
            "http://example.com/x") == "http://example.com"

    def test_standard_port_stripped(self):
        assert d._normalize_cookie_origin(
            "https://example.com:443/x") == "https://example.com"

    def test_non_standard_port_kept(self):
        assert d._normalize_cookie_origin(
            "https://example.com:8443") == "https://example.com:8443"

    def test_case_normalised(self):
        assert d._normalize_cookie_origin(
            "HTTPS://EXAMPLE.COM/P") == "https://example.com"

    def test_file_scheme_rejected(self):
        assert d._normalize_cookie_origin("file:///etc/passwd") == ""

    def test_data_scheme_rejected(self):
        assert d._normalize_cookie_origin("data:text/html,hi") == ""

    def test_about_scheme_rejected(self):
        assert d._normalize_cookie_origin("about:config") == ""

    def test_javascript_scheme_rejected(self):
        assert d._normalize_cookie_origin("javascript:alert(1)") == ""

    def test_empty_rejected(self):
        assert d._normalize_cookie_origin("") == ""

    def test_nul_rejected(self):
        assert d._normalize_cookie_origin("exa\x00mple.com") == ""

    def test_malformed_port_rejected(self):
        # urlparse .port raises ValueError; must fail closed, not crash.
        assert d._normalize_cookie_origin("http://example.com:bad") == ""

    def test_out_of_range_port_rejected(self):
        assert d._normalize_cookie_origin("https://example.com:99999") == ""


# ---------------------------------------------------------------------------
# ExportCookies — happy path
# ---------------------------------------------------------------------------

class TestExportCookiesHappyPath:
    def test_export_returns_count(self, staged):
        daemon, _, _ = staged
        resp = _export(daemon, {
            "domain": "example.com",
            "cookie_store_id": "firefox-container-1",
            "cookies": [{"name": "sid", "value": "abc"},
                        {"name": "csrf", "value": "xyz"}],
            "extension_id": "test@ext",
            "parent_exe": BROWSER_EXE,
        })
        assert resp["ok"] is True
        assert resp["exported"] == 2

    def test_export_full_url_domain(self, staged):
        daemon, _, _ = staged
        resp = _export(daemon, {
            "domain": "https://example.com/login",
            "cookies": [{"name": "sid", "value": "abc"}],
        })
        assert resp["ok"] is True
        assert resp["exported"] == 1

    def test_export_empty_cookie_list_ok(self, staged):
        daemon, _, _ = staged
        resp = _export(daemon, {"domain": "x.com", "cookies": []})
        assert resp["ok"] is True
        assert resp["exported"] == 0

    def test_export_audit_logged_with_origin_and_count(self, staged):
        daemon, _, _ = staged
        _export(daemon, {
            "domain": "example.com",
            "cookies": [{"name": "sid", "value": "TOP-SECRET-VALUE"}],
        })
        rows = daemon._audit.tail(10)
        ce = [r for r in rows if r["op"] == "cookies-export"]
        assert len(ce) == 1
        assert ce[0]["decision"] == "allow"
        # origin recorded, count recorded
        assert ce[0]["item_tag"] == "https://example.com"
        assert ce[0]["reason"] == "count:1"

    def test_export_never_logs_cookie_values(self, staged):
        daemon, _, _ = staged
        secret = "TOP-SECRET-COOKIE-VALUE"
        _export(daemon, {
            "domain": "example.com",
            "cookies": [{"name": "sid", "value": secret}],
        })
        # The whole audit row must not contain the cookie value anywhere.
        rows = daemon._audit.tail(10)
        for r in rows:
            for v in r.values():
                assert secret not in str(v)


# ---------------------------------------------------------------------------
# ExportCookies — identity / policy denial
# ---------------------------------------------------------------------------

class TestExportCookiesIdentity:
    def test_rejects_non_bridge_caller(self, staged, monkeypatch):
        daemon, _, _ = staged
        monkeypatch.setattr(d, "_browser_bridge_allowed",
                            lambda _pid: (False, "not-browser-bridge"))
        resp = _export(daemon, {
            "domain": "example.com",
            "cookies": [{"name": "sid", "value": "abc"}],
        })
        assert resp["ok"] is False
        assert resp["error"] == "policy_denied"

    def test_non_bridge_caller_audited_deny(self, staged, monkeypatch):
        daemon, _, _ = staged
        monkeypatch.setattr(d, "_browser_bridge_allowed",
                            lambda _pid: (False, "parent-not-browser"))
        _export(daemon, {"domain": "example.com", "cookies": []})
        rows = daemon._audit.tail(10)
        ce = [r for r in rows if r["op"] == "cookies-export"]
        assert len(ce) == 1
        assert ce[0]["decision"] == "deny"
        assert "parent-not-browser" in ce[0]["reason"]


# ---------------------------------------------------------------------------
# ExportCookies — malformed / forbidden input (fail closed)
# ---------------------------------------------------------------------------

class TestExportCookiesInput:
    def test_invalid_json(self, staged):
        daemon, _, _ = staged
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            resp = json.loads(daemon.ExportCookies("{not json",
                                                   sender=":1.42"))
        assert resp["ok"] is False
        assert resp["error"] == "invalid_request"

    @pytest.mark.parametrize("payload", ["[]", '"x"', "123", "null"])
    def test_non_object_json_fails_closed(self, staged, payload):
        daemon, _, _ = staged
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            resp = json.loads(daemon.ExportCookies(payload, sender=":1.42"))
        assert resp["ok"] is False
        assert resp["error"] == "invalid_request"
        rows = daemon._audit.tail(10)
        ce = [r for r in rows if r["op"] == "cookies-export"]
        assert ce and ce[0]["decision"] == "deny"
        assert ce[0]["reason"] == "non-object-json"

    def test_missing_domain(self, staged):
        daemon, _, _ = staged
        resp = _export(daemon, {"cookies": []})
        assert resp["ok"] is False
        assert resp["error"] == "invalid_request"

    def test_empty_domain(self, staged):
        daemon, _, _ = staged
        resp = _export(daemon, {"domain": "", "cookies": []})
        assert resp["ok"] is False
        assert resp["error"] == "invalid_request"

    def test_file_scheme_rejected(self, staged):
        daemon, _, _ = staged
        resp = _export(daemon, {
            "domain": "file:///etc/passwd",
            "cookies": [{"name": "x", "value": "y"}],
        })
        assert resp["ok"] is False
        assert resp["error"] == "invalid_request"

    def test_data_scheme_rejected(self, staged):
        daemon, _, _ = staged
        resp = _export(daemon, {
            "domain": "data:text/html,hi",
            "cookies": [],
        })
        assert resp["ok"] is False
        assert resp["error"] == "invalid_request"

    def test_bad_origin_audited(self, staged):
        daemon, _, _ = staged
        _export(daemon, {"domain": "file:///etc/passwd", "cookies": []})
        rows = daemon._audit.tail(10)
        ce = [r for r in rows if r["op"] == "cookies-export"]
        assert len(ce) == 1
        assert ce[0]["decision"] == "deny"
        assert ce[0]["reason"] == "bad-origin"

    def test_malformed_port_denied_not_crash(self, staged):
        daemon, _, _ = staged
        resp = _export(daemon, {
            "domain": "http://example.com:bad",
            "cookies": [],
        })
        assert resp["ok"] is False
        assert resp["error"] == "invalid_request"
        rows = daemon._audit.tail(10)
        ce = [r for r in rows if r["op"] == "cookies-export"]
        assert ce and ce[0]["decision"] == "deny"
        assert ce[0]["reason"] == "bad-origin"

    def test_cookies_not_a_list(self, staged):
        daemon, _, _ = staged
        resp = _export(daemon, {"domain": "example.com", "cookies": "nope"})
        assert resp["ok"] is False
        assert resp["error"] == "invalid_request"

    def test_missing_cookies_field(self, staged):
        daemon, _, _ = staged
        resp = _export(daemon, {"domain": "example.com"})
        assert resp["ok"] is False
        assert resp["error"] == "invalid_request"
