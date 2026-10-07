"""qdistro polkit authentication agent.

Registers itself with polkitd as the session agent for admin's uid;
intercepts BeginAuthentication; dispatches to one of three auth
methods depending on configuration:

- ``pam``     — verify the admin's password via python-pam (a small
                Qt prompt subprocess `qdistro-polkit-prompt` reads the
                password). Used for actions that need the actual admin
                credential (spec/13 password-vault unlock, etc.).
- ``fprint``  — verify via fprintd (net.reactivated.Fprint.Device).
                Same prompt subprocess but it kicks the verify and
                waits for the VerifyStatus signal.
- ``broker``  — delegate to the qdistro admin broker's
                RequestPolkitAuth / WaitForDecision flow. Approval
                is a yes/no admin decision rendered by the
                admin-approval-app (spec/25).

Method selection (highest priority first):

  1. ``QDISTRO_POLKIT_METHOD``   — env override (tests).
  2. ``QDISTRO_POLKIT_NONINTERACTIVE`` — bypass prompts entirely
     (``allow`` / ``deny`` / ``password=<pw>``). Tests only.
  3. /etc/qdistro/polkit-agent.conf  — fnmatch glob → method.
  4. default ``broker``.

polkitd accepts
``org.freedesktop.PolicyKit1.Authority.AuthenticationAgentResponse2``
from uid 0 only, and this agent runs as ADMIN_UID — so it never calls
polkitd back directly. The privileged broker delivers the response:
for ``broker``-method requests it does so when the filed request is
allowed, and for ``pam``/``fprint`` verdicts the agent relays through
``RespondPolkitAuth``. The response identity is picked from the list
polkitd itself offered in BeginAuthentication. On failure the agent
just completes the BeginAuthentication call (polkit treats no
response as deny).

The qdistro action namespace mapping (``action_to_qdistro``) is
unchanged from the v1 mapper-only stub: tests in
``test_polkit_mapper.py`` still pin it.

Spec refs: ``doc/password-manager.md`` §"Phase-8 follow-ups"
(admin polkit AuthenticationAgent), ``doc/admin-approval.md``
(broker delegation path), ``doc/permissions.md`` (qdistro
namespace).
"""
from __future__ import annotations

import fnmatch
import os
import pwd as _pwd_mod
import subprocess
import sys
import syslog
import threading

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

POLKIT_BUS = "org.freedesktop.PolicyKit1"
POLKIT_OBJ = "/org/freedesktop/PolicyKit1/Authority"
POLKIT_IFACE_AUTHORITY = "org.freedesktop.PolicyKit1.Authority"
POLKIT_IFACE_AGENT = "org.freedesktop.PolicyKit1.AuthenticationAgent"

AGENT_OBJ = "/org/qdistro/PolkitAgent"
AGENT_BUS = "org.qdistro.PolkitAgent"

QDISTRO_BROKER_BUS = "org.qdistro.AdminBroker1"
QDISTRO_BROKER_OBJ = "/org/qdistro/AdminBroker1"

# qdistro is single-tenant: the admin role is the fixed 'admin' account, which
# must be uid 1000. Resolve leniently at import (default 1000 when the account
# is absent) so this module stays importable for unit tests on hosts without
# the admin user; the invariant is enforced fail-closed at agent startup via
# _require_admin_account() (see main()).
def _resolve_admin_uid() -> int:
    try:
        return _pwd_mod.getpwnam("admin").pw_uid
    except KeyError:
        return 1000


def _require_admin_account() -> None:
    """Fail closed if the host lacks the fixed admin/uid-1000 account."""
    try:
        uid = _pwd_mod.getpwnam("admin").pw_uid
    except KeyError as e:
        raise RuntimeError("fixed admin user 'admin' does not exist") from e
    if uid != 1000:
        raise RuntimeError(
            f"fixed admin user 'admin' must resolve to uid 1000, got {uid}")


ADMIN_UID = _resolve_admin_uid()

# Reply timeouts for the broker delegation path. Filing is bounded; waiting
# for a decision is bounded only by admin attention, so the cutoff there is
# generous. Mirrors media/qdistro_media_exec.py, which had this right.
_REQUEST_TIMEOUT_S = 90
_WAIT_TIMEOUT_S = 900

DEFAULT_METHOD = "broker"
DEFAULT_PAM_SERVICE = "login"
DEFAULT_CONFIG_PATH = "/etc/qdistro/polkit-agent.conf"
DEFAULT_USER_CONFIG_PATH = "~/.config/qdistro/polkit-agent.conf"
DEFAULT_PROMPT_BIN = "/usr/local/bin/qdistro-polkit-prompt"

VALID_METHODS = ("pam", "fprint", "broker")

_FPRINTD_BUS_NAME = "net.reactivated.Fprint"
_FPRINTD_MGR_PATH = "/net/reactivated/Fprint/Manager"
_FPRINTD_MGR_IFACE = "net.reactivated.Fprint.Manager"
_FPRINTD_DEV_IFACE = "net.reactivated.Fprint.Device"


# -- Detail sanitisation --------------------------------------------------
# polkit's BeginAuthentication details dict is attacker-influenced — the
# app that triggered the polkit check supplies the message + details.
# Scrub control chars + cap lengths before shipping to the broker; the
# admin's detail pane renders these verbatim.

_MAX_POLKIT_KEYS = 16
_MAX_POLKIT_VAL = 512


def _scrub_value(s: str) -> str:
    """Strip ANSI escapes, newlines, and non-printable chars from s;
    truncate to _MAX_POLKIT_VAL bytes."""
    out = "".join(c for c in s if c == "\t" or c == " " or c.isprintable())
    return out[:_MAX_POLKIT_VAL]


def _sanitize_polkit_details(raw) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in dict(raw).items():
        if len(out) >= _MAX_POLKIT_KEYS:
            break
        key = _scrub_value(str(k))[:64]
        if key:
            out[key] = _scrub_value(str(v))
    return out


# -- Action namespace translation ----------------------------------------

def action_to_qdistro(polkit_id: str) -> str:
    """Map a polkit action ID into the qdistro namespace.

    Rules:
    - ``org.freedesktop.<rest>`` → ``qdistro.<rest>``  (most actions in practice)
    - ``<rest>`` (non-freedesktop) → ``qdistro.external.<rest>``

    The second rule keeps the namespace namespaced so an admin can
    still write broker rules that match all non-freedesktop polkit
    actions with a wildcard if the rules engine ever grows one.
    """
    if not isinstance(polkit_id, str) or not polkit_id:
        raise ValueError(f"bad polkit action id: {polkit_id!r}")
    fdo = "org.freedesktop."
    if polkit_id.startswith(fdo):
        return "qdistro." + polkit_id[len(fdo):]
    return "qdistro.external." + polkit_id


# -- Method config --------------------------------------------------------

def load_method_config(path: str = DEFAULT_CONFIG_PATH) -> list[tuple[str, str]]:
    """Parse the per-action method config.

    Format is one ``glob = method`` line per row, with ``#`` comments.
    Globs are fnmatch-style against the polkit action_id (NOT the
    qdistro-namespaced form). First-match wins — order in the file
    matters.

    Returns a list of (glob, method) pairs in declared order. Returns
    [] silently if the file is absent.
    """
    out: list[tuple[str, str]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                if "=" not in ln:
                    continue
                glob, method = ln.split("=", 1)
                glob = glob.strip()
                method = method.strip().lower()
                if glob and method in VALID_METHODS:
                    out.append((glob, method))
    except OSError:
        pass
    return out


def load_method_config_layered(
        user_path: str = DEFAULT_USER_CONFIG_PATH,
        system_path: str = DEFAULT_CONFIG_PATH) -> list[tuple[str, str]]:
    """Layered config: user entries first, then system entries.

    First-match-wins semantics combined with this ordering means a user
    glob always wins over a system glob, but unmatched system globs
    still apply. The agent's per-user session loads from
    ``~/.config/qdistro/polkit-agent.conf`` (user-writable, no root
    needed) layered atop ``/etc/qdistro/polkit-agent.conf`` (the
    system default). Editing the user file is what the admin app's
    Polkit tab writes.
    """
    user = load_method_config(os.path.expanduser(user_path))
    system = load_method_config(system_path)
    return user + system


def render_user_config(entries: list[tuple[str, str]],
                       header: str | None = None) -> str:
    """Render a list of (glob, method) pairs back to file format.

    Used by the admin app's Polkit tab when saving the user override
    file. Drops invalid entries silently; preserves declaration order.
    Adds a generated-by header so the file is recognisable.
    """
    if header is None:
        header = ("# qdistro-polkit-agent — per-user overrides\n"
                  "# Generated by qdistro-admin-approval-app.\n"
                  "# Format: <fnmatch glob> = <pam|fprint|broker>\n")
    body_lines = []
    for glob, method in entries:
        glob = (glob or "").strip()
        method = (method or "").strip().lower()
        if not glob or method not in VALID_METHODS:
            continue
        body_lines.append(f"{glob} = {method}")
    body = "\n".join(body_lines)
    return header + body + ("\n" if body else "")


def save_user_config(entries: list[tuple[str, str]],
                     path: str = DEFAULT_USER_CONFIG_PATH) -> str:
    """Atomically write entries to the user override file.

    Creates the parent directory if absent (mode 0o700; common parent
    is ~/.config which already exists, but the qdistro/ subdir might
    not). Returns the resolved path. Empty entries write a header-only
    file which is treated as "no overrides" by the loader.
    """
    resolved = os.path.expanduser(path)
    parent = os.path.dirname(resolved)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    body = render_user_config(entries)
    tmp = f"{resolved}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)  # in case tmp pre-existed with a looser mode
    os.replace(tmp, resolved)
    return resolved


def select_method(action_id: str,
                  config: list[tuple[str, str]],
                  env: dict | None = None) -> str:
    """Pick an auth method for a polkit action.

    Priority:
      1. env ``QDISTRO_POLKIT_METHOD`` (test override)
      2. config glob match (first-match-wins)
      3. ``DEFAULT_METHOD``
    """
    if env is None:
        env = os.environ
    forced = env.get("QDISTRO_POLKIT_METHOD", "").strip().lower()
    if forced in ("pam", "fprint", "broker"):
        return forced
    for glob, method in config:
        if fnmatch.fnmatchcase(action_id, glob):
            return method
    return DEFAULT_METHOD


# -- PAM ------------------------------------------------------------------

def _pam_authenticate(user: str, password: str,
                      service: str = DEFAULT_PAM_SERVICE) -> tuple[bool, str]:
    """Verify ``password`` against PAM for ``user``.

    Returns (ok, reason). reason is human-readable when ok is False.
    Wrapped so a missing python-pam doesn't crash the agent — auth
    just fails closed with a clear message.
    """
    try:
        import pam  # type: ignore[import-not-found]
    except ImportError:
        return False, "python-pam not installed"
    try:
        p = pam.pam()
        ok = p.authenticate(user, password, service=service)
        if ok:
            return True, "pam-ok"
        return False, p.reason or "pam-denied"
    except Exception as e:  # noqa: BLE001
        return False, f"pam-error: {e!r}"


def _prompt_password(action_id: str, message: str,
                     prompt_bin: str = DEFAULT_PROMPT_BIN,
                     env: dict | None = None) -> str | None:
    """Spawn the qdistro-polkit-prompt subprocess to read the admin's
    password. Returns the password string on success, or None if the
    user cancelled or the prompt is unavailable.

    For tests / headless paths, ``QDISTRO_POLKIT_NONINTERACTIVE`` can
    short-circuit:
      - ``deny``           → returns None
      - ``password=<pw>``  → returns ``<pw>``
      - ``allow``          → returns ``"`` (empty) — caller should
        treat as "test passed without password"
    """
    if env is None:
        env = os.environ
    nonint = env.get("QDISTRO_POLKIT_NONINTERACTIVE", "").strip()
    if nonint == "deny":
        return None
    if nonint.startswith("password="):
        return nonint.split("=", 1)[1]
    if nonint == "allow":
        return ""
    if not prompt_bin or not os.path.exists(prompt_bin):
        # No prompt UI available + no test override — fail closed.
        return None
    try:
        proc = subprocess.run(
            [prompt_bin, "--mode=pam",
             f"--action={action_id}",
             f"--message={message or 'Authentication required'}"],
            input="", capture_output=True, text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.rstrip("\n")


# -- fprintd ---------------------------------------------------------------

def _fprint_verify(user: str, system_bus,
                   timeout_s: int = 30) -> tuple[bool, str]:
    """Run a fprintd VerifyStart cycle for ``user``. Blocks until a
    VerifyStatus signal arrives or the timeout elapses.

    Returns (matched, reason).
    """
    try:
        mgr_obj = system_bus.get_object(_FPRINTD_BUS_NAME, _FPRINTD_MGR_PATH)
        mgr = dbus.Interface(mgr_obj, _FPRINTD_MGR_IFACE)
        dev_path = mgr.GetDefaultDevice()
        dev_obj = system_bus.get_object(_FPRINTD_BUS_NAME, dev_path)
        dev = dbus.Interface(dev_obj, _FPRINTD_DEV_IFACE)
        dev.Claim(user)
    except dbus.DBusException as e:
        return False, f"fprintd-claim: {e.get_dbus_message()}"

    done = threading.Event()
    result: dict[str, str] = {"status": "", "matched": False}

    def on_verify_status(status, finished):
        result["status"] = str(status)
        if str(status) == "verify-match":
            result["matched"] = True
        if bool(finished) or str(status) == "verify-match":
            done.set()

    sig = system_bus.add_signal_receiver(
        on_verify_status, signal_name="VerifyStatus",
        dbus_interface=_FPRINTD_DEV_IFACE, path=dev_path)
    try:
        dev.VerifyStart("any")
        done.wait(timeout=timeout_s)
    finally:
        try:
            dev.VerifyStop()
        except Exception:
            pass
        try:
            dev.Release()
        except Exception:
            pass
        try:
            sig.remove()
        except Exception:
            pass
    if result["matched"]:
        return True, "fprint-match"
    return False, f"fprint:{result['status'] or 'timeout'}"


# -- Agent implementation -------------------------------------------------

class QdistroPolkitAgent(dbus.service.Object):
    """polkit authentication agent — PAM / fprintd / broker dispatch."""

    def __init__(self, bus, path: str,
                 config: list[tuple[str, str]] | None = None):
        super().__init__(bus, path)
        self._sysbus = dbus.SystemBus()
        self._broker = None
        self._config = list(config) if config is not None \
            else load_method_config_layered()

    # -- broker delegation (the v1 path) ----------------------------------

    def _broker_iface(self):
        if self._broker is None:
            # Bind the proxy to the broker's UNIQUE name, not the
            # well-known one. A proxy on the well-known name re-resolves
            # its destination per call, so a worker that filed request id
            # 1 on broker instance D1 could, after a restart, call
            # WaitForDecision(1) on D2 -- whose id counter restarted at 1
            # -- and consume an unrelated request's decision (astra
            # r153). A unique-name proxy dies with its owner: the wait
            # fails with a D-Bus error and the auth denies, fail-closed.
            # activate_name_owner also starts an activatable broker, as
            # the previous get_object-on-well-known-name did.
            owner = str(self._sysbus.activate_name_owner(QDISTRO_BROKER_BUS))
            obj = self._sysbus.get_object(owner, QDISTRO_BROKER_OBJ)
            self._broker = dbus.Interface(obj, QDISTRO_BROKER_BUS)
        return self._broker

    def _file_request(self, qdistro_action: str, details: dict,
                      cookie: str, identities):
        """File one polkit auth request; return (broker proxy, request id).

        The caller must wait on the SAME proxy the request was filed
        through: request ids mean something only to the broker instance
        that issued them, and the proxy is what pins the instance. The
        retry used to catch every DBusException and re-file. Two of the
        errors it caught — NoReply and a mid-call disconnect — mean "we do
        not know whether the broker got it", so re-filing produced a second
        pending request for the same polkit cookie: the admin saw the same
        prompt twice and answering one left the other stranded. ServiceUnknown
        and NameHasNoOwner are the only ones that positively mean nothing was
        filed, because the name had no owner to receive the call.

        The request is filed as RequestPolkitAuth carrying polkit's cookie
        and offered identity list: on an allow the broker answers polkitd
        with AuthenticationAgentResponse2 itself — that call is uid-0-only
        and this process is not uid 0.
        """
        iface = self._broker_iface()
        try:
            return iface, int(iface.RequestPolkitAuth(
                qdistro_action, details, str(cookie), identities,
                timeout=_REQUEST_TIMEOUT_S))
        except dbus.DBusException as e:
            if e.get_dbus_name() not in (
                    "org.freedesktop.DBus.Error.ServiceUnknown",
                    "org.freedesktop.DBus.Error.NameHasNoOwner"):
                raise
            # The broker was not on the bus. Drop the cached proxy (it may
            # be bound to a dead unique name) and try once more, in case it
            # is being restarted underneath us.
            self._broker = None
            iface = self._broker_iface()
            return iface, int(iface.RequestPolkitAuth(
                qdistro_action, details, str(cookie), identities,
                timeout=_REQUEST_TIMEOUT_S))

    def _ask_broker(self, qdistro_action: str, details: dict,
                    cookie: str, identities) -> bool:
        try:
            iface, rid = self._file_request(qdistro_action, details,
                                            cookie, identities)
        except dbus.DBusException as e:
            self._broker = None
            syslog.syslog(syslog.LOG_ERR,
                          f"could not file a broker request: {e}; "
                          f"denying polkit request")
            return False
        try:
            # An admin has to read the prompt and decide, so this blocks on
            # human attention. dbus-python's default reply timeout is 25s,
            # which expired long before any real decision and was then read
            # as "broker unreachable" — the broker was up and healthy the
            # whole time. Same generous cutoff as the media exec client.
            # `iface` is the same unique-name-bound proxy that filed the
            # request: rid only exists on that broker instance.
            return bool(iface.WaitForDecision(rid, timeout=_WAIT_TIMEOUT_S))
        except dbus.DBusException as e:
            self._broker = None
            syslog.syslog(
                syslog.LOG_ERR,
                f"no decision for broker request {rid}: {e}; denying polkit "
                f"request. The request may still be pending in the broker — "
                f"it is NOT re-filed here, because a second copy of the same "
                f"prompt is worse than one that goes unanswered.")
            return False

    # -- BeginAuthentication ---------------------------------------------

    @dbus.service.method(POLKIT_IFACE_AGENT,
                         in_signature="sssa{ss}sa(sa{sv})",
                         out_signature="",
                         async_callbacks=("ok_cb", "err_cb"))
    def BeginAuthentication(self, action_id, message, icon_name,
                            details, cookie, identities,
                            ok_cb, err_cb):
        """Called by polkitd when an action needs authentication."""
        action = str(action_id)
        msg = str(message)
        method = select_method(action, self._config)
        syslog.syslog(syslog.LOG_INFO,
                      f"polkit BeginAuth: action={action} method={method}")
        det = _sanitize_polkit_details(details)
        det["polkit_action_id"] = _scrub_value(action)
        det["polkit_message"]   = _scrub_value(msg)
        det["polkit_cookie"]    = _scrub_value(str(cookie))

        def _drive() -> bool:
            try:
                allowed, reason = self._authenticate(
                    action, msg, det, method, str(cookie), identities)
            except Exception as e:  # noqa: BLE001
                syslog.syslog(syslog.LOG_ERR,
                              f"polkit-agent auth crashed: {e}")
                err_cb(dbus.DBusException(
                    f"qdistro polkit-agent crashed: {e}",
                    name="org.freedesktop.PolicyKit1.Error.Failed"))
                return False
            # The broker method answers polkitd itself on an allow — the
            # response call is uid-0-only, so the uid-1000 agent cannot
            # (and must not try to) deliver it. pam/fprint verdicts are
            # local, so those go through the broker's relay.
            if allowed and method != "broker":
                try:
                    self._respond(str(cookie), identities)
                except Exception as e:  # noqa: BLE001
                    syslog.syslog(syslog.LOG_ERR,
                                  f"AuthenticationAgentResponse2 failed: {e}")
                    err_cb(dbus.DBusException(
                        f"could not deliver positive decision: {e}",
                        name="org.freedesktop.PolicyKit1.Error.Failed"))
                    return False
            syslog.syslog(syslog.LOG_INFO,
                          f"polkit BeginAuth: action={action} "
                          f"method={method} -> "
                          f"{'allow' if allowed else 'deny'} ({reason})")
            ok_cb()
            return False
        # Not GLib.idle_add: _drive blocks for the duration of the auth
        # (broker WaitForDecision waits on a human, up to _WAIT_TIMEOUT_S;
        # the PAM prompt and fprint verify block too), and an idle callback
        # runs ON the main loop — one stray BeginAuth (an unrelated package
        # asking for auth_admin) parked the loop for its whole timeout and
        # froze session registration in the VM run. A worker thread leaves
        # the loop free to service registration and signal watches.
        threading.Thread(target=_drive, daemon=True).start()

    # -- method dispatch ---------------------------------------------------

    def _authenticate(self, action_id: str, message: str,
                      details: dict, method: str, cookie: str,
                      identities) -> tuple[bool, str]:
        if method == "pam":
            return self._auth_pam(action_id, message)
        if method == "fprint":
            return self._auth_fprint()
        # broker (default fallback)
        qd_action = action_to_qdistro(action_id)
        ok = self._ask_broker(qd_action, details, cookie, identities)
        return ok, ("broker-allow" if ok else "broker-deny")

    def _auth_pam(self, action_id: str,
                  message: str) -> tuple[bool, str]:
        user = _admin_user()
        pw = _prompt_password(action_id, message)
        if pw is None:
            return False, "prompt-cancelled"
        # For "allow" non-interactive shortcut (empty pw + bypass mode),
        # don't run PAM — let the caller treat it as a test pass.
        nonint = os.environ.get("QDISTRO_POLKIT_NONINTERACTIVE", "")
        if nonint == "allow" and pw == "":
            return True, "noninteractive-allow"
        ok, reason = _pam_authenticate(user, pw)
        return ok, reason

    def _auth_fprint(self) -> tuple[bool, str]:
        nonint = os.environ.get("QDISTRO_POLKIT_NONINTERACTIVE", "")
        if nonint == "allow":
            return True, "noninteractive-allow"
        if nonint == "deny":
            return False, "noninteractive-deny"
        return _fprint_verify(_admin_user(), self._sysbus)

    @dbus.service.method(POLKIT_IFACE_AGENT,
                         in_signature="s", out_signature="")
    def CancelAuthentication(self, cookie):
        # Forward the cancel to the broker: it decides the matching
        # queued request deny so the admin prompt does not linger and
        # this auth's WaitForDecision waiter releases.
        syslog.syslog(syslog.LOG_INFO, f"polkit cancel: {cookie}")
        try:
            self._broker_iface().CancelPolkitAuth(
                str(cookie), timeout=_REQUEST_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            syslog.syslog(
                syslog.LOG_WARNING,
                f"could not forward polkit cancel to the broker: {e}; "
                f"the queued request stays until decided or reaped")

    # -- polkit reply --
    def _respond(self, cookie: str, identities) -> None:
        """Relay a positive pam/fprint verdict through the broker.

        polkitd accepts AuthenticationAgentResponse2 from uid 0 only and
        the agent runs as the admin uid, so the privileged broker makes
        the call. The identity is picked FROM polkit's offered list —
        on this image [unix-user uid=0], not the session uid — because
        polkitd rejects a response naming an identity it did not offer.
        """
        self._broker_iface().RespondPolkitAuth(
            str(cookie), identities, timeout=_REQUEST_TIMEOUT_S)


# -- Registration with polkitd -------------------------------------------
#
# polkitd only accepts RegisterAuthenticationAgent for a unix-session subject
# that EQUALS the session it computes for the caller
# (polkitbackendinteractiveauthority.c: "Cannot determine session the caller
# is in" / "Passed session and the session the caller is in differs").
# polkitd computes that session as sd_pid_get_session(caller pid) and, when
# the caller is not inside a session scope -- always true here, the agent
# runs in user@UID.service -- falls back to sd_uid_get_display(uid): the
# user's display session.
#
# The user manager starts this unit from qdwin-session.target, which the
# lingering admin's default.target also wants, so the agent routinely runs
# while the admin has no display session at all: only logind's class=manager
# session for user@.service exists until greetd logs the admin in (and in
# headless/linger-only guests it never exists). Exiting non-zero there made
# Restart=on-failure respawn the agent every RestartSec forever. Instead the
# agent stays up, registers when a display session appears, and re-registers
# when the display session changes.

LOGIND_BUS = "org.freedesktop.login1"
LOGIND_OBJ = "/org/freedesktop/login1"
LOGIND_IFACE_MANAGER = "org.freedesktop.login1.Manager"
LOGIND_IFACE_SESSION = "org.freedesktop.login1.Session"
LOGIND_IFACE_USER = "org.freedesktop.login1.User"
DBUS_IFACE_PROPS = "org.freedesktop.DBus.Properties"

# logind session classes that never count as "the session the caller is in"
# for polkitd: sd_pid_get_session() only resolves session-*.scope cgroups,
# and logind never elects a manager/background session as a user's display.
_NON_LOGIN_CLASSES = frozenset({"manager", "manager-early", "background",
                                "background-light", "none"})

# Safety-net poll for display-session changes logind does not signal as
# SessionNew/SessionRemoved (e.g. display re-election). Cheap: two D-Bus
# property reads, silent unless the outcome changes.
RECONCILE_POLL_S = 30
# Short follow-up checks after a logind session signal: a new session is
# announced before logind elects it as the user's display session.
_SIGNAL_FOLLOWUP_MS = (300, 1500, 5000)


def _logind_session_props(bus, session_path) -> tuple[str, str]:
    obj = bus.get_object(LOGIND_BUS, session_path)
    props = dbus.Interface(obj, DBUS_IFACE_PROPS)
    sid = str(props.Get(LOGIND_IFACE_SESSION, "Id"))
    cls = str(props.Get(LOGIND_IFACE_SESSION, "Class"))
    return sid, cls


def _session_id(bus) -> str | None:
    """The logind session polkitd will attribute this process to, or None.

    Mirrors polkitd's own lookup so the subject we pass equals the session it
    computes for the caller: the process's own login session if it has one
    (agent started by hand from a terminal), else the user's display session
    (agent started by the user manager -- the normal case). Returns None when
    neither exists; the caller then waits instead of failing.
    ``QDISTRO_POLKIT_SESSION_ID`` overrides for tests.

    ``XDG_SESSION_ID`` is deliberately not consulted: the user manager's
    environment is shared by every login and keeps a value imported by an
    earlier one, so it can name a session that is gone or is not the one
    polkitd sees.
    """
    test = os.environ.get("QDISTRO_POLKIT_SESSION_ID")
    if test:
        return test
    manager = dbus.Interface(bus.get_object(LOGIND_BUS, LOGIND_OBJ),
                             LOGIND_IFACE_MANAGER)
    try:
        path = manager.GetSessionByPID(dbus.UInt32(os.getpid()))
        sid, cls = _logind_session_props(bus, path)
        if sid and cls not in _NON_LOGIN_CLASSES:
            return sid
    except dbus.DBusException:
        pass  # NoSessionForPID: not in a session scope (user@.service)
    try:
        user_path = manager.GetUser(dbus.UInt32(os.getuid()))
    except dbus.DBusException:
        return None  # NoUserForUID
    props = dbus.Interface(bus.get_object(LOGIND_BUS, user_path),
                           DBUS_IFACE_PROPS)
    display = props.Get(LOGIND_IFACE_USER, "Display")
    sid, path = str(display[0]), str(display[1])
    if not sid or path in ("", "/"):
        return None
    try:
        _sid, cls = _logind_session_props(bus, path)
    except dbus.DBusException:
        return None  # session vanished between the two calls
    if cls in _NON_LOGIN_CLASSES:
        return None
    return sid


def _authority(bus):
    return dbus.Interface(bus.get_object(POLKIT_BUS, POLKIT_OBJ),
                          POLKIT_IFACE_AUTHORITY)


def _session_subject(session_id: str):
    return ("unix-session", {"session-id": dbus.String(session_id)})


def _private_system_bus():
    """A dedicated system-bus connection for the agent + its registration.

    Not the shared ``dbus.SystemBus()``: the registration polkitd records
    belongs to the unique name of the calling connection, and closing that
    connection is the only reliable way to retract it -- polkitd refuses
    UnregisterAuthenticationAgent once the caller's session has moved on.
    """
    conn = dbus.SystemBus(private=True)
    conn.set_exit_on_disconnect(False)
    return conn


def _register(bus, agent_path: str) -> tuple[str | None, str | None]:
    """Register for the current session.

    Returns ``(session_id, owner)``; both None when no login session
    exists. ``owner`` is polkitd's UNIQUE bus name, resolved BEFORE the
    call and used as its destination: resolving it afterwards would let
    a polkitd restart in between pair a live registration that died with
    daemon D1 to D2's freshly-read owner, leaving the cache confident of
    a registration nobody holds (astra r153). A restart inside the
    resolve/call window fails the call -- the proxy is bound to the dead
    unique name -- instead of landing on an instance we did not name.

    Raises dbus.DBusException when the registration cannot be issued or
    polkitd refuses it.
    """
    sid = _session_id(bus)
    if sid is None:
        return None, None
    # activate_name_owner, not get_name_owner: polkitd is dbus-activated,
    # and a bare owner lookup on an absent daemon would leave
    # registration waiting for some other client to start it (astra
    # r156). The call both activates and returns the unique owner to
    # pin the registration to.
    owner = str(bus.activate_name_owner(POLKIT_BUS))
    authority = dbus.Interface(
        bus.get_object(owner, POLKIT_OBJ), POLKIT_IFACE_AUTHORITY)
    authority.RegisterAuthenticationAgent(
        _session_subject(sid), "en_US.UTF-8", agent_path)
    syslog.syslog(syslog.LOG_NOTICE,
                  f"registered as session polkit agent (path={agent_path}, "
                  f"session={sid})")
    return sid, owner


def _polkit_owner(bus) -> str | None:
    """Unique name currently owning polkitd's well-known name.

    None means the name is unowned (polkitd absent); a DBusException means
    the lookup itself failed and callers should not draw conclusions.
    """
    try:
        return str(bus.get_name_owner(POLKIT_BUS))
    except dbus.DBusException as e:
        if e.get_dbus_name() == "org.freedesktop.DBus.Error.NameHasNoOwner":
            return None
        raise


class SessionRegistrar:
    """Keep the agent registered for the user's current login session.

    The agent object and its polkitd registration live on a private
    system-bus connection this class owns and recreates per session. On a
    session change polkitd refuses UnregisterAuthenticationAgent for the old
    session -- the subject must equal the session it computes for the
    caller, which by then is the new session or none -- so the old entry
    could only be dropped by asking at exactly the right moment. Instead the
    connection itself is closed: polkitd removes an agent's registration
    when the unique name that registered it vanishes, so no stale
    registration survives a session change (or accumulates across ordinary
    sequential logins).

    A registration is cached together with the unique-name owner of
    POLKIT_BUS it was made against. Owner changes mean the daemon (and its
    registration table) restarted, so the cache is dropped; a queued
    owner-acquired signal for the same owner is ignored. This ordering is
    what makes a Register call that itself bus-activated polkitd safe.

    ``reconcile()`` is idempotent: it looks up the session polkitd would
    attribute us to and (re-)registers only when that differs from the one
    we registered for. Logging happens on state changes only, so an agent
    waiting for a login does not spam the journal.
    """

    def __init__(self, bus, agent_path: str,
                 make_connection=None, make_agent=None):
        # `bus` is the shared system bus, used only for logind queries and
        # signal watches so it never has to be torn down. The exported agent
        # object goes on the private connection together with its
        # registration -- polkitd calls BeginAuthentication back on the
        # registering unique name, so both must be the same connection.
        self.bus = bus
        self.agent_path = agent_path
        self._make_connection = make_connection or _private_system_bus
        self._make_agent = make_agent or (
            lambda conn: QdistroPolkitAgent(conn, agent_path))
        self._conn = None
        self._agent = None
        self.session_id: str | None = None
        # Unique-name owner of POLKIT_BUS the current registration was made
        # against. Registrations die with their polkitd instance; a queued
        # NameOwnerChanged for an activation that already happened must not
        # invalidate this cache, so reconcile() compares owners rather than
        # trusting signal order.
        self._polkit_owner: str | None = None
        self._last_state: object = object()

    def _note(self, state, priority, message: str) -> None:
        if state != self._last_state:
            self._last_state = state
            syslog.syslog(priority, message)

    def _drop_connection(self) -> None:
        conn = self._conn
        self._conn = None
        self._agent = None
        self._polkit_owner = None
        if conn is None:
            return
        try:
            conn.close()
        except Exception as e:  # noqa: BLE001
            syslog.syslog(syslog.LOG_WARNING,
                          f"closing the retired agent connection failed: {e}")

    def _forget_registration(self, reason: str) -> None:
        """The polkitd instance we registered with is gone or restarted.

        Registrations do not survive their daemon, so only the cache needs
        clearing; the private connection is still usable and registering on
        it lands in the new instance's empty table.
        """
        old = self.session_id
        self.session_id = None
        self._polkit_owner = None
        syslog.syslog(syslog.LOG_NOTICE,
                      f"{reason}; the registration for session {old} died "
                      "with the old polkitd instance")

    def reconcile(self) -> bool:
        try:
            want = _session_id(self.bus)
        except dbus.DBusException as e:
            self._note(("lookup-error", str(e)), syslog.LOG_WARNING,
                       f"cannot query logind for the login session: {e}")
            return True
        if self._conn is not None and not self._conn.get_is_connected():
            # The transport is gone; polkitd's name-owner cleanup already
            # removed whatever registration this connection held.
            self._conn = None
            self._agent = None
            self.session_id = None
            self._polkit_owner = None
        if self.session_id is not None:
            # The registration is only valid while the polkitd instance we
            # registered with owns the name. Checking here (not just in the
            # signal handler) covers the race where our Register call
            # bus-activated polkitd and its owner-acquired signal is still
            # queued behind the main loop.
            try:
                owner = _polkit_owner(self.bus)
            except dbus.DBusException as e:
                self._note(("owner-error", str(e)), syslog.LOG_WARNING,
                           f"cannot resolve polkitd's bus name owner: {e}")
                return True
            if owner != self._polkit_owner:
                self._forget_registration("polkitd restarted or vanished")
        if want is not None and want == self.session_id:
            return True
        if self.session_id is not None or (
                want is None and self._conn is not None):
            old = self.session_id
            self.session_id = None
            self._drop_connection()
            if old is not None:
                syslog.syslog(syslog.LOG_NOTICE,
                              f"login session {old} ended or is no longer "
                              "the display session; unregistered")
        if want is None:
            self._note("no-session", syslog.LOG_NOTICE,
                       f"no login session for uid {os.getuid()} yet; "
                       "waiting for one before registering with polkitd")
            return True
        if self._conn is None:
            try:
                conn = self._make_connection()
                try:
                    # libdbus marks bus connections exit-on-disconnect:
                    # without this, closing the retired connection on a
                    # session change (or its transport dying) exits the
                    # process with status 1 -- silently, because it is a
                    # C-level exit() reached through mainloop dispatch,
                    # not a Python exception.
                    conn.set_exit_on_disconnect(False)
                except AttributeError:
                    pass  # unit-test fakes are not libdbus connections
                try:
                    agent = self._make_agent(conn)
                except Exception:
                    # Do not leak the fresh connection when the agent
                    # object cannot be exported on it.
                    conn.close()
                    raise
            except Exception as e:  # noqa: BLE001
                self._note(("connect-error", str(e)), syslog.LOG_ERR,
                           "cannot open a private system-bus connection: "
                           f"{e}; will retry when logind sessions change")
                return True
            self._conn, self._agent = conn, agent
        try:
            self.session_id, self._polkit_owner = \
                _register(self._conn, self.agent_path)
        except dbus.DBusException as e:
            name = e.get_dbus_name() or ""
            certain_miss = name in (
                # the name had no owner to send to, or the unique-name
                # destination died before the call -- provably nothing
                # was registered
                "org.freedesktop.DBus.Error.ServiceUnknown",
                "org.freedesktop.DBus.Error.NameHasNoOwner",
            ) or name.startswith("org.freedesktop.PolicyKit1.")
            if not certain_miss:
                # NoReply, a mid-call disconnect, anything else: the
                # Register may still have landed, so this connection's
                # registration state is unknown and a retry on it could
                # hit the duplicate-agent refusal. Retire the connection
                # -- dropping it retracts whatever polkitd recorded.
                self.session_id = None
                self._polkit_owner = None
                self._drop_connection()
            self._note(("register-error", want, str(e)), syslog.LOG_ERR,
                       f"registration for session {want} failed: {e}; "
                       "will retry when logind sessions change")
            return True
        if self.session_id is not None:
            self._last_state = ("registered", self.session_id)
        return True

    def _on_logind_signal(self, *args) -> None:
        self.reconcile()
        for delay in _SIGNAL_FOLLOWUP_MS:
            GLib.timeout_add(delay, self._once)

    def _on_logind_props(self, iface, changed, _invalidated) -> None:
        # SessionNew can arrive well before logind elects the session as
        # the user's Display session, and the election itself emits only
        # PropertiesChanged on the User object -- observed live: a ~25 s
        # gap between session creation and Display, during which every
        # reconcile() saw "no login session" and the registration waited
        # for the poll tick. Re-run reconcile on the election itself.
        if str(iface) != LOGIND_IFACE_USER or "Display" not in changed:
            return
        self.reconcile()
        for delay in _SIGNAL_FOLLOWUP_MS:
            GLib.timeout_add(delay, self._once)

    def _on_polkit_owner(self, name, old_owner, new_owner) -> None:
        if str(name) != POLKIT_BUS or not str(new_owner):
            return
        # A polkitd owner appeared. The signal may describe an activation
        # that predates a registration we already made on this same daemon
        # (queued behind a synchronous reconcile()), so it must not clear
        # state on its own -- reconcile() decides by comparing owners.
        self.reconcile()

    def _once(self) -> bool:
        self.reconcile()
        return False  # one-shot GLib source

    def start(self) -> None:
        for signal in ("SessionNew", "SessionRemoved"):
            try:
                self.bus.add_signal_receiver(
                    self._on_logind_signal, signal_name=signal,
                    dbus_interface=LOGIND_IFACE_MANAGER,
                    bus_name=LOGIND_BUS, path=LOGIND_OBJ)
            except Exception as e:  # noqa: BLE001
                syslog.syslog(syslog.LOG_WARNING,
                              f"cannot watch logind {signal}: {e}; "
                              f"polling every {RECONCILE_POLL_S}s only")
        try:
            # Display-session election is announced on the admin's User
            # object, separately from session create/remove -- pin the
            # watch to that object (logind names it after the uid).
            self.bus.add_signal_receiver(
                self._on_logind_props, signal_name="PropertiesChanged",
                dbus_interface="org.freedesktop.DBus.Properties",
                bus_name=LOGIND_BUS,
                path=f"{LOGIND_OBJ}/user/_{os.getuid()}")
        except Exception as e:  # noqa: BLE001
            syslog.syslog(syslog.LOG_WARNING,
                          f"cannot watch the user's Display election: {e}")
        try:
            # bus_name + path pin the watch to signals the bus daemon itself
            # sends about POLKIT_BUS -- any other sender's identically
            # shaped payload is not a polkitd lifecycle event.
            self.bus.add_signal_receiver(
                self._on_polkit_owner, signal_name="NameOwnerChanged",
                dbus_interface="org.freedesktop.DBus",
                bus_name="org.freedesktop.DBus",
                path="/org/freedesktop/DBus")
        except Exception as e:  # noqa: BLE001
            syslog.syslog(syslog.LOG_WARNING,
                          f"cannot watch for a polkitd restart: {e}")
        GLib.timeout_add_seconds(RECONCILE_POLL_S, self.reconcile)
        self.reconcile()


def _admin_user() -> str:
    return os.environ.get("QDISTRO_POLKIT_USER") \
        or os.environ.get("USER") \
        or os.environ.get("LOGNAME") \
        or "admin"


# -- main -----------------------------------------------------------------

def main() -> int:
    syslog.openlog("qdistro-polkit-agent", syslog.LOG_PID, syslog.LOG_DAEMON)
    # Fail closed before serving if the host lacks the admin/uid-1000 account.
    _require_admin_account()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    # Auth drivers run on worker threads (BeginAuthentication); libdbus
    # needs its thread support initialised before connections are shared
    # across threads.
    dbus.mainloop.glib.threads_init()
    bus = dbus.SessionBus()
    try:
        bus.request_name(AGENT_BUS, dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    except dbus.DBusException as e:
        syslog.syslog(syslog.LOG_ERR, f"request_name failed: {e}")
        print(f"qdistro-polkit-agent: cannot claim {AGENT_BUS}: {e}",
              file=sys.stderr)
        return 1
    # The session-bus name above is only the per-session singleton guard: it
    # stops two agents racing in one login. The AGENT OBJECT must live on the
    # SYSTEM bus, on the same connection we register from.
    #
    # polkitd records the unique name of the connection that called
    # RegisterAuthenticationAgent and calls BeginAuthentication back on THAT
    # name. Exporting the object on the session bus while registering from a
    # separate system-bus connection registered one name and exported the
    # object on another, so every BeginAuthentication call polkitd made went
    # to a system-bus connection with nothing at /org/qdistro/PolkitAgent.
    # Registration succeeded, the unit looked healthy, and no authorization
    # ever reached the agent — observed live: polkitd logged "FAILED to
    # authenticate", the agent's journal showed nothing at all.
    #
    # The registrar keeps object and registration on one PRIVATE system-bus
    # connection it owns and replaces on session change — closing the
    # connection is also the unregistration mechanism (see SessionRegistrar).
    sysbus = dbus.SystemBus()
    # Register now if the admin is logged in, otherwise wait for a login
    # without exiting (see SessionRegistrar): exiting made the user manager
    # respawn the agent every RestartSec for as long as no session existed.
    registrar = SessionRegistrar(sysbus, AGENT_OBJ)
    registrar.start()
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
