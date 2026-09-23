"""pwd autofill plugin — wire qdbrowser to the qdistro pwd vault.

Phase-C of plan2/tasks/P04-browser-integration.md.

Flow (high level):

  1. A password field in any tab fires a "focus" event (via a content
     script injected by the WebView). The plugin's
     :meth:`request_fill` is called with the page URL + caller field id.
  2. The plugin mints an intent token (HMAC against the per-session
     secret it fetched during the bridge handshake), then calls
     ``pwd.fill`` on the qdbrowser browser_bridge.
  3. The bridge forwards to ``org.qdistro.Pwd1.Fill`` via D-Bus.
  4. The pwd daemon either returns a candidate credential set OR
     reports ``vault_locked`` — in which case it has already
     triggered the polkit unlock prompt.
  5. The plugin asks the compositor-popup service to render the
     "Autofill <site>?" admin prompt (NOT an in-page DOM element —
     password-manager.md §"Delivery mechanism" forbids that).
  6. On admin approve → plugin pushes the credential into the field
     via ``runJavaScript``. On admin deny → plugin tells the
     content script "no credentials"; the page sees an empty fill
     and the content script surfaces a small toast.

Compositor popup: in production we route through
``org.qdistro.Compositor1.PromptAutofill`` (see browser_bridge.py
§ ``_handle_screenlock_inhibit``-style forward). The plugin defers
the actual D-Bus call through an injectable
:class:`AutofillPromptClient` so unit tests cover the deny + allow
branches without a live compositor.

Cross-references:
  - plan2/research/browser-compositor-autofill-popup.md — open
    question: which exact wp_security_context_v1 toplevel will the
    compositor parent the prompt to? Today the prompt is parented
    to the qdshell admin layer; longer-term it should attach to the
    qdbrowser toplevel so a user can see "this site asked for fill"
    without context-switching.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger("qdbrowser.pwd_autofill")


# ---------------------------------------------------------------------------
# Constants — CANONICAL bus names. Three review angles flagged the
# bus-name drift between qdbrowser, browser_bridge, and the pwd daemon;
# the values below are the single source of truth (any consumer that
# needs one must import from here).
#
#   bridge:     ``org.qdistro.BrowserBridge.<ppid>`` (SESSION)
#   pwd:        ``org.qdistro.Pwd1`` (SYSTEM)         — daemon canonical
#   compositor: ``org.qdistro.Compositor1`` (SESSION) — popup
# ---------------------------------------------------------------------------

BRIDGE_BUS_PREFIX = "org.qdistro.BrowserBridge."
BRIDGE_OBJ_PATH = "/org/qdistro/BrowserBridge"
BRIDGE_IFACE = "org.qdistro.BrowserBridge"

PWD_BUS = "org.qdistro.Pwd1"
PWD_OBJ_PATH = "/org/qdistro/Pwd1"
PWD_IFACE = "org.qdistro.Pwd1"

# Compositor popup interface. ``Compositor1`` is the qdshell-side
# autofill prompt; the well-known name lives on SESSION. P04 lands the
# orchestrator-side caller; the real popup endpoint is tracked in
# plan2/research/browser-compositor-autofill-popup.md.
COMPOSITOR_BUS = "org.qdistro.Compositor1"
COMPOSITOR_OBJ_PATH = "/org/qdistro/Compositor1"
COMPOSITOR_IFACE = "org.qdistro.Compositor1"

INTENT_TOKEN_TTL_S = 5.0


def select_bridge_names(names: list[str]) -> list[tuple[int, str]]:
    """Filter session-bus names to legit bridge instances + sort by ppid.

    Mirrors :func:`qdistro_browser_bridge_client._select_bridges_by_ppid`
    — the suffix after :data:`BRIDGE_BUS_PREFIX` must be all-digits OR a
    ``p<digits>`` form (D-Bus name elements can't start with a digit,
    so spawners prepend a ``p`` to the ppid). Either form is accepted;
    a same-uid attacker that claims ``org.qdistro.BrowserBridge.evil``
    is filtered out (P04 H1 security review). Defined here so both
    :class:`JeepneyBridgeClient` and any future consumer call ONE
    selection routine — drift between the two filters previously
    allowed a spoofed claim to win on the autofill path.
    """
    out: list[tuple[int, str]] = []
    for n in names:
        if not isinstance(n, str):
            continue
        if not n.startswith(BRIDGE_BUS_PREFIX):
            continue
        if n.startswith(":"):
            continue
        suffix = n[len(BRIDGE_BUS_PREFIX):]
        if suffix.isdigit():
            ppid_int = int(suffix)
        elif (len(suffix) >= 2 and suffix[0] == "p"
              and suffix[1:].isdigit()):
            ppid_int = int(suffix[1:])
        else:
            continue
        out.append((ppid_int, n))
    out.sort(key=lambda t: t[0])
    return out


# ---------------------------------------------------------------------------
# Errors surfaced to the caller / extension
# ---------------------------------------------------------------------------

class AutofillError(Exception):
    """Base error for the autofill plugin."""


class AutofillDenied(AutofillError):
    """Admin rejected the autofill prompt."""


class AutofillVaultLocked(AutofillError):
    """Vault is locked and polkit-unlock failed or was cancelled."""


class AutofillNoMatch(AutofillError):
    """No credential matched the URL — falls back to no-op fill."""


# ---------------------------------------------------------------------------
# Intent token (mirrors browser_bridge._compute_token_hmac)
# ---------------------------------------------------------------------------

@dataclass
class IntentToken:
    request_id: str
    ts: float
    op: str
    hmac_hex: str

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "ts": self.ts,
            "op": self.op,
            "hmac": self.hmac_hex,
        }


def mint_intent_token(secret: bytes, op: str,
                      now_fn: Callable[[], float] = time.time
                      ) -> IntentToken:
    """Build an intent token whose HMAC matches the bridge's verify path.

    The canonical message is ``"<request_id>|<ts>|<op>"`` and the MAC is
    SHA-256 against ``secret``. The bridge sweeps an in-memory replay
    map keyed by ``request_id``, so we need only ensure uniqueness
    within the 5-second TTL window — 16 random hex bytes is more than
    enough.
    """
    request_id = secrets.token_hex(16)
    ts = now_fn()
    canonical = f"{request_id}|{ts}|{op}".encode()
    mac = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    return IntentToken(request_id=request_id, ts=ts, op=op, hmac_hex=mac)


# ---------------------------------------------------------------------------
# Compositor-popup client (injectable for tests)
# ---------------------------------------------------------------------------

@dataclass
class AutofillPrompt:
    """The content of an autofill prompt rendered by the compositor.

    The compositor displays site + candidate username + an
    allow/deny pair. The selected username (when the user picks one
    from a 1-of-N drop-down) is returned in :class:`AutofillDecision`.
    """
    url: str
    candidate_usernames: tuple[str, ...]
    silo: str


@dataclass
class AutofillDecision:
    allow: bool
    selected_username: str | None = None
    reason: str = ""


class AutofillPromptClient:
    """Interface for asking the compositor to render an autofill prompt.

    Production wires this to ``org.qdistro.Compositor1.PromptAutofill``
    via jeepney. Tests inject a fake that records calls + answers
    synchronously. The method is **blocking** — the caller (the
    plugin's request_fill) parks on the response.

    Justification: even with a polkit-style async surface, the password
    field can't be filled until the user has decided. A synchronous
    block here keeps the dispatch single-threaded; the Qt event loop
    keeps pumping because the prompt runs in qdshell, not in
    qdbrowser's process.
    """

    def prompt(self, payload: AutofillPrompt) -> AutofillDecision:
        raise NotImplementedError


class JeepneyAutofillPromptClient(AutofillPromptClient):
    """jeepney-backed production client.

    Routes through ``org.qdistro.Compositor1.PromptAutofill``. The
    compositor is responsible for rendering the dialog as a floating
    surface, NOT a wp_layer_surface_v1 toplevel that a malicious page
    could fake (see password-manager.md §"Render path").

    On any failure (jeepney missing, compositor absent, RPC timeout)
    the result is ``allow=False, reason="prompt_unreachable"`` —
    fail-closed by design.
    """

    def __init__(self, timeout_s: float = 15.0):
        # 15s is the longest a user reasonably waits at an admin
        # prompt; longer windows compound with the bridge-side
        # intent-token TTL (5s) and let a stale fill chain sit
        # parked. The previous 60s default was unbounded enough that
        # M3 review flagged it as a thread-pool starvation risk.
        self._timeout_s = float(timeout_s)

    def prompt(self, payload: AutofillPrompt) -> AutofillDecision:
        try:
            from jeepney import DBusAddress, new_method_call
            from jeepney.io.blocking import open_dbus_connection
        except ImportError:
            return AutofillDecision(
                allow=False, reason="jeepney_missing")
        body_json = json.dumps({
            "url": payload.url,
            "candidate_usernames": list(payload.candidate_usernames),
            "silo": payload.silo,
        })
        try:
            conn = open_dbus_connection(bus="SESSION")
        except Exception as exc:
            log.warning("compositor prompt: SESSION bus unavailable: %s",
                        exc)
            return AutofillDecision(
                allow=False, reason="session_bus_unreachable")
        try:
            addr = DBusAddress(
                COMPOSITOR_OBJ_PATH,
                bus_name=COMPOSITOR_BUS,
                interface=COMPOSITOR_IFACE,
            )
            msg = new_method_call(addr, "PromptAutofill", "s",
                                  (body_json,))
            try:
                reply = conn.send_and_get_reply(
                    msg, timeout=self._timeout_s)
            except Exception as exc:
                log.warning("compositor PromptAutofill failed: %s", exc)
                return AutofillDecision(
                    allow=False, reason="prompt_unreachable")
            if reply.header.message_type.name == "ERROR":
                return AutofillDecision(
                    allow=False, reason="prompt_error")
            try:
                body = (reply.body[0]
                        if reply.body
                        and isinstance(reply.body[0], str)
                        else "{}")
                obj = json.loads(body)
            except Exception:
                return AutofillDecision(
                    allow=False, reason="prompt_bad_reply")
            return AutofillDecision(
                allow=bool(obj.get("allow", False)),
                selected_username=obj.get("username"),
                reason=str(obj.get("reason") or ""),
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Bridge client (injectable for tests)
# ---------------------------------------------------------------------------

class BridgeClient:
    """Surface qdbrowser uses to call the local bridge daemon.

    Production goes through
    :func:`qdistro_browser_bridge_client.call_bridge` which speaks
    ``org.qdistro.BrowserBridge.<ppid>.RequestTabs(op, args_json)``.
    For autofill we keep the same surface so the bridge's
    intent-token verifier sees a regular ``pwd.fill`` op.
    """

    def call(self, op: str, args: dict) -> dict:
        raise NotImplementedError


class JeepneyBridgeClient(BridgeClient):
    """Production client. ppid is the bridge's parent — admin can
    override via ``QDISTRO_BROWSER_BRIDGE_PPID`` for the dev path
    where the bridge is launched standalone."""

    def __init__(self, ppid: int | None = None,
                 timeout_s: float = 10.0):
        self._ppid = ppid
        self._timeout_s = float(timeout_s)

    def _resolve_ppid(self) -> int:
        if self._ppid is not None:
            return int(self._ppid)
        # ``QDISTRO_BROWSER_BRIDGE_PPID`` is a debug knob that only
        # honors itself when explicitly opted into via ``QDISTRO_DEBUG``;
        # otherwise a parent that controls qdbrowser's environment
        # could redirect autofill traffic to an attacker-chosen ppid
        # (L2 review).
        if os.environ.get("QDISTRO_DEBUG", "").strip() == "1":
            env = os.environ.get(
                "QDISTRO_BROWSER_BRIDGE_PPID", "").strip()
            if env.isdigit():
                return int(env)
        return 0

    def call(self, op: str, args: dict) -> dict:
        try:
            from jeepney import DBusAddress, new_method_call
            from jeepney.bus_messages import message_bus
            from jeepney.io.blocking import open_dbus_connection
        except ImportError:
            return {"ok": False, "error": "jeepney_missing"}
        try:
            conn = open_dbus_connection(bus="SESSION")
        except Exception as exc:
            return {"ok": False, "error": "session_bus_unreachable",
                    "detail": str(exc)[:200]}
        try:
            # Resolve the bridge bus name. Prefer an explicit ppid,
            # else scan bus names for the BrowserBridge prefix.
            # The selection routine pins all-digits suffix, sorts by
            # ppid, picks the lowest — same gate as
            # qdistro_browser_bridge_client._select_bridges_by_ppid
            # so a same-uid impostor with a non-numeric suffix is
            # filtered out (P04 H1 security review).
            target = ""
            ppid = self._resolve_ppid()
            if ppid:
                target = f"{BRIDGE_BUS_PREFIX}{ppid}"
            else:
                try:
                    reply = conn.send_and_get_reply(
                        message_bus.ListNames(), timeout=2.0)
                    names = list(reply.body[0]) if reply.body else []
                    bridges = select_bridge_names(names)
                    if bridges:
                        target = bridges[0][1]
                except Exception as exc:
                    return {"ok": False, "error": "bridge_not_found",
                            "detail": str(exc)[:200]}
            if not target:
                return {"ok": False, "error": "bridge_not_found"}
            addr = DBusAddress(
                BRIDGE_OBJ_PATH,
                bus_name=target,
                interface=BRIDGE_IFACE,
            )
            msg = new_method_call(addr, "RequestTabs", "ss",
                                  (op, json.dumps(args)))
            try:
                reply = conn.send_and_get_reply(
                    msg, timeout=self._timeout_s)
            except Exception as exc:
                return {"ok": False, "error": "bridge_call_failed",
                        "detail": str(exc)[:200]}
            if reply.header.message_type.name == "ERROR":
                return {"ok": False, "error": "bridge_error",
                        "detail": str(reply.body)[:200]}
            if reply.body and isinstance(reply.body[0], str):
                try:
                    return json.loads(reply.body[0])
                except json.JSONDecodeError:
                    return {"ok": False, "error": "bridge_bad_reply"}
            return {"ok": False, "error": "bridge_empty_reply"}
        finally:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Pwd autofill orchestrator
# ---------------------------------------------------------------------------

@dataclass
class FillResult:
    ok: bool
    username: str = ""
    password: str = ""
    error: str = ""

    def to_extension_reply(self) -> dict:
        """Shape the extension sees on the native-messaging port.

        Deny / no-match / locked all collapse to ``{ok: False,
        error: <code>}`` so the content script's UI surface only
        needs a single error branch (browser.md §"deny UX").
        """
        if self.ok:
            return {"ok": True, "username": self.username,
                    "password": self.password}
        return {"ok": False, "error": self.error or "autofill_failed"}


def _sanitize_for_prompt(s: str) -> str:
    """Drop Unicode bidi-override characters before the prompt
    renders. A malicious page can navigate to a crafted URL whose
    netloc contains an RTL override; without scrubbing, the
    compositor's prompt body can paint a misleading site label
    (S8 review).
    """
    bad = {"‪", "‫", "‬", "‭", "‮",
           "⁦", "⁧", "⁨", "⁩"}
    return "".join(c for c in s if c not in bad)


@dataclass
class AutofillOrchestrator:
    """Drives the pwd.fill round-trip + compositor popup.

    Both client surfaces are injected so unit tests cover every branch
    without a real bridge / compositor. The session secret is set via
    :meth:`set_session_secret` after the qdistro.handshake completes.

    Re-entrancy: ``fill()`` is serialised by an internal lock so two
    concurrent fills against the same orchestrator can't queue
    overlapping compositor prompts (M4 review). If a second fill comes
    in while the first is parked at the compositor, the second
    returns ``ok=False, error="busy"``.

    Stale-secret recovery: when the bridge replies
    ``intent_token_bad_hmac`` (the bridge process restarted and
    rotated its session secret), the orchestrator drops the cached
    secret, re-handshakes exactly once, retries the fill exactly once,
    then surfaces the result. This avoids a permanent broken state
    after a bridge crash (H2 correctness).
    """

    bridge: BridgeClient
    prompt: AutofillPromptClient
    silo: str | None = None
    _session_secret: bytes | None = field(default=None, repr=False)
    _fill_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.silo is None:
            # Lazy-import to avoid a circular module-load: the
            # clipboard_silo helper depends on env only.
            try:
                from qdbrowser.clipboard_silo import current_silo
                self.silo = current_silo() or "user"
            except Exception:  # noqa: BLE001
                self.silo = "user"

    def set_session_secret(self, secret_hex: str) -> None:
        if not isinstance(secret_hex, str) or not secret_hex:
            self._session_secret = None
            return
        try:
            self._session_secret = bytes.fromhex(secret_hex)
        except ValueError:
            self._session_secret = None

    def has_session(self) -> bool:
        return self._session_secret is not None

    def _do_fill(self, url: str, username: str | None
                 ) -> FillResult:
        """Single attempt: mint → bridge → prompt → result. Does not
        re-handshake on bad HMAC; the public :meth:`fill` does that
        once and retries.
        """
        token = mint_intent_token(self._session_secret, "pwd.fill")
        args = {
            "url": url,
            "username": username,
            "intent_token": token.to_dict(),
        }
        reply = self.bridge.call("pwd.fill", args)
        if not reply.get("ok"):
            err = reply.get("error", "bridge_error")
            if err == "vault_locked":
                return FillResult(ok=False, error="vault_locked")
            return FillResult(ok=False, error=err)
        credentials = reply.get("credentials") or []
        fill_token = reply.get("fill_token") or ""
        if not credentials:
            return FillResult(ok=False, error="no_match")
        candidate_usernames = tuple(
            str(c.get("username", "")) for c in credentials
            if c.get("username"))
        decision = self.prompt.prompt(AutofillPrompt(
            url=_sanitize_for_prompt(url),
            candidate_usernames=candidate_usernames,
            silo=self.silo or "user",
        ))
        if not decision.allow:
            return FillResult(ok=False, error="autofill_denied")
        chosen = None
        if decision.selected_username:
            if decision.selected_username not in candidate_usernames:
                # A buggy or compromised compositor returned a
                # username that wasn't on the candidate list — refuse
                # rather than silently falling back to credentials[0]
                # (S4 correctness review).
                return FillResult(
                    ok=False, error="bad_username_selection")
            chosen = next(
                (c for c in credentials
                 if (str(c.get("username", ""))
                     == decision.selected_username)),
                None)
        if chosen is None:
            chosen = credentials[0]
        selected_username = str(chosen.get("username", ""))
        confirm_token = mint_intent_token(
            self._session_secret, "pwd.fill_confirm")
        confirm_args = {
            "url": url,
            "username": selected_username,
            "fill_token": fill_token,
            "intent_token": confirm_token.to_dict(),
        }
        confirm_reply = self.bridge.call("pwd.fill_confirm", confirm_args)
        if not confirm_reply.get("ok"):
            return FillResult(ok=False,
                              error=confirm_reply.get("error", "confirm_error"))
        confirmed_creds = confirm_reply.get("credentials") or []
        if not confirmed_creds:
            return FillResult(ok=False, error="no_match")
        confirmed = confirmed_creds[0]
        confirmed_username = str(confirmed.get("username", ""))
        if confirmed_username != selected_username:
            return FillResult(ok=False, error="bad_confirm_username")
        return FillResult(
            ok=True,
            username=selected_username,
            password=str(confirmed.get("password", "")),
        )

    def fill(self, url: str, *, username: str | None = None,
             ) -> FillResult:
        """Run the pwd.fill round-trip end-to-end.

        Returns a :class:`FillResult` whose ``to_extension_reply()``
        the caller forwards back to the WebExtension (or to the
        in-process content-script bridge).

        Steps:

          1. Without a session secret → fail with
             ``no_session``. Caller must handshake first.
          2. Mint an intent token; ``pwd.fill`` requires one.
          3. Call the bridge. Vault-locked is treated as a hard
             failure for this attempt; the pwd daemon has already
             triggered the polkit unlock prompt asynchronously so a
             retry after a few seconds may succeed. The plugin
             surfaces ``vault_locked`` so the content script can
             show a "click to unlock" affordance.
          4. With credentials in hand, ask the compositor popup.
          5. On allow → return the credential. On deny → return
             ``autofill_denied``.

        On ``intent_token_bad_hmac`` (the bridge restarted and
        rotated its secret) the orchestrator drops the stale secret,
        re-handshakes exactly once, retries the fill exactly once.
        """
        if not isinstance(url, str) or not url:
            self._audit(url, "deny", "missing_url")
            return FillResult(ok=False, error="missing_url")
        if not self.has_session():
            self._audit(url, "deny", "no_session")
            return FillResult(ok=False, error="no_session")
        if not self._fill_lock.acquire(blocking=False):
            self._audit(url, "deny", "busy")
            return FillResult(ok=False, error="busy")
        try:
            result = self._do_fill(url, username)
            if (not result.ok
                    and result.error in ("intent_token_bad_hmac",
                                         "missing_intent_token")):
                # Bridge restarted → re-handshake once, retry once.
                log.info("autofill bad_hmac, re-handshaking")
                self._session_secret = None
                new_secret = perform_handshake(self.bridge)
                if not new_secret:
                    self._audit(url, "deny", "handshake_refresh_failed")
                    return FillResult(
                        ok=False, error="handshake_refresh_failed")
                self.set_session_secret(new_secret)
                result = self._do_fill(url, username)
            self._audit(url, "allow" if result.ok else "deny",
                        result.error or "")
            return result
        finally:
            self._fill_lock.release()

    def _audit(self, url: str, decision: str, reason: str) -> None:
        """Emit a structured journal line for every autofill outcome.

        Format mirrors ClipboardGate.qml's verdict shape so operators
        running ``journalctl --user --identifier qdbrowser`` see a
        uniform audit trail across the two cross-process gates
        (P04 HIGH-4 operational).
        """
        try:
            log.info("autofill url=%s silo=%s decision=%s reason=%s",
                     _sanitize_for_prompt(url),
                     self.silo or "", decision, reason or "")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Module-level convenience for the in-tree integration tests
# ---------------------------------------------------------------------------

def perform_handshake(bridge: BridgeClient) -> str | None:
    """Run ``qdistro.handshake`` against the bridge to fetch the
    per-session HMAC secret. Returns the hex secret or ``None`` on
    failure (logged once at WARN).
    """
    try:
        reply = bridge.call("qdistro.handshake", {})
    except Exception as exc:  # noqa: BLE001
        log.warning("handshake failed: %s", exc)
        return None
    if not reply.get("ok"):
        log.warning("handshake bridge reply not ok: %s",
                    reply.get("error"))
        return None
    secret = reply.get("session_secret_hex") or ""
    if not isinstance(secret, str) or not secret:
        return None
    return secret


__all__ = [
    "AutofillDecision",
    "AutofillDenied",
    "AutofillError",
    "AutofillNoMatch",
    "AutofillOrchestrator",
    "AutofillPrompt",
    "AutofillPromptClient",
    "AutofillVaultLocked",
    "BridgeClient",
    "FillResult",
    "IntentToken",
    "JeepneyAutofillPromptClient",
    "JeepneyBridgeClient",
    "mint_intent_token",
    "perform_handshake",
]
