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
                RequestPermission / WaitForDecision flow. Approval
                is a yes/no admin decision rendered by the
                admin-approval-app (spec/25).

Method selection (highest priority first):

  1. ``QDISTRO_POLKIT_METHOD``   — env override (tests).
  2. ``QDISTRO_POLKIT_NONINTERACTIVE`` — bypass prompts entirely
     (``allow`` / ``deny`` / ``password=<pw>``). Tests only.
  3. /etc/qdistro/polkit-agent.conf  — fnmatch glob → method.
  4. default ``broker``.

On success the agent calls
``org.freedesktop.PolicyKit1.Authority.AuthenticationAgentResponse2``
with the unix-user identity for ADMIN_UID. On failure the agent
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

try:
    ADMIN_UID = _pwd_mod.getpwnam("admin").pw_uid
except KeyError as e:
    raise RuntimeError("fixed admin user 'admin' does not exist") from e
if ADMIN_UID != 1000:
    raise RuntimeError(
        f"fixed admin user 'admin' must resolve to uid 1000, got {ADMIN_UID}")

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
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
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
            obj = self._sysbus.get_object(QDISTRO_BROKER_BUS, QDISTRO_BROKER_OBJ)
            self._broker = dbus.Interface(obj, QDISTRO_BROKER_BUS)
        return self._broker

    def _ask_broker(self, qdistro_action: str, details: dict) -> bool:
        try:
            iface = self._broker_iface()
            rid = int(iface.RequestPermission(qdistro_action, details))
            return bool(iface.WaitForDecision(rid))
        except dbus.DBusException:
            self._broker = None
            try:
                iface = self._broker_iface()
                rid = int(iface.RequestPermission(qdistro_action, details))
                return bool(iface.WaitForDecision(rid))
            except dbus.DBusException as e:
                syslog.syslog(syslog.LOG_ERR,
                              f"broker unreachable: {e}; denying polkit request")
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
                allowed, reason = self._authenticate(action, msg, det, method)
            except Exception as e:  # noqa: BLE001
                syslog.syslog(syslog.LOG_ERR,
                              f"polkit-agent auth crashed: {e}")
                err_cb(dbus.DBusException(
                    f"qdistro polkit-agent crashed: {e}",
                    name="org.freedesktop.PolicyKit1.Error.Failed"))
                return False
            if allowed:
                try:
                    self._respond(cookie)
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
        GLib.idle_add(_drive)

    # -- method dispatch ---------------------------------------------------

    def _authenticate(self, action_id: str, message: str,
                      details: dict, method: str) -> tuple[bool, str]:
        if method == "pam":
            return self._auth_pam(action_id, message)
        if method == "fprint":
            return self._auth_fprint()
        # broker (default fallback)
        qd_action = action_to_qdistro(action_id)
        ok = self._ask_broker(qd_action, details)
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
        # v1: no-op. If admin takes forever we just keep the broker
        # request open; polkit will eventually time out upstream.
        syslog.syslog(syslog.LOG_INFO, f"polkit cancel: {cookie}")

    # -- polkit reply --
    def _respond(self, cookie: str) -> None:
        polkitd = self._sysbus.get_object(POLKIT_BUS, POLKIT_OBJ)
        authority = dbus.Interface(polkitd, POLKIT_IFACE_AUTHORITY)
        identity = ("unix-user", {"uid": dbus.UInt32(ADMIN_UID)})
        authority.AuthenticationAgentResponse2(
            dbus.UInt32(os.getuid()), str(cookie), identity,
        )


# -- Registration with polkitd -------------------------------------------

def _register(bus, agent_path: str) -> None:
    polkitd = bus.get_object(POLKIT_BUS, POLKIT_OBJ)
    authority = dbus.Interface(polkitd, POLKIT_IFACE_AUTHORITY)
    subject = ("unix-session", {"session-id": dbus.String(_session_id())})
    authority.RegisterAuthenticationAgent(subject, "en_US.UTF-8", agent_path)
    syslog.syslog(syslog.LOG_NOTICE,
                  f"registered as session polkit agent (path={agent_path})")


def _admin_user() -> str:
    return os.environ.get("QDISTRO_POLKIT_USER") \
        or os.environ.get("USER") \
        or os.environ.get("LOGNAME") \
        or "admin"


def _session_id() -> str:
    """Find the logind session id for this process.

    Prefer ``XDG_SESSION_ID`` (set by pam_systemd in any logind-managed
    session). Fall back to ``/proc/self/sessionid`` (audit-kernel only),
    then ``loginctl show-session self -p Id``. Test overrides via
    ``QDISTRO_POLKIT_SESSION_ID``.
    """
    test = os.environ.get("QDISTRO_POLKIT_SESSION_ID")
    if test:
        return test
    env = os.environ.get("XDG_SESSION_ID")
    if env:
        return env
    try:
        with open("/proc/self/sessionid") as f:
            sid = f.read().strip()
            if sid and sid != "4294967295":
                return sid
    except OSError:
        pass
    try:
        out = subprocess.check_output(
            ["loginctl", "show-session", "self", "-p", "Id", "--value"],
            text=True, timeout=5).strip()
        if out:
            return out
    except Exception:  # noqa: BLE001
        pass
    raise RuntimeError("cannot determine logind session id for polkit registration")


# -- main -----------------------------------------------------------------

def main() -> int:
    syslog.openlog("qdistro-polkit-agent", syslog.LOG_PID, syslog.LOG_DAEMON)
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    try:
        bus.request_name(AGENT_BUS, dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    except dbus.DBusException as e:
        syslog.syslog(syslog.LOG_ERR, f"request_name failed: {e}")
        print(f"qdistro-polkit-agent: cannot claim {AGENT_BUS}: {e}",
              file=sys.stderr)
        return 1
    agent = QdistroPolkitAgent(bus, AGENT_OBJ)  # noqa: F841
    try:
        _register(dbus.SystemBus(), AGENT_OBJ)
    except Exception as e:  # noqa: BLE001
        syslog.syslog(syslog.LOG_ERR, f"registration failed: {e}")
        print(f"qdistro-polkit-agent: registration failed: {e}",
              file=sys.stderr)
        return 1
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
