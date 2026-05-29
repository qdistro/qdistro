"""Structured-audit coverage for the autofill decision point.

Per todo/security-hardening-carryforward.md §"Clipboard":

    Autofill approval or denial should produce a structured audit event
    at the component that has the full decision context, including
    origin URL, silo/app context, extension/browser identity, decision,
    and denial reason, without logging password material.

These tests prove, against a real on-disk vault + audit sqlite (no
D-Bus), that every Fill / FillConfirm / Save *allow AND deny* emits a
structured pwd_audit row carrying origin + bridge/extension identity +
silo/app context + decision + reason, and that NO credential material
ever lands in any audit row.
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
from qdistro_pwd_vault import (  # type: ignore
    add_item, create_vault, unlock_vault,
)


BROWSER_EXE = "/usr/lib64/firefox/firefox"
SECRET = "sup3r-s3cret-pässw0rd!"  # must never appear in audit rows

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
    vd = str(tmp_path / "vaults")
    audit_path = str(tmp_path / "audit.sqlite")
    monkeypatch.setattr(d, "VAULT_DIR", vd)
    monkeypatch.setattr(d, "AUDIT_DB", audit_path)
    monkeypatch.setattr(d, "BROWSER_PWD_VAULT", "passwords")
    monkeypatch.setattr(d, "_browser_bridge_allowed",
                        lambda _pid: (True, "test-bridge"))
    # Deterministic attested app id (parent-browser exe) for assertions.
    monkeypatch.setattr(d, "_bridge_app_id", lambda _pid: BROWSER_EXE)
    create_vault(vd, "passwords", b"vault-pass")
    daemon = d.PwdDaemon.__new__(d.PwdDaemon)
    daemon._unlocked = {}
    daemon._audit = PwdAuditLog(audit_path)
    return daemon, vd, audit_path


def _unlock(daemon, vd):
    key = unlock_vault(vd, "passwords", b"vault-pass")
    daemon._unlocked["passwords"] = {
        "key": bytearray(key), "unlocked_at": 0, "last_use": 0,
    }
    return key


def _add_credential(vd, key, origin, username, password,
                    pin_app_exe=BROWSER_EXE):
    tag = f"pwd:{origin}/{username}"
    add_item(vd, "passwords", key, tag, password.encode("utf-8"),
             pin_app_exe=pin_app_exe, replace=True)


def _call(daemon, method, payload, caller=CALLER):
    with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
         patch("qdistro_pwd_daemon.snapshot_caller", return_value=caller):
        return json.loads(method(json.dumps(payload), sender=":1.42"))


def _rows(daemon, op):
    return [r for r in daemon._audit.tail(50) if r["op"] == op]


def _assert_no_secret_anywhere(daemon, *secrets):
    """Scan EVERY field of EVERY audit row for any secret substring."""
    for r in daemon._audit.tail(100):
        for k, v in r.items():
            if isinstance(v, str):
                for s in secrets:
                    assert s not in v, (
                        f"secret leaked into audit field {k!r}: {v!r}")


# ---------------------------------------------------------------------------
# Schema: the new structured columns exist and survive round-trip.
# ---------------------------------------------------------------------------

class TestSchema:
    def test_columns_present(self, tmp_path):
        log = PwdAuditLog(str(tmp_path / "a.sqlite"))
        log.record("fill", "v", decision="allow", reason="ok",
                   origin="https://x.test", app_id="firefox ext:e@x",
                   app_context="silo-default")
        row = log.tail(1)[0]
        assert row["origin"] == "https://x.test"
        assert row["app_id"] == "firefox ext:e@x"
        assert row["app_context"] == "silo-default"

    def test_additive_migration_on_legacy_db(self, tmp_path):
        """An old DB without the new columns is migrated in place."""
        import sqlite3
        p = str(tmp_path / "legacy.sqlite")
        conn = sqlite3.connect(p)
        conn.executescript(
            "CREATE TABLE pwd_audit ("
            " id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, op TEXT NOT NULL,"
            " vault TEXT NOT NULL, item_tag TEXT, decision TEXT NOT NULL,"
            " reason TEXT NOT NULL, caller_uid INTEGER, caller_pid INTEGER,"
            " caller_exe TEXT, caller_sha TEXT, caller_selinux TEXT,"
            " caller_cgroup TEXT);")
        conn.execute(
            "INSERT INTO pwd_audit (ts, op, vault, decision, reason) "
            "VALUES (1, 'fill', 'v', 'allow', 'legacy-row')")
        conn.commit()
        conn.close()
        # Opening through PwdAuditLog must add the columns and keep working.
        log = PwdAuditLog(p)
        log.record("fill", "v", decision="deny", reason="r",
                   origin="https://x.test", app_id="firefox",
                   app_context=None)
        cols = {c[1] for c in
                log._conn.execute("PRAGMA table_info(pwd_audit)")}
        assert {"origin", "app_id", "app_context"} <= cols
        rows = log.tail(10)
        assert any(r["reason"] == "legacy-row" for r in rows)
        assert any(r["origin"] == "https://x.test" for r in rows)


# ---------------------------------------------------------------------------
# Fill — allow and deny both carry full structured context.
# ---------------------------------------------------------------------------

class TestFillStructuredAudit:
    def test_allow_records_full_context(self, staged):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", SECRET)

        res = _call(daemon, daemon.Fill, {
            "url": "https://example.com/login",
            "extension_id": "addon@firefox",
            "silo": "work",
        })
        assert res["ok"] is True

        row = _rows(daemon, "fill")[0]
        assert row["decision"] == "allow"
        assert row["origin"] == "https://example.com"
        assert "firefox" in row["app_id"]
        assert "ext:addon@firefox" in row["app_id"]
        assert row["app_context"] == "work"
        assert row["reason"].startswith("matched:")

    def test_deny_non_bridge_records_reason(self, staged, monkeypatch):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        monkeypatch.setattr(d, "_browser_bridge_allowed",
                            lambda _pid: (False, "parent-not-browser"))
        res = _call(daemon, daemon.Fill, {"url": "https://example.com/"})
        assert res["ok"] is False

        row = _rows(daemon, "fill")[0]
        assert row["decision"] == "deny"
        assert row["reason"] == "bridge-caller:parent-not-browser"
        # Attested identity captured even on a rejected caller.
        assert row["app_id"] == BROWSER_EXE

    def test_deny_vault_locked_has_origin(self, staged):
        daemon, vd, _ = staged
        # not unlocked
        res = _call(daemon, daemon.Fill, {
            "url": "https://example.com/x", "silo": "personal"})
        assert res["ok"] is False
        row = _rows(daemon, "fill")[0]
        assert row["decision"] == "deny"
        assert row["reason"] == "vault-locked"
        assert row["origin"] == "https://example.com"
        assert row["app_context"] == "personal"

    def test_deny_bad_origin_records_row(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        res = _call(daemon, daemon.Fill, {"url": "data:text/html,hi"})
        assert res["ok"] is False
        row = _rows(daemon, "fill")[0]
        assert row["decision"] == "deny"
        assert row["reason"] == "bad-url-origin"

    def test_deny_bad_port_url_audited_not_crashed(self, staged):
        """A non-numeric / out-of-range port must become an audited
        bad-url-origin deny, not an unhandled ValueError."""
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        for bad in ("https://example.com:bad/x", "https://example.com:99999/"):
            res = _call(daemon, daemon.Fill, {"url": bad})
            assert res["ok"] is False
            assert res["error"] == "invalid_request"
        deny = [r for r in _rows(daemon, "fill")
                if r["reason"] == "bad-url-origin"]
        assert len(deny) == 2

    def test_deny_non_object_json_audited(self, staged):
        """json.loads of a non-object ([], 123, "x", null) must produce an
        audited deny, not an unhandled exception."""
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        for body in ("[]", "123", "\"x\"", "null"):
            with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
                 patch("qdistro_pwd_daemon.snapshot_caller",
                       return_value=CALLER):
                res = json.loads(daemon.Fill(body, sender=":1.42"))
            assert res["ok"] is False
        rows = [r for r in _rows(daemon, "fill")
                if r["reason"] == "non-object-json"]
        assert len(rows) == 4

    def test_deny_malformed_ipv6_url_audited(self, staged):
        """A URL that makes urlparse() itself raise (unterminated IPv6)
        must fail closed to an audited bad-url-origin deny."""
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        for bad in ("http://[::1", "http://[::1]:bad/", "http://[::1]:99999/"):
            res = _call(daemon, daemon.Fill, {"url": bad})
            assert res["ok"] is False
            assert res["error"] == "invalid_request"
        deny = [r for r in _rows(daemon, "fill")
                if r["reason"] == "bad-url-origin"]
        assert len(deny) == 3


# ---------------------------------------------------------------------------
# FillConfirm — allow and deny both audited, no password in the row.
# ---------------------------------------------------------------------------

class TestFillConfirmStructuredAudit:
    def _token(self, daemon, url, username=None):
        res = _call(daemon, daemon.Fill, {"url": url, "username": username})
        assert res["ok"] is True, res
        return res["fill_token"]

    def test_allow_records_context_no_password(self, staged):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", SECRET)
        token = self._token(daemon, "https://example.com/login")

        res = _call(daemon, daemon.FillConfirm, {
            "url": "https://example.com/login",
            "username": "alice",
            "fill_token": token,
            "extension_id": "addon@firefox",
            "silo": "work",
        })
        assert res["ok"] is True
        # Sanity: the secret really was returned to the caller...
        assert res["credentials"][0]["password"] == SECRET

        row = _rows(daemon, "fill-confirm")[0]
        assert row["decision"] == "allow"
        assert row["origin"] == "https://example.com"
        assert "ext:addon@firefox" in row["app_id"]
        assert row["app_context"] == "work"
        assert row["item_tag"] == "pwd:https://example.com/alice"
        # ...but it must NOT be anywhere in the audit log.
        _assert_no_secret_anywhere(daemon, SECRET)

    def test_deny_token_mismatch_audited(self, staged):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", SECRET)
        token = self._token(daemon, "https://example.com/login")
        # Right token, wrong username binding => fill-token-mismatch deny.
        res = _call(daemon, daemon.FillConfirm, {
            "url": "https://example.com/login",
            "username": "alice",
            "fill_token": token + "x",  # corrupt -> invalid/expired
        })
        assert res["ok"] is False
        row = _rows(daemon, "fill-confirm")[0]
        assert row["decision"] == "deny"
        assert row["reason"]  # non-empty denial reason
        assert row["origin"] == "https://example.com"
        _assert_no_secret_anywhere(daemon, SECRET)

    def test_deny_wrong_exe_audited(self, staged):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", SECRET,
                        pin_app_exe=BROWSER_EXE)
        token = self._token(daemon, "https://example.com/")
        wrong = dict(CALLER, exe="/usr/bin/chromium")
        res = _call(daemon, daemon.FillConfirm, {
            "url": "https://example.com/",
            "username": "alice",
            "fill_token": token,
        }, caller=wrong)
        assert res["ok"] is False
        deny_rows = [r for r in _rows(daemon, "fill-confirm")
                     if r["decision"] == "deny"]
        assert deny_rows, "wrong-exe FillConfirm must record a deny row"
        assert deny_rows[0]["reason"]
        _assert_no_secret_anywhere(daemon, SECRET)


# ---------------------------------------------------------------------------
# Save — allow and deny both audited, password never persisted to audit.
# ---------------------------------------------------------------------------

class TestSaveStructuredAudit:
    def test_allow_records_context_no_password(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        res = _call(daemon, daemon.Save, {
            "url": "https://bank.com/login",
            "username": "user@bank.com",
            "password": SECRET,
            "extension_id": "addon@firefox",
            "silo": "finance",
            "parent_exe": BROWSER_EXE,
        })
        assert res["ok"] is True

        row = _rows(daemon, "save")[0]
        assert row["decision"] == "allow"
        assert row["reason"] == "saved"
        assert row["origin"] == "https://bank.com"
        assert "ext:addon@firefox" in row["app_id"]
        assert row["app_context"] == "finance"
        _assert_no_secret_anywhere(daemon, SECRET)

    def test_deny_update_wrong_exe_audited(self, staged):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://bank.com", "user@bank.com",
                        "old-pw", pin_app_exe=BROWSER_EXE)
        wrong = dict(CALLER, exe="/usr/bin/chromium")
        res = _call(daemon, daemon.Save, {
            "url": "https://bank.com/",
            "username": "user@bank.com",
            "password": SECRET,
            "extension_id": "evil@addon",
            "silo": "finance",
        }, caller=wrong)
        assert res["ok"] is False
        assert res["error"] == "policy_denied"

        row = _rows(daemon, "save")[0]
        assert row["decision"] == "deny"
        assert row["reason"]
        assert row["origin"] == "https://bank.com"
        assert "ext:evil@addon" in row["app_id"]
        _assert_no_secret_anywhere(daemon, SECRET)

    def test_deny_missing_fields_audited(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        # password present but username missing -> missing-fields deny.
        res = _call(daemon, daemon.Save, {
            "url": "https://x.test/",
            "password": SECRET,
        })
        assert res["ok"] is False
        row = _rows(daemon, "save")[0]
        assert row["decision"] == "deny"
        assert row["reason"] == "missing-fields"
        _assert_no_secret_anywhere(daemon, SECRET)

    def test_deny_non_object_json_audited(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            res = json.loads(daemon.Save("[1,2,3]", sender=":1.42"))
        assert res["ok"] is False
        assert _rows(daemon, "save")[0]["reason"] == "non-object-json"

    def test_advisory_context_is_length_bounded(self, staged):
        """Request-controlled extension_id / silo are advisory metadata;
        bound their length so a hostile bridge cannot dump a large blob
        (or a multi-line credential paste) into an audit row."""
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        huge = "A" * 5000
        res = _call(daemon, daemon.Save, {
            "url": "https://x.test/",
            "username": "u",
            "password": SECRET,
            "extension_id": huge,
            "silo": huge,
        })
        assert res["ok"] is True
        row = _rows(daemon, "save")[0]
        # app_id = "<attested-exe> ext:<capped>"; the self-reported
        # extension portion is capped at 128 chars.
        ext_part = row["app_id"].split("ext:", 1)[1]
        assert len(ext_part) <= 128
        assert len(row["app_context"]) <= 128
        _assert_no_secret_anywhere(daemon, SECRET)

    def test_advisory_context_password_redacted(self, staged):
        """Belt-and-suspenders: a hostile/buggy bridge that copies the
        submitted password into extension_id / silo / app_context must not
        get it persisted to the audit row (Save is the one path that sees
        the password, so it redacts any advisory value equal to it)."""
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        res = _call(daemon, daemon.Save, {
            "url": "https://x.test/",
            "username": "u",
            "password": SECRET,
            "extension_id": SECRET,      # password laundered into ext id
            "silo": SECRET,              # ...and into silo
            "app_context": SECRET,
        })
        assert res["ok"] is True
        row = _rows(daemon, "save")[0]
        # The advisory fields equal to the password were dropped entirely.
        assert SECRET not in (row["app_id"] or "")
        assert SECRET not in (row["app_context"] or "")
        _assert_no_secret_anywhere(daemon, SECRET)

    def test_advisory_context_password_with_whitespace_redacted(self, staged):
        """Redaction compares the CLEANED value too, so a password wrapped
        in whitespace/control chars (which cleans back to the password)
        is still dropped, not laundered into audit."""
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        res = _call(daemon, daemon.Save, {
            "url": "https://x.test/",
            "username": "u",
            "password": SECRET,
            "extension_id": f"  {SECRET}\n",
            "silo": f"\t{SECRET} ",
        })
        assert res["ok"] is True
        _assert_no_secret_anywhere(daemon, SECRET)

    def test_username_equal_password_redacted_in_item_tag(self, staged):
        """A hostile/buggy bridge that sets username == password must not
        launder the secret through the audited item_tag."""
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        res = _call(daemon, daemon.Save, {
            "url": "https://x.test/",
            "username": SECRET,      # username IS the password
            "password": SECRET,
        })
        assert res["ok"] is True
        row = _rows(daemon, "save")[0]
        assert row["item_tag"] == "pwd:https://x.test/<redacted>"
        _assert_no_secret_anywhere(daemon, SECRET)


# ---------------------------------------------------------------------------
# Global invariant: across a full Save/Fill/FillConfirm lifecycle, no
# credential material appears in ANY audit row, ANY column.
# ---------------------------------------------------------------------------

def test_lifecycle_never_logs_password(staged):
    daemon, vd, _ = staged
    _unlock(daemon, vd)

    save = _call(daemon, daemon.Save, {
        "url": "https://bank.com/login",
        "username": "user@bank.com",
        "password": SECRET,
        "extension_id": "addon@firefox",
        "silo": "finance",
    })
    assert save["ok"] is True

    fill = _call(daemon, daemon.Fill, {"url": "https://bank.com/"})
    assert fill["ok"] is True

    confirm = _call(daemon, daemon.FillConfirm, {
        "url": "https://bank.com/",
        "username": "user@bank.com",
        "fill_token": fill["fill_token"],
    })
    assert confirm["ok"] is True
    assert confirm["credentials"][0]["password"] == SECRET

    _assert_no_secret_anywhere(daemon, SECRET)
    # And no audit column named like a value/payload/password ever exists.
    sample = daemon._audit.tail(1)[0]
    for forbidden in ("password", "payload", "value", "secret"):
        assert forbidden not in sample
