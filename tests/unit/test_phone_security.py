"""Security-focused negative tests for the phone approval path.

Covers the under-tested negative cases called out in
todo/codex-testing/under-tested-areas.md §8: invalid signatures,
replayed decisions, and clock skew.

These complement (do not duplicate) test_phone.py /
test_phone_daemon.py. Where the code does NOT enforce a protection
(future-dated timestamps; replay rejection at the HTTP layer), the
test is clearly labelled and pins the *current* behaviour rather than
asserting a guard that does not exist — see the module docstring notes
on each class.

Module-loading mirrors the existing phone tests so the same on-disk
``phone/qdistro_phone*.py`` is exercised.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PHONE_DIR = REPO_ROOT / "phone"


def _load(label: str, path: Path):
    spec = importlib.util.spec_from_file_location(label, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[label] = mod
    spec.loader.exec_module(mod)
    return mod


ph = _load("qdistro_phone", PHONE_DIR / "qdistro_phone.py")
pd = _load("qdistro_phone_daemon",
           PHONE_DIR / "qdistro_phone_daemon.py")


# ---- helpers -----------------------------------------------------

SECRET = b"unit-test-secret"


def _valid_sig(*, request_id: str, decision: str, expires_at: int,
               secret: bytes = SECRET) -> str:
    """Re-derive a valid sig by extracting it from build_approval_push,
    so the test depends on the production signer rather than re-coding
    the HMAC scheme.
    """
    # ttl is computed from now_ts; pin now_ts so expires_at is exact.
    now_ts = float(expires_at) - 60.0
    body = ph.build_approval_push(
        request_id=request_id,
        action_id="org.qdistro.pwd.unlock",
        user="admin",
        callback_base_url="https://x.example/cb",
        callback_secret=secret,
        ttl_seconds=60,
        now_ts=now_ts,
    )
    assert body["x-qdistro-expires-at"] == expires_at
    label = "Approve" if decision == "allow" else "Deny"
    url = next(a["url"] for a in body["actions"]
               if a["label"] == label)
    return url.split("sig=", 1)[1].split("&", 1)[0]


# ---- invalid signature -------------------------------------------

class TestInvalidSignature:
    """The HMAC must bind request_id, decision and expires_at; a sig
    valid for one tuple must not verify against a different tuple, and
    a sig made with a different key must be rejected.
    """

    def test_payload_tampered_request_id_rejected(self):
        # Sig is valid for req-A/allow/1120; verifier asked about req-B.
        sig = _valid_sig(request_id="req-A", decision="allow",
                         expires_at=1120)
        ok = ph.verify_callback_signature(
            request_id="req-B", decision="allow",
            expires_at=1120, sig=sig,
            callback_secret=SECRET, now_ts=1100.0)
        assert ok is False

    def test_payload_tampered_decision_rejected(self):
        # Sig signed for "deny" presented as "allow" — privilege flip.
        sig = _valid_sig(request_id="req-A", decision="deny",
                         expires_at=1120)
        ok = ph.verify_callback_signature(
            request_id="req-A", decision="allow",
            expires_at=1120, sig=sig,
            callback_secret=SECRET, now_ts=1100.0)
        assert ok is False

    def test_payload_tampered_expires_at_rejected(self):
        # Sig signed for expiry 1120 presented with a stretched expiry.
        sig = _valid_sig(request_id="req-A", decision="allow",
                         expires_at=1120)
        ok = ph.verify_callback_signature(
            request_id="req-A", decision="allow",
            expires_at=999_999, sig=sig,
            callback_secret=SECRET, now_ts=1100.0)
        assert ok is False

    def test_wrong_key_rejected(self):
        # A perfectly-formed sig produced under a different secret.
        sig = _valid_sig(request_id="req-A", decision="allow",
                         expires_at=1120, secret=b"attacker-secret")
        ok = ph.verify_callback_signature(
            request_id="req-A", decision="allow",
            expires_at=1120, sig=sig,
            callback_secret=SECRET, now_ts=1100.0)
        assert ok is False

    def test_empty_sig_rejected(self):
        ok = ph.verify_callback_signature(
            request_id="req-A", decision="allow",
            expires_at=1120, sig="",
            callback_secret=SECRET, now_ts=1100.0)
        assert ok is False

    def test_empty_secret_rejected(self):
        sig = _valid_sig(request_id="req-A", decision="allow",
                         expires_at=1120)
        ok = ph.verify_callback_signature(
            request_id="req-A", decision="allow",
            expires_at=1120, sig=sig,
            callback_secret=b"", now_ts=1100.0)
        assert ok is False


# ---- clock skew / expiry -----------------------------------------

class TestClockSkew:
    """verify_callback_signature checks expiry as ``expires_at < now``.

    What the code enforces:
      * a decision whose expires_at is in the past is rejected (already
        covered by test_phone.test_callback_signature_rejects_expired;
        here we pin the exact boundary).

    What the code does NOT enforce (pinned, clearly labelled):
      * there is no lower bound / max-future-skew check, so a sig whose
        expires_at sits absurdly far in the future still verifies. A
        compromised signer could mint a near-immortal token. See the
        FINDING note in the test below.
    """

    def test_expiry_boundary_now_equals_expires_accepts(self):
        # int(expires_at) < int(now) is the reject condition, so
        # now == expires_at is still inside the window (accepted).
        sig = _valid_sig(request_id="req-A", decision="allow",
                         expires_at=1120)
        ok = ph.verify_callback_signature(
            request_id="req-A", decision="allow",
            expires_at=1120, sig=sig,
            callback_secret=SECRET, now_ts=1120.0)
        assert ok is True

    def test_expiry_one_second_past_rejected(self):
        sig = _valid_sig(request_id="req-A", decision="allow",
                         expires_at=1120)
        ok = ph.verify_callback_signature(
            request_id="req-A", decision="allow",
            expires_at=1120, sig=sig,
            callback_secret=SECRET, now_ts=1121.0)
        assert ok is False

    def test_far_future_expiry_NOT_rejected_current_behavior(self):
        # FINDING / pinned current behaviour: there is no max-future
        # skew guard. A token expiring in the year ~2065 verifies fine
        # today, so a leaked/forged-by-the-signer far-future token is
        # effectively long-lived. This asserts the CURRENT (unguarded)
        # behaviour, not a desired one.
        far = 3_000_000_000  # ~year 2065
        sig = _valid_sig(request_id="req-A", decision="allow",
                         expires_at=far)
        ok = ph.verify_callback_signature(
            request_id="req-A", decision="allow",
            expires_at=far, sig=sig,
            callback_secret=SECRET, now_ts=1000.0)
        assert ok is True


# ---- replay ------------------------------------------------------

class TestReplayQueueLayer:
    """Replay protection is NOT in verify_callback_signature (a valid
    sig verifies arbitrarily many times). The only one-shot guard is
    the queue's ``UNIQUE(request_id, decision)`` constraint, where
    record_decision returns -1 on the duplicate INSERT.
    """

    def test_signature_alone_is_not_replay_protected(self):
        # Pinned: the verifier has no nonce/seen-set, so the same sig
        # passes repeatedly. Replay defence lives at the queue, not
        # here.
        sig = _valid_sig(request_id="req-A", decision="allow",
                         expires_at=1120)
        kw = dict(request_id="req-A", decision="allow",
                  expires_at=1120, sig=sig,
                  callback_secret=SECRET, now_ts=1100.0)
        assert ph.verify_callback_signature(**kw) is True
        assert ph.verify_callback_signature(**kw) is True  # again

    # NB: the plain (request_id, decision) dedup -> -1 case is already
    # covered by test_phone_daemon.TestQueue.test_record_and_dedup, so
    # it is intentionally not repeated here. The replay angles unique to
    # this file are the verifier-has-no-guard pin (above), the
    # distinct-decision sub-case (below), and the end-to-end HTTP replay
    # in TestReplayHttpLayer.

    def test_queue_allows_distinct_decision_same_request(self, tmp_path):
        # The dedup key is (request_id, decision): an allow then a deny
        # for the same request both insert. Pinned so a future change
        # to the uniqueness key is caught.
        conn = pd.open_queue(str(tmp_path / "decisions.sqlite"))
        a = pd.record_decision(
            conn, request_id="req-A", decision="allow",
            expires_at=1120)
        d = pd.record_decision(
            conn, request_id="req-A", decision="deny",
            expires_at=1120)
        assert a >= 1 and d >= 1 and a != d


class TestReplayHttpLayer:
    """End-to-end replay against the live HTTP handler.

    FINDING / pinned behaviour: a replayed callback is de-duplicated at
    the queue (row_id -1) but the handler still returns HTTP 200 — it
    does NOT signal the replay to the caller with a 4xx. The decision
    is correctly not double-recorded, but the phone/ntfy side cannot
    tell an accepted decision from a swallowed replay. This asserts the
    CURRENT behaviour.
    """

    def _start(self, tmp_path, secret: bytes):
        srv = pd.build_server(
            host="127.0.0.1", port=0,
            callback_secret=secret,
            queue_path=str(tmp_path / "decisions.sqlite"))
        host, port = srv.server_address
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv, host, port

    def _post(self, host, port, path_q):
        conn = http.client.HTTPConnection(host, port)
        conn.request("POST", path_q)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, json.loads(body)

    def test_replayed_callback_not_double_recorded(self, tmp_path):
        srv, host, port = self._start(tmp_path, SECRET)
        try:
            push = ph.build_approval_push(
                request_id="rep-1",
                action_id="org.qdistro.pwd.unlock",
                user="admin",
                callback_base_url=f"http://{host}:{port}/v1/decision",
                callback_secret=SECRET,
                ttl_seconds=600)
            allow_url = next(a["url"] for a in push["actions"]
                             if a["label"] == "Approve")
            u = urlparse(allow_url)
            path_q = f"{u.path}?{u.query}"

            s1, d1 = self._post(host, port, path_q)
            assert s1 == 200
            assert d1["ok"] is True
            first_row = d1["row_id"]
            assert first_row >= 1

            # Replay the identical signed callback.
            s2, d2 = self._post(host, port, path_q)
            # Pinned current behaviour: still 200, but row_id == -1
            # (dedup) — NOT double-recorded, but no replay signal.
            assert s2 == 200
            assert d2["ok"] is True
            assert d2["row_id"] == -1
        finally:
            srv.shutdown()
            srv.server_close()

    def test_expired_callback_403_at_http(self, tmp_path):
        # A correctly-signed but already-expired callback is refused by
        # the handler (verify returns False -> 403). Complements the
        # verifier-level expiry test with an end-to-end assertion.
        srv, host, port = self._start(tmp_path, SECRET)
        try:
            sig = _valid_sig(request_id="exp-1", decision="allow",
                             expires_at=1120)
            path_q = f"/v1/decision/exp-1/allow?sig={sig}&exp=1120"
            conn = http.client.HTTPConnection(host, port)
            conn.request("POST", path_q)
            resp = conn.getresponse()
            resp.read()
            # exp=1120 is far in the past relative to real wall-clock,
            # so the handler (which uses real time.time()) rejects it.
            assert resp.status == 403
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()
