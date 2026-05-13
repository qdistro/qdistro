"""Tests for phone/qdistro_phone — task(117) / spec/18 Phase-8 MVP.

Pure-python: presence smoother is deterministic with injected ts;
the ntfy-message builder is a pure function; verify is HMAC.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

_MOD = (Path(__file__).resolve().parent.parent.parent
        / "phone" / "qdistro_phone.py")
spec = importlib.util.spec_from_file_location(
    "qdistro_phone", _MOD)
ph = importlib.util.module_from_spec(spec)
sys.modules["qdistro_phone"] = ph
spec.loader.exec_module(ph)


# ---- presence ----

class TestPresence:
    def test_initial_unknown(self):
        p = ph.PresenceTracker()
        assert p.state(now=1000.0) == "unknown"

    def test_present_at_strong_rssi(self):
        p = ph.PresenceTracker()
        for i in range(5):
            p.observe(-50, ts=1000.0 + i)
        assert p.state(now=1004.5) == "present"

    def test_unknown_at_weak_rssi(self):
        # Below the -75 default threshold = "weak signal" treated
        # as unknown.
        p = ph.PresenceTracker()
        for i in range(5):
            p.observe(-90, ts=1000.0 + i)
        assert p.state(now=1004.5) == "unknown"

    def test_absent_after_grace(self):
        p = ph.PresenceTracker()
        p.observe(-50, ts=1000.0)
        # absent_grace default 30 s
        assert p.state(now=1031.0) == "absent"

    def test_present_with_dropouts_within_grace(self):
        p = ph.PresenceTracker()
        p.observe(-50, ts=1000.0)
        # 20 s later, still within grace, still good mean rssi
        assert p.state(now=1020.0) == "present"

    def test_window_size_limits_smoothing(self):
        p = ph.PresenceTracker(window=3)
        # only the last 3 readings count
        p.observe(-30, ts=1000.0)
        p.observe(-30, ts=1001.0)
        p.observe(-95, ts=1002.0)
        p.observe(-95, ts=1003.0)
        p.observe(-95, ts=1004.0)
        # last 3 are -95,-95,-95 — mean -95 below threshold
        assert p.state(now=1004.5) == "unknown"

    def test_reset(self):
        p = ph.PresenceTracker()
        p.observe(-40, ts=1000.0)
        p.reset()
        assert p.state(now=1010.0) == "unknown"

    def test_invalid_window(self):
        try:
            ph.PresenceTracker(window=0)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


# ---- ntfy push body ----

class TestApprovalPush:
    def _build(self, **overrides):
        defaults = dict(
            request_id="req-001",
            action_id="com.qdistro.pwd.unlock",
            user="work-user",
            callback_base_url="https://admin.tailnet.ts.net/cb",
            callback_secret=b"shared-secret",
            ttl_seconds=120,
            now_ts=1000.0,
        )
        defaults.update(overrides)
        return ph.build_approval_push(**defaults)

    def test_basic_shape(self):
        body = self._build()
        assert body["topic"] == "qdistro-work-user"
        assert "Approval" in body["title"]
        assert body["x-qdistro-request-id"] == "req-001"
        assert body["x-qdistro-action-id"] == "com.qdistro.pwd.unlock"
        assert body["x-qdistro-expires-at"] == 1120
        assert len(body["actions"]) == 2
        labels = [a["label"] for a in body["actions"]]
        assert labels == ["Approve", "Deny"]

    def test_action_url_signed(self):
        body = self._build()
        approve = body["actions"][0]
        assert approve["url"].startswith(
            "https://admin.tailnet.ts.net/cb/req-001/allow?sig=")
        assert "&exp=1120" in approve["url"]

    def test_callback_signature_verifies(self):
        body = self._build()
        approve = body["actions"][0]
        # extract the sig from the URL
        url = approve["url"]
        sig = url.split("sig=", 1)[1].split("&", 1)[0]
        ok = ph.verify_callback_signature(
            request_id="req-001", decision="allow",
            expires_at=1120, sig=sig,
            callback_secret=b"shared-secret",
            now_ts=1100.0)
        assert ok is True

    def test_callback_signature_rejects_tampered(self):
        # different secret = different sig
        ok = ph.verify_callback_signature(
            request_id="req-001", decision="allow",
            expires_at=1120, sig="x" * 64,
            callback_secret=b"shared-secret",
            now_ts=1100.0)
        assert ok is False

    def test_callback_signature_rejects_expired(self):
        body = self._build()
        approve = body["actions"][0]
        sig = approve["url"].split("sig=", 1)[1].split("&", 1)[0]
        ok = ph.verify_callback_signature(
            request_id="req-001", decision="allow",
            expires_at=1120, sig=sig,
            callback_secret=b"shared-secret",
            now_ts=2000.0)  # past expiry
        assert ok is False

    def test_invalid_decision_rejected(self):
        ok = ph.verify_callback_signature(
            request_id="req-001", decision="approve",  # not in set
            expires_at=1120, sig="abc",
            callback_secret=b"shared-secret")
        assert ok is False

    def test_invalid_input_rejected(self):
        try:
            self._build(request_id="")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
        try:
            self._build(callback_base_url="ftp://x")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")

    def test_min_ttl_enforced(self):
        # 1-second ttl gets bumped to 15 s minimum.
        body = self._build(ttl_seconds=1)
        assert body["x-qdistro-expires-at"] == 1015


# ---- per-phone trust config ----

class TestTrustConfig:
    SAMPLE = """\
# qdistro phone trust config
[pixel-9-pro]
trust = full
presence = on
approver = on
camera = off

[backup-phone]
trust = limited
presence = on
"""

    def test_parse(self):
        out = ph.parse_phone_trust_config(self.SAMPLE)
        assert "pixel-9-pro" in out
        assert "backup-phone" in out
        assert out["pixel-9-pro"]["trust"] == "full"
        assert out["pixel-9-pro"]["camera"] == "off"
        assert out["backup-phone"]["trust"] == "limited"

    def test_invalid_trust_level_dropped(self):
        out = ph.parse_phone_trust_config(
            "[bad]\ntrust = god-mode\npresence = on\n")
        # trust key dropped, presence kept
        assert "trust" not in out["bad"]
        assert out["bad"]["presence"] == "on"

    def test_grants_full(self):
        g = ph.trust_grants("full")
        assert "approver" in g
        assert "camera" in g
        assert "presence" in g

    def test_grants_limited(self):
        g = ph.trust_grants("limited")
        assert g == {"presence"}

    def test_grants_unknown_empty(self):
        assert ph.trust_grants("god") == set()

    def test_empty_input(self):
        assert ph.parse_phone_trust_config("") == {}
        assert ph.parse_phone_trust_config(None) == {}  # type: ignore
