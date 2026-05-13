"""qdistro-phone — Phase-8 MVP skeleton (host side).

Per doc/phone.md §"Phase-8 MVP scope". Spec-rewrite
shipped this session pivots away from the original Kotlin Android
app: v1 reuses KDE Connect (file send / clipboard / media) +
UnifiedPush via self-hosted ntfy (admin-approver push) +
Microsoft RD Client (window projection) + Aegis-or-similar
RFC-6238 TOTP.

This module ships the smallest pure-python wrappers covering the
two host-side primitives the daemon needs:

1. **Presence** — smoothed BLE-RSSI readings of a paired phone
   advertising a known service UUID (or matching MAC). The
   smoother is the load-bearing piece per the feasibility
   research: raw RSSI is too noisy to drive auto-lock directly,
   and BlueZ's BLE D-Bus surface only exposes RSSI on
   advertisements (not while connected). Smoothing window
   defaults to 10 readings; absent-grace defaults to 30 s.

2. **Push** — admin-approver request packaged as a UnifiedPush /
   ntfy message body. Includes an HMAC-signed callback URL the
   phone hits to record the decision. We don't open sockets
   here — the function returns the JSON body the daemon's HTTP
   client posts to ntfy.

Out of scope (Phase-9 / v2 Kotlin app): camera streaming, RDP
deep-link generation, KDE-Connect bridge, Tailscale LocalAPI
peer-enumeration, signed-response binding to Android Keystore.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import deque
from typing import Iterable

# Default service UUID a paired qdistro-companion (or a KDE Connect
# Android instance with bluetooth backend on) advertises.
DEFAULT_BLE_SERVICE_UUID = (
    "1864198a-3a83-4d9d-b5f2-2e5f4b9c1c3e"
)


# ---- presence smoother ------------------------------------------

class PresenceTracker:
    """Smooths BLE-RSSI advertisements + reports a presence state.

    States: 'present' | 'absent' | 'unknown' (until first sample).
    Caller calls observe() each time BlueZ surfaces an RSSI; calls
    state(now) to read the current decision.

    - "absent" requires no observation for `absent_grace` seconds
      (default 30, matching spec/18 §"Presence detection").
    - "present" requires the moving-window mean RSSI to be ≥
      `present_threshold` (default -75 dBm); below that we treat
      as "weak signal, treat as unknown" so a phone ten metres
      away through a wall doesn't wedge the desktop unlocked.
    - "fail = unknown, not absent" (spec rule): if BlueZ stack
      itself is unreachable, the daemon never calls observe();
      state() returns 'unknown' indefinitely + the lock policy
      decides whether unknown means lock or continue.
    """

    def __init__(
            self,
            *,
            window: int = 10,
            absent_grace: float = 30.0,
            present_threshold: int = -75,
    ):
        if window < 1:
            raise ValueError("window must be >= 1")
        self._window = window
        self._absent_grace = float(absent_grace)
        self._present_threshold = int(present_threshold)
        self._samples: deque[tuple[float, int]] = deque(maxlen=window)
        self._last_seen: float | None = None

    @property
    def absent_grace(self) -> float:
        return self._absent_grace

    @property
    def present_threshold(self) -> int:
        return self._present_threshold

    def observe(self, rssi: int, ts: float | None = None) -> None:
        """Record one RSSI reading."""
        t = float(ts if ts is not None else time.time())
        self._samples.append((t, int(rssi)))
        self._last_seen = t

    def mean_rssi(self) -> float | None:
        if not self._samples:
            return None
        return sum(s[1] for s in self._samples) / len(self._samples)

    def state(self, now: float | None = None) -> str:
        """Return 'present' | 'absent' | 'unknown'."""
        if self._last_seen is None:
            return "unknown"
        n = float(now if now is not None else time.time())
        if (n - self._last_seen) > self._absent_grace:
            return "absent"
        m = self.mean_rssi()
        if m is None:
            return "unknown"
        return "present" if m >= self._present_threshold else "unknown"

    def reset(self) -> None:
        self._samples.clear()
        self._last_seen = None


# ---- ntfy / UnifiedPush message builder -------------------------

def _shorten(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def build_approval_push(
        *,
        request_id: str,
        action_id: str,
        user: str,
        title: str | None = None,
        body: str | None = None,
        callback_base_url: str,
        callback_secret: bytes,
        ttl_seconds: int = 120,
        now_ts: float | None = None,
) -> dict:
    """Build a JSON body suitable for POST to ntfy.

    The phone receives this via UnifiedPush + ntfy distributor;
    the action buttons fire HTTPS callbacks to
    ``<callback_base_url>/<request_id>/<decision>?sig=<hmac>``.

    HMAC is over ``b"<request_id>|<decision>|<expires_at>"`` keyed
    by ``callback_secret``. The expires_at value is included in the
    URL so the phone-side click can race against TTL truthfully.
    """
    if not request_id or not action_id:
        raise ValueError("request_id and action_id are required")
    if not callback_base_url.startswith(("http://", "https://")):
        raise ValueError("callback_base_url must be http(s)")
    if not callback_secret:
        raise ValueError("callback_secret must be non-empty bytes")
    now = float(now_ts if now_ts is not None else time.time())
    expires_at = int(now + max(15, int(ttl_seconds)))

    def _sig(decision: str) -> str:
        msg = f"{request_id}|{decision}|{expires_at}".encode()
        return hmac.new(callback_secret, msg,
                        hashlib.sha256).hexdigest()

    base = callback_base_url.rstrip("/")
    actions = [
        {
            "action": "http",
            "label": "Approve",
            "url": (f"{base}/{request_id}/allow"
                    f"?sig={_sig('allow')}&exp={expires_at}"),
            "method": "POST",
            "clear": True,
        },
        {
            "action": "http",
            "label": "Deny",
            "url": (f"{base}/{request_id}/deny"
                    f"?sig={_sig('deny')}&exp={expires_at}"),
            "method": "POST",
            "clear": True,
        },
    ]
    return {
        "topic": f"qdistro-{user}",
        "title": _shorten(title or f"Approval: {action_id}", 120),
        "message": _shorten(
            body or f"polkit action {action_id} on user {user}", 400),
        "tags": ["lock", "qdistro"],
        "priority": 4,
        "actions": actions,
        "x-qdistro-request-id": request_id,
        "x-qdistro-action-id": action_id,
        "x-qdistro-expires-at": expires_at,
    }


def verify_callback_signature(
        *, request_id: str, decision: str, expires_at: int,
        sig: str, callback_secret: bytes,
        now_ts: float | None = None,
) -> bool:
    """Verify the HMAC-signed approve/deny callback the phone fires.

    Returns False on signature mismatch, expiry, or any malformed
    input — the daemon refuses the action on False, no exception
    surfaced.
    """
    if not all((request_id, decision, sig, callback_secret)):
        return False
    if decision not in ("allow", "deny"):
        return False
    now = float(now_ts if now_ts is not None else time.time())
    if int(expires_at) < int(now):
        return False
    msg = f"{request_id}|{decision}|{int(expires_at)}".encode()
    expected = hmac.new(callback_secret, msg,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


# ---- per-phone trust scopes --------------------------------------

VALID_TRUST_LEVELS: tuple[str, ...] = ("limited", "trusted", "full")


def parse_phone_trust_config(text: str) -> dict[str, dict]:
    """Parse a per-phone trust config — one section per phone.

    Format::

        [pixel-9-pro]
        trust = full
        presence = on
        approver = on
        camera = off

        [backup-phone]
        trust = limited
        presence = on

    Returns ``{phone_id: {trust, presence, approver, camera, ...}}``.
    Unknown keys are kept as strings; the daemon decides.
    """
    out: dict[str, dict] = {}
    cur: str | None = None
    cur_body: dict | None = None
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.startswith("[") and ln.endswith("]"):
            if cur and cur_body is not None:
                out[cur] = cur_body
            cur = ln[1:-1].strip()
            cur_body = {}
            continue
        if "=" not in ln or cur is None or cur_body is None:
            continue
        k, v = ln.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k == "trust":
            v = v.lower()
            if v not in VALID_TRUST_LEVELS:
                continue
        elif v.lower() in ("on", "true", "yes"):
            v = "on"
        elif v.lower() in ("off", "false", "no"):
            v = "off"
        cur_body[k] = v
    if cur and cur_body is not None:
        out[cur] = cur_body
    return out


def trust_grants(level: str) -> set[str]:
    """Return the set of feature-strings a trust level enables."""
    if level == "full":
        return {"presence", "totp", "approver",
                "projection", "camera"}
    if level == "trusted":
        return {"presence", "totp", "approver"}
    if level == "limited":
        return {"presence"}
    return set()
