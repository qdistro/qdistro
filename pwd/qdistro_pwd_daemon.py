#!/usr/bin/env python3
"""qdistro password-manager daemon.

System-bus D-Bus service mediating access to encrypted vaults. Phase 8
MVP slice (spec/13): single password-encrypted vault per name, layered
caller-identity verification, per-item app-pin gate. Defers TPM,
fingerprint, recovery codes, multi-vault policy DSL, autotype, browser
bridge, SSH agent, portal.Secret backend.

Daemon runs as the dedicated `qdistro-pwd` system uid (created by
fresh-vm-bootstrap.sh / install-pwd-for-vm.sh). The vault directory
/var/lib/qdistro/vaults/ is owned 0700 by qdistro-pwd; only the daemon
domain can read .vault files (enforced by the SELinux module
qdistro_pwd.{te,fc} once shipped).

Bus name: `org.qdistro.Pwd1` on the system bus, object `/org/qdistro/Pwd1`.

Method matrix (admin uid = 1000):

    Admin-only:
      CreateVault(name, password) -> bool
      AddItem(vault, tag, value, pin_app_exe, pin_selinux, pin_uid) -> bool
      DeleteItem(vault, tag) -> bool
      ListItems(vault) -> aa{sv}
      GetItemAdmin(vault, tag) -> string         # bypasses app-pin gate
      ListAuditLog(limit) -> aa{sv}

    Any caller:
      ListVaults() -> as
      IsUnlocked(vault) -> bool
      UnlockVault(vault, password) -> bool       # admin-prompt-equivalent for MVP
      LockVault(vault) -> bool
      GetItem(vault, tag) -> string              # gated by per-item pin match

Audit log: every Get attempt (allow / deny), every Unlock / Lock /
admin-write op. Payload is never persisted.

Auto-lock: vaults relock after IDLE_TIMEOUT_S of no activity (default
10 minutes). Configurable via QDISTRO_PWD_IDLE_S env var.
"""
from __future__ import annotations

import json
import os
import pwd as _pwd_mod
import re
import secrets
import signal
import time
import urllib.parse
from typing import Any

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib
from qdistro_pwd_audit import PwdAuditLog  # type: ignore[import-not-found]
from qdistro_pwd_fprint import (  # type: ignore[import-not-found]
    admin_username as fprint_admin_username,
)
from qdistro_pwd_fprint import (
    is_fprintd_available as fprint_is_available,
)
from qdistro_pwd_fprint import (
    verify as fprint_verify,
)
from qdistro_pwd_identity import (  # type: ignore[import-not-found]
    pin_match,
    snapshot_caller,
)
from qdistro_pwd_pinstash import (  # type: ignore[import-not-found]
    DEFAULT_STASH_PATH as PORTAL_PIN_STASH_PATH,
)
from qdistro_pwd_pinstash import (
    MAX_PIN_BYTES as PORTAL_PIN_MAX_BYTES,
)
from qdistro_pwd_pinstash import (
    PinStashError,
)
from qdistro_pwd_pinstash import (
    stash_meta as portal_pin_meta,
)
from qdistro_pwd_pinstash import (
    stash_pin as portal_pin_stash,
)
from qdistro_pwd_pinstash import (
    unseal_pin as portal_pin_unseal,
)
from qdistro_pwd_polkit import (  # type: ignore[import-not-found]
    PolkitDenied,
    PolkitNoAgent,
)
from qdistro_pwd_polkit import (
    check_unlock as polkit_check_unlock,
)
from qdistro_pwd_tpm import (  # type: ignore[import-not-found]
    TpmAuthFailed,
    TpmBackendError,
    TpmUnavailable,
    configured_pcrs,
    lookup_backend,
    select_backend,
)
from qdistro_pwd_vault import (  # type: ignore[import-not-found]
    DEFAULT_VAULT_DIR,
    VAULT_FORMAT_VERSION_TPM,
    VaultBadPassword,
    VaultDuplicate,
    VaultIntegrityError,
    VaultNotFound,
    add_item,
    create_vault,
    create_vault_tpm,
    delete_item,
    get_item_payload,
    get_item_pins,
    get_tpm_seal_meta,
    list_items,
    list_vaults,
    rotate_vault,
    rotate_vault_tpm,
    unlock_vault,
    unlock_vault_tpm,
    vault_version,
)

BUS_NAME = "org.qdistro.Pwd1"
OBJ_PATH = "/org/qdistro/Pwd1"
try:
    ADMIN_UID = _pwd_mod.getpwnam("admin").pw_uid
except KeyError as e:
    raise RuntimeError("fixed admin user 'admin' does not exist") from e
if ADMIN_UID != 1000:
    raise RuntimeError(
        f"fixed admin user 'admin' must resolve to uid 1000, got {ADMIN_UID}")

VAULT_DIR = os.environ.get("QDISTRO_PWD_VAULT_DIR", DEFAULT_VAULT_DIR)
AUDIT_DB = os.environ.get(
    "QDISTRO_PWD_AUDIT_DB", "/var/lib/qdistro/audit/pwd_audit.sqlite")

# Vault used by the spec/13 Phase-8.3 XDG portal Secret backend to
# stash per-app-id master keys. Defaults to "portal-keys"; admin
# creates this vault before the portal backend can serve secrets.
PORTAL_KEYS_VAULT = os.environ.get("QDISTRO_PWD_PORTAL_VAULT",
                                   "portal-keys")
PORTAL_KEY_BYTES = 32  # length of bytes returned to portal callers

# Optional second-factor: gate AutoUnlockPortalKeys on a successful
# fprintd verify before unsealing the stashed PIN. Off by default so
# hosts without a fingerprint reader keep working unmodified. Set to
# "1" / "true" / "yes" to enable. When enabled and fprintd is absent
# OR no finger enrolled, the daemon hard-fails — operator must either
# enroll a finger or unset the env to recover. Set
# QDISTRO_PORTAL_FPRINT_OPTIONAL=1 alongside to soft-skip when fprintd
# is unreachable (useful for mixed fleets).
# Vault used for browser-managed passwords (Fill / Save / FillConfirm).
# Admin creates this vault before the browser bridge can store or retrieve
# credentials. Defaults to "passwords".
BROWSER_PWD_VAULT = os.environ.get("QDISTRO_PWD_BROWSER_VAULT", "passwords")
BROWSER_BRIDGE_SCRIPT = os.environ.get(
    "QDISTRO_PWD_BROWSER_BRIDGE_SCRIPT",
    "/usr/libexec/qdistro/qdistro_browser_bridge.py")
PORTAL_BACKEND_SCRIPT = os.environ.get(
    "QDISTRO_PWD_PORTAL_SCRIPT",
    "/usr/libexec/qdistro/qdistro_pwd_portal.py")
PORTAL_BACKEND_UNIT = os.environ.get(
    "QDISTRO_PWD_PORTAL_UNIT",
    "qdistro-pwd-portal.service")

# Trusted parent-browser exes for the bridge-identity gate. Resolved
# through the SAME shared module the bridge entry gate uses
# (``qdistro_browser_allowlist``, P0-4 follow-up) so this defense-in-depth
# gate cannot drift wider than the entry gate: the optional browsers
# (Chrome/Brave/Vivaldi/Edge) count as a valid bridge parent only when an
# admin has opted them in via the root-owned config; Firefox+Chromium is the
# always-trusted baseline.
#
# Imported defensively (mirroring the bridge's qdistro_proc_identity guard):
# if the module is absent the gate falls back to the Firefox+Chromium
# BASELINE — the narrowest, fail-closed set — never the historical full
# matrix. ``QDISTRO_PWD_BROWSER_PARENT_EXES``, when set, REPLACES the
# resolved set entirely (the historical test/non-RPM escape hatch).
try:
    import qdistro_browser_allowlist as _browser_allowlist  # type: ignore
except Exception:  # noqa: BLE001 — fail closed to the baseline if unavailable
    _browser_allowlist = None  # type: ignore[assignment]

_BROWSER_BASELINE_PARENT_EXES: tuple[str, ...] = (
    "/usr/lib64/firefox/firefox",
    "/usr/lib/firefox/firefox",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

_BROWSER_PARENT_EXES_ENV_OVERRIDE: tuple[str, ...] | None = (
    tuple(
        p for p in os.environ["QDISTRO_PWD_BROWSER_PARENT_EXES"].split(":")
        if p)
    if os.environ.get("QDISTRO_PWD_BROWSER_PARENT_EXES") is not None
    else None)


def _resolve_browser_parent_exes() -> tuple[str, ...]:
    """Effective trusted parent-browser exes for the pwd bridge gate.

    Env override (full replacement) wins; otherwise the shared module's
    baseline + admin opt-in; otherwise the Firefox+Chromium baseline,
    fail-closed. Read live at gate time so an opt-in config edit applies
    without restarting the daemon.
    """
    if _BROWSER_PARENT_EXES_ENV_OVERRIDE is not None:
        return _BROWSER_PARENT_EXES_ENV_OVERRIDE
    if _browser_allowlist is not None:
        return _browser_allowlist.resolve_parent_exes()
    try:
        import qdistro_browser_allowlist as allowlist  # type: ignore
    except Exception:  # noqa: BLE001 — still fail closed if unavailable
        return _BROWSER_BASELINE_PARENT_EXES
    globals()["_browser_allowlist"] = allowlist
    return allowlist.resolve_parent_exes()

PORTAL_REQUIRE_FPRINT = os.environ.get(
    "QDISTRO_PORTAL_REQUIRE_FPRINT", "0").lower() in ("1", "true", "yes")
PORTAL_FPRINT_OPTIONAL = os.environ.get(
    "QDISTRO_PORTAL_FPRINT_OPTIONAL", "0").lower() in ("1", "true", "yes")

# Vault auto-lock idle timeout. After this many seconds with no GetItem
# / IsUnlocked / list activity, the in-memory master key is wiped.
IDLE_TIMEOUT_S = int(os.environ.get("QDISTRO_PWD_IDLE_S", "600"))


class PwdPolicyError(dbus.DBusException):
    _dbus_error_name = "org.qdistro.Pwd1.PolicyError"


class PwdNotUnlocked(dbus.DBusException):
    _dbus_error_name = "org.qdistro.Pwd1.NotUnlocked"


class PwdBadPassword(dbus.DBusException):
    _dbus_error_name = "org.qdistro.Pwd1.BadPassword"


class PwdNotFound(dbus.DBusException):
    _dbus_error_name = "org.qdistro.Pwd1.NotFound"


class PwdDuplicate(dbus.DBusException):
    _dbus_error_name = "org.qdistro.Pwd1.Duplicate"


class PwdIntegrityError(dbus.DBusException):
    _dbus_error_name = "org.qdistro.Pwd1.Integrity"


def _wipe_bytearray(b: bytearray) -> None:
    """Best-effort overwrite of a sensitive byte buffer."""
    try:
        for i in range(len(b)):
            b[i] = 0
    except Exception:
        pass


def _normalize_url_origin(url: str) -> str:
    """Extract the origin (scheme://host[:port]) from a URL.

    Standard ports (80 for http, 443 for https) are omitted so that
    ``https://example.com:443/path`` normalises to ``https://example.com``
    and matches credentials stored without an explicit port.
    """
    # urlparse() itself raises ValueError on some malformed inputs (e.g. an
    # unterminated IPv6 literal "http://[::1"), and parsed.port raises on a
    # non-numeric / out-of-range port ("https://h:bad/", ":99999"). Fail
    # closed on either so the caller records an audited `bad-url-origin`
    # deny instead of crashing the RPC.
    try:
        parsed = urllib.parse.urlparse(url)
        scheme = (parsed.scheme or "https").lower()
        if scheme not in ("http", "https"):
            return ""
        host = (parsed.hostname or "").lower()
        if not host or "\x00" in host:
            return ""
        port = parsed.port
    except ValueError:
        return ""
    if port and not (
        (scheme == "https" and port == 443)
        or (scheme == "http" and port == 80)
    ):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def _normalize_cookie_origin(domain: str) -> str:
    """Normalise a cookie-export target to an http(s) origin.

    The browser bridge forwards either a full URL or a bare host as
    ``domain``.  Accept both, but fail closed on anything that is not
    plain http/https: an explicit non-http(s) scheme (``file:``,
    ``data:``, ``about:``, ``javascript:`` …) or a value that cannot be
    resolved to a hostname returns ``""`` so the caller rejects it.
    """
    if not isinstance(domain, str) or not domain or "\x00" in domain:
        return ""
    try:
        parsed = urllib.parse.urlparse(domain)
        scheme = parsed.scheme
        if scheme:
            # An explicit scheme must be http(s) — reject file:/data:/etc.
            if scheme.lower() not in ("http", "https"):
                return ""
            return _normalize_url_origin(domain)
        # No scheme: treat as a bare host. Re-parse with an https:// prefix
        # so urlparse populates .hostname (a bare string lands in .path).
        return _normalize_url_origin("https://" + domain)
    except ValueError:
        # urlparse / .port raise ValueError on malformed ports
        # (e.g. "host:bad", "host:99999"); fail closed.
        return ""


def _read_proc_cmdline(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read(16384)
    except OSError:
        return []
    return [p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p]


def _read_proc_ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _read_proc_exe(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def _read_proc_cgroup(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as f:
            paths = []
            for line in f:
                parts = line.rstrip("\n").split(":", 2)
                if len(parts) == 3:
                    paths.append(parts[2])
            return paths
    except OSError:
        return []


def _proc_in_systemd_unit(pid: int, unit_name: str) -> bool:
    if not unit_name:
        return False
    for path in _read_proc_cgroup(pid):
        if unit_name in [p for p in path.split("/") if p]:
            return True
    return False


def _browser_bridge_allowed(pid: int) -> tuple[bool, str]:
    """Verify that a browser-password RPC came from the native bridge.

    The system bus cannot restrict by executable. The bridge is installed
    as ``python3 /usr/libexec/qdistro/qdistro_browser_bridge.py`` and is
    launched by the browser's native-messaging host. Require both facts so
    a random same-UID Python process cannot call FillConfirm directly and
    satisfy the per-item pin stored by Save.

    The bridge script must be the *executed* script — the first non-flag
    argument after the interpreter (``python3 [opts] <script> ...``). A
    prior version accepted the script path anywhere in argv, so a hostile
    native host launched as ``python3 evil.py <bridge-script>`` (passing
    the real path as a data argument) would have impersonated the bridge.
    """
    cmdline = _read_proc_cmdline(pid)
    script_real = os.path.realpath(BROWSER_BRIDGE_SCRIPT)
    # Locate the executed script: skip the interpreter (argv[0]) and any
    # leading interpreter flags; the first non-flag token is the script
    # Python actually runs. Only a small allowlist of *valueless* flags is
    # tolerated before the script. Flags that consume the following token
    # as an operand (``-W spec``, ``-X opt``, ``-c cmd``, ``-m mod`` …)
    # would otherwise let an attacker smuggle the real script path into an
    # option value while Python actually executes a different file, e.g.
    # ``python3 -W <bridge-script> evil.py`` runs evil.py. Reject anything
    # not in the valueless allowlist so the parser can't be fooled.
    _VALUELESS_FLAGS = frozenset({
        "-b", "-bb", "-B", "-d", "-E", "-i", "-I", "-O", "-OO",
        "-q", "-s", "-S", "-u", "-v", "-vv", "-x",
    })
    executed_script = ""
    for arg in cmdline[1:]:
        if arg.startswith("-"):
            if arg in _VALUELESS_FLAGS:
                continue
            # Unknown / operand-consuming flag (incl. -c/-m/-W/-X): we
            # cannot safely locate the executed script. Fail closed.
            return False, "not-browser-bridge"
        executed_script = arg
        break
    if not executed_script or os.path.realpath(executed_script) != script_real:
        return False, "not-browser-bridge"
    ppid = _read_proc_ppid(pid)
    if ppid is None:
        return False, "parent-unreadable"
    parent_exe = _read_proc_exe(ppid)
    allowed = {os.path.realpath(p) for p in _resolve_browser_parent_exes()}
    if os.path.realpath(parent_exe) not in allowed:
        return False, "parent-not-browser"
    return True, "browser-bridge"


def _python_executed_script(pid: int) -> str:
    """Return the Python script path being executed by pid, or empty.

    We accept only simple valueless interpreter flags before the script.
    Flags such as -c, -m, -W, or -X can consume following operands or run
    code that is not the script path, so they fail closed.
    """
    cmdline = _read_proc_cmdline(pid)
    if len(cmdline) < 2:
        return ""
    valueless_flags = frozenset({
        "-b", "-bb", "-B", "-d", "-E", "-i", "-I", "-O", "-OO",
        "-q", "-s", "-S", "-u", "-v", "-vv", "-x",
    })
    for arg in cmdline[1:]:
        if arg.startswith("-"):
            if arg in valueless_flags:
                continue
            return ""
        return arg
    return ""


def _is_system_python_exe(path: str) -> bool:
    """Return true for the system Python interpreters used by script helpers."""
    real = os.path.realpath(path)
    directory = os.path.dirname(real)
    basename = os.path.basename(real)
    if directory not in ("/usr/bin", "/usr/local/bin"):
        return False
    return re.fullmatch(r"python(?:3(?:\.\d+)?)?", basename) is not None


def _portal_backend_allowed(pid: int) -> tuple[bool, str]:
    """Verify GetPortalKey is called by the installed portal backend."""
    if not _proc_in_systemd_unit(pid, PORTAL_BACKEND_UNIT):
        return False, "not-portal-backend-unit"
    portal_script = os.path.realpath(PORTAL_BACKEND_SCRIPT)
    exe = _read_proc_exe(pid)
    if not exe:
        return False, "not-portal-backend"
    exe_real = os.path.realpath(exe)
    if exe_real == portal_script:
        return True, "portal-backend"
    if not _is_system_python_exe(exe_real):
        return False, "not-portal-backend"
    script = _python_executed_script(pid)
    if not script or os.path.realpath(script) != portal_script:
        return False, "not-portal-backend"
    return True, "portal-backend"


def _bridge_app_id(pid: int) -> str:
    """Kernel-attested browser identity behind a bridge RPC, for audit.

    The bridge process's parent is the browser native-messaging host; its
    exe path is the strongest non-spoofable handle we have on *which
    browser* is driving the autofill request. Returned best-effort (empty
    on a race / unreadable proc) — used only for the audit row, never for
    a security decision (the gate is _browser_bridge_allowed)."""
    ppid = _read_proc_ppid(pid)
    if ppid is None:
        return ""
    return _read_proc_exe(ppid)


def _request_app_context(
        req: dict[str, Any],
        redact: str | None = None) -> tuple[str | None, str | None]:
    """Extract self-reported extension/browser identity + silo/app context
    from a browser-bridge request, for the audit row only.

    Returns (app_id, app_context). ``app_id`` is the self-reported
    extension id when present (clearly distinct from the kernel-attested
    parent-browser exe, which the caller stamps separately); ``app_context``
    is the silo/app label when the bridge forwards one. Both are advisory
    metadata — never a security input.

    ``redact`` (the submitted password, on the Save path) names a value
    that must NEVER be echoed into an audit field: any advisory string
    equal to it is dropped, so a hostile/buggy bridge cannot launder the
    password into audit via extension_id / silo / app_context."""
    if not isinstance(req, dict):
        return None, None

    def _clean(v: object) -> str | None:
        # Advisory, request-controlled metadata. Bound the length and drop
        # control chars so a hostile bridge cannot smuggle a large blob (or
        # a newline-laden credential dump) into an audit row through the
        # context fields. NB: we only ever read the extension_id / silo /
        # app_context keys — never `password` — so credential material does
        # not reach the audit row through this path.
        if not isinstance(v, str) or not v:
            return None
        cleaned = "".join(ch for ch in v if ch.isprintable())[:128].strip()
        if not cleaned:
            return None
        # Compare AFTER cleaning so " pw\n" (which cleans to the password)
        # is caught too — never persist the submitted password to audit.
        if redact and (v == redact or cleaned == redact):
            return None
        return cleaned

    ext = _clean(req.get("extension_id"))
    silo = _clean(req.get("silo")) or _clean(req.get("app_context"))
    app_id = f"ext:{ext}" if ext else None
    return app_id, silo


class PwdDaemon(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, OBJ_PATH)
        # vault name → {"key": bytearray, "unlocked_at": ts, "last_use": ts}
        self._unlocked: dict[str, dict[str, Any]] = {}
        self._audit = PwdAuditLog(AUDIT_DB)
        # Fill→FillConfirm binding: token → {origin, username, pid, expires}
        self._fill_tokens: dict[str, dict[str, Any]] = {}
        _FILL_TOKEN_TTL = 120  # seconds
        self._fill_token_ttl = _FILL_TOKEN_TTL
        # Idle tick: every 30s, relock vaults that have been idle for
        # more than IDLE_TIMEOUT_S.
        GLib.timeout_add_seconds(30, self._idle_tick)

    # -- helpers -------------------------------------------------------

    def _peer_info(self, sender: str) -> tuple[int, int]:
        bus = dbus.SystemBus()
        dbus_proxy = bus.get_object(
            "org.freedesktop.DBus", "/org/freedesktop/DBus")
        ifc = dbus.Interface(dbus_proxy, "org.freedesktop.DBus")
        uid = int(ifc.GetConnectionUnixUser(sender))
        pid = int(ifc.GetConnectionUnixProcessID(sender))
        return uid, pid

    def _require_admin(self, sender: str) -> None:
        uid, _ = self._peer_info(sender)
        if uid != ADMIN_UID:
            raise PwdPolicyError(
                f"operation requires admin uid {ADMIN_UID}, got {uid}")

    def _require_root(self, sender: str) -> None:
        """Gate for broker-only methods (the workflow engine runs as root
        inside the qbus-admin broker)."""
        uid, _ = self._peer_info(sender)
        if uid != 0:
            raise PwdPolicyError(
                f"operation requires root (broker), got uid {uid}")

    def _touch(self, vault: str) -> None:
        if vault in self._unlocked:
            self._unlocked[vault]["last_use"] = int(time.time())

    def _idle_tick(self) -> bool:
        now = int(time.time())
        relocked = []
        for name, state in list(self._unlocked.items()):
            if now - state["last_use"] >= IDLE_TIMEOUT_S:
                self._do_lock(name, reason="idle-timeout")
                relocked.append(name)
        for name in relocked:
            self.VaultLocked(name, "idle-timeout")
        return True  # reschedule

    def _do_lock(self, name: str, *, reason: str) -> bool:
        state = self._unlocked.pop(name, None)
        if state is None:
            return False
        key = state.get("key")
        if isinstance(key, bytearray):
            _wipe_bytearray(key)
        # Locking a vault invalidates every outstanding Fill→FillConfirm
        # approval that targets it. Tokens are an in-memory, session-scoped
        # grant; a relock (explicit, idle-timeout, or rotate) must not leave
        # a stale approval that a later re-unlock within the TTL would
        # silently honour. For the browser-pwd vault, drop all fill tokens.
        if name == BROWSER_PWD_VAULT:
            self._fill_tokens = {}
        self._audit.record(
            "lock", name, decision="allow", reason=reason)
        return True

    # -- vault lifecycle -----------------------------------------------

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="b",
                         sender_keyword="sender")
    def CreateVault(self, name: str, password: str, sender=None) -> bool:
        self._require_admin(sender)
        try:
            create_vault(VAULT_DIR, str(name), str(password).encode("utf-8"))
        except VaultDuplicate as e:
            self._audit.record("create", str(name),
                               decision="deny", reason=str(e))
            raise PwdDuplicate(str(e)) from e
        except ValueError as e:
            raise PwdPolicyError(str(e)) from e
        self._audit.record("create", str(name), decision="allow",
                           reason="vault created (v1/scrypt)")
        return True

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="b",
                         sender_keyword="sender")
    def CreateVaultTPM(self, name: str, pin: str, sender=None) -> bool:
        """Create a v2 TPM-sealed vault. PIN becomes the TPM auth-value
        (anti-DA-lockout enforced by the TPM). Backend selected per the
        QDISTRO_PWD_TPM_BACKEND env on the daemon."""
        self._require_admin(sender)
        backend = select_backend()
        if not backend.is_available():
            raise PwdPolicyError(
                f"TPM backend {backend.name!r} not available on this host")
        try:
            create_vault_tpm(VAULT_DIR, str(name),
                             str(pin).encode("utf-8"), backend,
                             pcrs=configured_pcrs())
        except VaultDuplicate as e:
            self._audit.record("create", str(name),
                               decision="deny", reason=str(e))
            raise PwdDuplicate(str(e)) from e
        except ValueError as e:
            raise PwdPolicyError(str(e)) from e
        except TpmUnavailable as e:
            raise PwdPolicyError(f"TPM unavailable: {e}") from e
        self._audit.record("create", str(name), decision="allow",
                           reason=f"vault created (v2/tpm:{backend.name})")
        return True

    @dbus.service.method(BUS_NAME, out_signature="as")
    def ListVaults(self) -> list[str]:
        return list_vaults(VAULT_DIR)

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="i")
    def VaultVersion(self, name: str) -> int:
        """Return the on-disk format version (1 = scrypt, 2 = TPM-sealed)
        so a CLI can route an UnlockVault call to the right secret kind
        (password vs PIN)."""
        try:
            return vault_version(VAULT_DIR, str(name))
        except VaultNotFound as e:
            raise PwdNotFound(str(e)) from e

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="a{sv}")
    def VaultInfo(self, name: str) -> dict:
        """Return a compact metadata blob: version, tpm backend (if v2)."""
        try:
            v = vault_version(VAULT_DIR, str(name))
        except VaultNotFound as e:
            raise PwdNotFound(str(e)) from e
        out: dict = {
            "version":     dbus.Int32(v),
            "tpm_backend": dbus.String(""),
            "tpm_pcrs":    dbus.String(""),
        }
        if v == VAULT_FORMAT_VERSION_TPM:
            seal = get_tpm_seal_meta(VAULT_DIR, str(name))
            out["tpm_backend"] = dbus.String(seal.get("backend", ""))
            out["tpm_pcrs"]    = dbus.String(seal.get("pcrs", ""))
        return out

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="b")
    def IsUnlocked(self, name: str) -> bool:
        return str(name) in self._unlocked

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="b",
                         sender_keyword="sender")
    def UnlockVault(self, name: str, secret: str, sender=None) -> bool:
        """Unlock a vault. Dispatches on the vault's on-disk version:
        v1 → secret is the scrypt password; v2 → secret is the TPM PIN.

        For non-admin callers, gated through the polkit action
        `org.qdistro.pwd.unlock` so an admin polkit agent prompts
        before any secret is unsealed. Admin uid bypasses polkit
        (caller is already authoritative).
        """
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        name = str(name)
        # Polkit gate — admin uid bypasses; non-admin must clear polkit
        # auth via a registered AuthenticationAgent before we even touch
        # the disk. This is the spec/13 Phase-8.2 entry point.
        try:
            allowed, polkit_reason = polkit_check_unlock(
                uid, pid, name, caller_exe=caller.get("exe", ""))
        except PolkitNoAgent as e:
            self._audit.record("unlock", name, decision="deny",
                               reason="polkit-no-agent", caller=caller)
            raise PwdPolicyError(
                f"polkit gate: no admin AuthenticationAgent registered "
                f"({e})") from e
        except PolkitDenied as e:
            self._audit.record("unlock", name, decision="deny",
                               reason="polkit-denied", caller=caller)
            raise PwdPolicyError(f"polkit gate refused: {e}") from e
        except dbus.DBusException as e:
            self._audit.record("unlock", name, decision="deny",
                               reason=f"polkit-dbus:{e.get_dbus_name()}",
                               caller=caller)
            raise PwdPolicyError(
                f"polkit gate unreachable: {e.get_dbus_message()}") from e
        if not allowed:
            self._audit.record("unlock", name, decision="deny",
                               reason=f"polkit:{polkit_reason}", caller=caller)
            raise PwdPolicyError(f"polkit gate refused: {polkit_reason}")
        try:
            v = vault_version(VAULT_DIR, name)
        except VaultNotFound as e:
            self._audit.record("unlock", name, decision="deny",
                               reason="no-such-vault", caller=caller)
            raise PwdNotFound(str(e)) from e
        try:
            if v == VAULT_FORMAT_VERSION_TPM:
                key = unlock_vault_tpm(VAULT_DIR, name,
                                       str(secret).encode("utf-8"),
                                       lookup_backend)
                unlock_reason = f"pin-ok-tpm/{polkit_reason}"
            else:
                key = unlock_vault(VAULT_DIR, name,
                                   str(secret).encode("utf-8"))
                unlock_reason = f"password-ok/{polkit_reason}"
        except VaultBadPassword as e:
            self._audit.record("unlock", name, decision="deny",
                               reason="bad-password", caller=caller)
            raise PwdBadPassword(str(e)) from e
        except VaultIntegrityError as e:
            self._audit.record("unlock", name, decision="deny",
                               reason=f"integrity:{e}", caller=caller)
            raise PwdIntegrityError(str(e)) from e
        # Stash the key in a bytearray so we can zero it on lock. The
        # original `bytes` is immutable; Python doesn't expose a way to
        # zero it, so the best we can do is hold the only mutable copy.
        ba = bytearray(key)
        now = int(time.time())
        self._unlocked[name] = {"key": ba, "unlocked_at": now, "last_use": now}
        self._audit.record("unlock", name, decision="allow",
                           reason=unlock_reason, caller=caller)
        self.VaultUnlocked(name)
        return True

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="b",
                         sender_keyword="sender")
    def UnlockVaultFprint(self, name: str, secret: str,
                          sender=None) -> bool:
        """Admin-only fast-path: fprintd verify + unseal in one
        round-trip.

        Same shape as UnlockVault (vault name + secret) but skips the
        polkit gate in favour of a direct fprintd VerifyStart cycle.
        Useful for app-driven dialogs that want to surface "Touch
        sensor" themselves rather than going through the polkit agent's
        prompt subprocess.

        Caller MUST be admin uid (fprintd is bound to a specific
        Linux user; cross-uid fprintd verification is intentionally
        unsupported here). The PIN/password is supplied by the caller —
        fprintd alone cannot derive the vault's master key, so this
        method is not a stand-alone factor; it's a faster *gate* than
        polkit while the actual unseal still runs through the v1/v2
        crypto path.

        On fprintd unreachable / no enrolled finger, raises
        ``PwdPolicyError`` (fail-closed). Wrong fingerprint raises
        ``PwdBadPassword`` so the caller's UI can offer a re-touch
        without distinguishing "no match" from "wrong PIN".
        """
        uid, pid = self._peer_info(sender)
        if uid != ADMIN_UID:
            raise PwdPolicyError(
                f"UnlockVaultFprint requires admin uid {ADMIN_UID}, "
                f"got {uid}")
        caller = snapshot_caller(pid, uid)
        name = str(name)
        sysbus = dbus.SystemBus()
        user = fprint_admin_username(ADMIN_UID)
        if not fprint_is_available(sysbus):
            self._audit.record("unlock", name, decision="deny",
                               reason="fprint-unavailable", caller=caller)
            raise PwdPolicyError(
                "fprintd unreachable or no enrolled fingerprint")
        matched, fpr_reason = fprint_verify(user, sysbus)
        if not matched:
            self._audit.record("unlock", name, decision="deny",
                               reason=f"fprint-deny:{fpr_reason}",
                               caller=caller)
            raise PwdBadPassword(
                f"fingerprint verification failed: {fpr_reason}")
        try:
            v = vault_version(VAULT_DIR, name)
        except VaultNotFound as e:
            self._audit.record("unlock", name, decision="deny",
                               reason="no-such-vault", caller=caller)
            raise PwdNotFound(str(e)) from e
        try:
            if v == VAULT_FORMAT_VERSION_TPM:
                key = unlock_vault_tpm(VAULT_DIR, name,
                                       str(secret).encode("utf-8"),
                                       lookup_backend)
                kind = "v2"
            else:
                key = unlock_vault(VAULT_DIR, name,
                                   str(secret).encode("utf-8"))
                kind = "v1"
        except VaultBadPassword as e:
            self._audit.record("unlock", name, decision="deny",
                               reason="bad-password", caller=caller)
            raise PwdBadPassword(str(e)) from e
        except VaultIntegrityError as e:
            self._audit.record("unlock", name, decision="deny",
                               reason=f"integrity:{e}", caller=caller)
            raise PwdIntegrityError(str(e)) from e
        ba = bytearray(key)
        now = int(time.time())
        self._unlocked[name] = {"key": ba, "unlocked_at": now,
                                "last_use": now}
        self._audit.record(
            "unlock", name, decision="allow",
            reason=f"fprint-pass:{fpr_reason}/{kind}",
            caller=caller)
        self.VaultUnlocked(name)
        return True

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="b",
                         sender_keyword="sender")
    def LockVault(self, name: str, sender=None) -> bool:
        uid, _ = self._peer_info(sender)
        # Locking is not gated on admin; any caller can lock (anyone can
        # ALWAYS lock more, just not unlock more — symmetric with file-
        # system unlinking-vs-creating).
        ok = self._do_lock(str(name), reason=f"explicit-lock-uid{uid}")
        if ok:
            self.VaultLocked(str(name), "explicit")
        return ok

    @dbus.service.method(BUS_NAME, in_signature="sss", out_signature="b",
                         sender_keyword="sender")
    def RotateVault(self, name: str, old_secret: str, new_secret: str,
                    sender=None) -> bool:
        """Rotate a vault's secret without re-encrypting items.

        Dispatches on the vault's on-disk version: v1 → password
        rotation (new salt + re-derived KEK); v2 → PIN rotation (re-seal
        master key under new PIN, also picks up the current PCR state).
        Items remain byte-for-byte unchanged because they are encrypted
        under the master key, not the password/PIN.
        Admin uid only.
        """
        self._require_admin(sender)
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        name = str(name)
        # Lock the in-memory copy (if any) before rotating — the master
        # key bytes don't change but downstream callers should re-unlock
        # under the new secret to reset the idle timer's lineage.
        was_unlocked = name in self._unlocked
        try:
            v = vault_version(VAULT_DIR, name)
        except VaultNotFound as e:
            self._audit.record("rotate", name, decision="deny",
                               reason="no-such-vault", caller=caller)
            raise PwdNotFound(str(e)) from e
        try:
            if v == VAULT_FORMAT_VERSION_TPM:
                backend = select_backend()
                if not backend.is_available():
                    raise PwdPolicyError(
                        f"TPM backend {backend.name!r} not available")
                rotate_vault_tpm(VAULT_DIR, name,
                                 str(old_secret).encode("utf-8"),
                                 str(new_secret).encode("utf-8"),
                                 backend, lookup_backend,
                                 pcrs=configured_pcrs())
                rotate_reason = f"pin-rotated-tpm/{backend.name}"
            else:
                rotate_vault(VAULT_DIR, name,
                             str(old_secret).encode("utf-8"),
                             str(new_secret).encode("utf-8"))
                rotate_reason = "password-rotated-scrypt"
        except VaultBadPassword as e:
            self._audit.record("rotate", name, decision="deny",
                               reason="bad-old-secret", caller=caller)
            raise PwdBadPassword(str(e)) from e
        except VaultIntegrityError as e:
            self._audit.record("rotate", name, decision="deny",
                               reason=f"integrity:{e}", caller=caller)
            raise PwdIntegrityError(str(e)) from e
        except TpmUnavailable as e:
            self._audit.record("rotate", name, decision="deny",
                               reason=f"tpm-unavailable:{e}", caller=caller)
            raise PwdPolicyError(f"TPM unavailable: {e}") from e
        if was_unlocked:
            # Force a fresh unlock under the new secret. The master key
            # bytes are unchanged but caller should re-prove the new
            # secret before further item access.
            self._do_lock(name, reason="rotated")
            self.VaultLocked(name, "rotated")
        self._audit.record("rotate", name, decision="allow",
                           reason=rotate_reason, caller=caller)
        return True

    # -- item management (admin) ---------------------------------------

    @dbus.service.method(BUS_NAME,
                         in_signature="ssssss",
                         out_signature="b",
                         sender_keyword="sender")
    def AddItem(self, vault: str, tag: str, value: str,
                pin_app_exe: str, pin_selinux: str, pin_uid: str = "",
                sender=None) -> bool:
        # Signature note: pin_uid is wire-typed as a string (D-Bus 's'),
        # not an int32 ('i'), because every documented caller passes
        # six strings (qdistro/pwd/README.md §"AddItem"). The empty
        # string is treated as "no uid pin" (-1 internally); any
        # non-empty value is parsed as a decimal int. A previous
        # iteration declared ``sssssis`` with a trailing reserved
        # string, which made the busctl/test call paths impossible to
        # write without a mid-signature int — see s105 in qdistro2
        # tests/integration/vm/.
        self._require_admin(sender)
        vault = str(vault)
        tag = str(tag)
        if vault not in self._unlocked:
            raise PwdNotUnlocked(f"vault {vault!r} is locked")
        self._touch(vault)
        pin_uid_s = str(pin_uid).strip()
        pin_uid_i = int(pin_uid_s) if pin_uid_s else -1
        try:
            add_item(VAULT_DIR, vault,
                     bytes(self._unlocked[vault]["key"]),
                     tag, str(value).encode("utf-8"),
                     pin_app_exe=str(pin_app_exe),
                     pin_selinux=str(pin_selinux),
                     pin_uid=(pin_uid_i if pin_uid_i >= 0 else None),
                     replace=True)
        except (VaultDuplicate, VaultNotFound, ValueError) as e:
            self._audit.record("add", vault, item_tag=tag,
                               decision="error", reason=str(e))
            raise PwdPolicyError(str(e)) from e
        self._audit.record("add", vault, item_tag=tag,
                           decision="allow", reason="item upserted")
        return True

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="b",
                         sender_keyword="sender")
    def DeleteItem(self, vault: str, tag: str, sender=None) -> bool:
        self._require_admin(sender)
        vault = str(vault)
        tag = str(tag)
        try:
            ok = delete_item(VAULT_DIR, vault, tag)
        except VaultNotFound as e:
            raise PwdNotFound(str(e)) from e
        self._audit.record("delete", vault, item_tag=tag,
                           decision="allow" if ok else "error",
                           reason="deleted" if ok else "absent")
        return ok

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="aa{sv}",
                         sender_keyword="sender")
    def ListItems(self, vault: str, sender=None) -> list[dict]:
        self._require_admin(sender)
        try:
            return [self._wrap_item_meta(it) for it in list_items(VAULT_DIR, str(vault))]
        except VaultNotFound as e:
            raise PwdNotFound(str(e)) from e

    @staticmethod
    def _wrap_item_meta(it: dict[str, Any]) -> dict[str, Any]:
        return {
            "tag":         dbus.String(it.get("tag", "")),
            "pin_app_exe": dbus.String(it.get("pin_app_exe", "")),
            "pin_selinux": dbus.String(it.get("pin_selinux", "")),
            "pin_uid":     dbus.Int32(int(it["pin_uid"]) if it.get("pin_uid") is not None else -1),
            "created":     dbus.Int64(int(it.get("created", 0))),
        }

    # -- get item (the load-bearing path) ------------------------------

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="s",
                         sender_keyword="sender")
    def GetItem(self, vault: str, tag: str, sender=None) -> str:
        """Read an item — gated by per-item app-pin match against the
        kernel-attested caller identity. Admin uid bypasses the pin
        gate via GetItemAdmin.
        """
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        vault = str(vault)
        tag = str(tag)
        if vault not in self._unlocked:
            self._audit.record("get", vault, item_tag=tag,
                               decision="deny", reason="vault-locked",
                               caller=caller)
            raise PwdNotUnlocked(f"vault {vault!r} is locked")
        self._touch(vault)
        try:
            pins = get_item_pins(VAULT_DIR, vault, tag)
        except VaultNotFound as e:
            self._audit.record("get", vault, item_tag=tag,
                               decision="deny", reason="no-such-item",
                               caller=caller)
            raise PwdNotFound(str(e)) from e
        ok, reason = pin_match(pins, caller)
        if not ok:
            self._audit.record("get", vault, item_tag=tag,
                               decision="deny", reason=reason,
                               caller=caller)
            raise PwdPolicyError(f"pin gate refused: {reason}")
        try:
            payload = get_item_payload(
                VAULT_DIR, vault, bytes(self._unlocked[vault]["key"]), tag)
        except VaultIntegrityError as e:
            self._audit.record("get", vault, item_tag=tag,
                               decision="deny", reason="integrity-fail",
                               caller=caller)
            raise PwdIntegrityError(str(e)) from e
        self._audit.record("get", vault, item_tag=tag,
                           decision="allow", reason=reason, caller=caller)
        return payload.decode("utf-8")

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="s",
                         sender_keyword="sender")
    def GetItemAdmin(self, vault: str, tag: str, sender=None) -> str:
        """Admin-only retrieval that bypasses the per-item pin gate. Used
        by the admin CLI for inspection and recovery."""
        self._require_admin(sender)
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        vault = str(vault)
        tag = str(tag)
        if vault not in self._unlocked:
            raise PwdNotUnlocked(f"vault {vault!r} is locked")
        self._touch(vault)
        try:
            payload = get_item_payload(
                VAULT_DIR, vault, bytes(self._unlocked[vault]["key"]), tag)
        except VaultNotFound as e:
            raise PwdNotFound(str(e)) from e
        except VaultIntegrityError as e:
            raise PwdIntegrityError(str(e)) from e
        self._audit.record("get-admin", vault, item_tag=tag,
                           decision="allow", reason="admin-bypass",
                           caller=caller)
        return payload.decode("utf-8")

    @dbus.service.method(BUS_NAME, in_signature="sss", out_signature="s",
                         sender_keyword="sender")
    def DeliverToWorkflow(self, vault: str, tag: str, run_id: str,
                          sender=None) -> str:
        """Release an unsealed item to the workflow engine for a run.

        The caller is the qbus-admin broker (running as root), which then
        hands the secret to the consuming task through a narrow,
        scrubbed channel (ssh-agent / env / fd-pass / tmpfs). Restricted
        to uid 0 server-side AND at the bus level so a non-root uid can't
        invoke it. The per-item app-pin gate is intentionally bypassed:
        the broker — not the ultimate consumer — is the D-Bus peer, and
        the broker owns the delivery + scrub lifecycle (see
        permissions.md §"Secret delivery to privileged tasks").

        The secret transits only this private system-bus reply. Only
        delivery *metadata* (vault, tag, run_id) is audited — never the
        secret value — and a SecretDeliveredToWorkflow signal is emitted
        for the workflow audit chain.
        """
        self._require_root(sender)
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        vault = str(vault)
        tag = str(tag)
        run_id = str(run_id)
        # Defense-in-depth: the broker stamps every delivery with the live
        # WorkflowRun id. Reject a missing/over-long/garbage run_id so a
        # forged, blank, or payload-bearing value can't be recorded in the
        # audit chain as a legitimate release.
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", run_id):
            self._audit.record("deliver-workflow", vault, item_tag=tag,
                               decision="deny", reason="bad-run-id",
                               caller=caller)
            raise PwdPolicyError(f"invalid workflow run_id {run_id!r}")
        if vault not in self._unlocked:
            self._audit.record("deliver-workflow", vault, item_tag=tag,
                               decision="deny", reason="vault-locked",
                               caller=caller)
            raise PwdNotUnlocked(f"vault {vault!r} is locked")
        self._touch(vault)
        try:
            payload = get_item_payload(
                VAULT_DIR, vault, bytes(self._unlocked[vault]["key"]), tag)
        except VaultNotFound as e:
            self._audit.record("deliver-workflow", vault, item_tag=tag,
                               decision="deny", reason="no-such-item",
                               caller=caller)
            raise PwdNotFound(str(e)) from e
        except VaultIntegrityError as e:
            self._audit.record("deliver-workflow", vault, item_tag=tag,
                               decision="deny", reason="integrity-fail",
                               caller=caller)
            raise PwdIntegrityError(str(e)) from e
        self._audit.record("deliver-workflow", vault, item_tag=tag,
                           decision="allow", reason=f"run={run_id}",
                           caller=caller)
        self.SecretDeliveredToWorkflow(run_id, vault, tag)
        return payload.decode("utf-8")

    # -- browser-bridge Fill / Save / FillConfirm ----------------------
    #
    # These methods are called by qdistro-browser-bridge on behalf of the
    # browser extension.  Fill returns credential metadata (no passwords)
    # for a URL; FillConfirm retrieves the actual password after a user
    # pick; Save stores or updates a credential.
    #
    # Items are stored in the BROWSER_PWD_VAULT vault with tag format
    # ``pwd:<origin>/<username>`` so they're grep-able for admin tooling.

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="s",
                         sender_keyword="sender")
    def Fill(self, creds_json: str, sender=None) -> str:
        """Look up matching credentials for a URL. Returns JSON with
        credential metadata (username, url) but NOT the password.
        The extension must call FillConfirm to get the password after
        the user picks a credential."""
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        vault = BROWSER_PWD_VAULT
        # Structured audit context, enriched as the request is parsed.
        # `app_id` is the kernel-attested parent-browser exe (best-effort);
        # origin/extension/silo are layered in below. Stamped on every
        # Fill audit row — allow AND deny — so a denial is investigatable.
        actx: dict[str, Any] = {"app_id": _bridge_app_id(pid) or None}
        bridge_ok, bridge_reason = _browser_bridge_allowed(pid)
        if not bridge_ok:
            self._audit.record("fill", vault, decision="deny",
                               reason=f"bridge-caller:{bridge_reason}",
                               caller=caller, **actx)
            return json.dumps({"ok": False, "error": "policy_denied"})
        try:
            req = json.loads(str(creds_json))
        except (json.JSONDecodeError, TypeError) as e:
            self._audit.record("fill", vault, decision="deny",
                               reason=f"invalid-json:{e}", caller=caller,
                               **actx)
            return json.dumps({"ok": False, "error": "invalid_request"})
        if not isinstance(req, dict):
            # json.loads accepts non-objects ([], "x", 123, null); a later
            # req.get() would raise — fail closed and audit (matches the
            # ExportCookies guard).
            self._audit.record("fill", vault, decision="deny",
                               reason="non-object-json", caller=caller,
                               **actx)
            return json.dumps({"ok": False, "error": "invalid_request"})

        # Layer self-reported extension id / silo context onto the
        # attested browser identity (extension id keeps its own ext: tag,
        # so the audit reader can tell attested from self-reported).
        ext_app_id, app_context = _request_app_context(req)
        if ext_app_id and actx.get("app_id"):
            actx["app_id"] = f"{actx['app_id']} {ext_app_id}"
        elif ext_app_id:
            actx["app_id"] = ext_app_id
        actx["app_context"] = app_context

        url = req.get("url")
        if not isinstance(url, str) or not url:
            self._audit.record("fill", vault, decision="deny",
                               reason="missing-url", caller=caller, **actx)
            return json.dumps({"ok": False, "error": "invalid_request"})

        origin = _normalize_url_origin(url)
        if not origin:
            self._audit.record("fill", vault, decision="deny",
                               reason="bad-url-origin", caller=caller, **actx)
            return json.dumps({"ok": False, "error": "invalid_request"})
        actx["origin"] = origin

        username_filter = req.get("username") or None

        if vault not in self._unlocked:
            self._audit.record("fill", vault, decision="deny",
                               reason="vault-locked", caller=caller, **actx)
            return json.dumps({"ok": False, "error": "vault_locked"})
        self._touch(vault)

        # Scan items for matching pwd:<origin>/* tags
        try:
            items = list_items(VAULT_DIR, vault)
        except VaultNotFound:
            self._audit.record("fill", vault, decision="deny",
                               reason="vault-missing", caller=caller, **actx)
            return json.dumps({"ok": False, "error": "vault_locked"})

        tag_prefix = f"pwd:{origin}/"
        matches = []
        for it in items:
            tag = it.get("tag", "")
            if not tag.startswith(tag_prefix):
                continue
            item_username = tag[len(tag_prefix):]
            if username_filter and item_username != username_filter:
                continue
            # Verify caller identity against item pins
            pins = {
                "pin_app_exe": it.get("pin_app_exe", ""),
                "pin_selinux": it.get("pin_selinux", ""),
                "pin_uid": it.get("pin_uid"),
            }
            # Items with no pins set are admin-only (pin_match returns
            # False); skip them silently for the browser fill flow.
            ok, reason = pin_match(pins, caller)
            if not ok:
                continue
            matches.append({
                "username": item_username,
                "url": origin,
            })

        if not matches:
            self._audit.record("fill", vault, decision="allow",
                               reason="no-match", caller=caller, **actx)
            return json.dumps({"ok": False, "error": "no_match"})

        fill_token = secrets.token_urlsafe(32)
        now = int(time.time())
        if not hasattr(self, "_fill_tokens"):
            self._fill_tokens = {}
        self._fill_tokens = {
            k: v for k, v in self._fill_tokens.items()
            if v["expires"] > now
        }
        ttl = getattr(self, "_fill_token_ttl", 120)
        for m in matches:
            self._fill_tokens[fill_token + ":" + m["username"]] = {
                "origin": origin,
                "username": m["username"],
                "pid": pid,
                "expires": now + ttl,
            }

        self._audit.record("fill", vault, decision="allow",
                           reason=f"matched:{len(matches)}", caller=caller,
                           **actx)
        return json.dumps({
            "ok": True,
            "credentials": matches,
            "fill_token": fill_token,
        })

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="s",
                         sender_keyword="sender")
    def FillConfirm(self, creds_json: str, sender=None) -> str:
        """Second step of fill: retrieve the actual password for a
        credential the user picked from the Fill list."""
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        vault = BROWSER_PWD_VAULT
        actx: dict[str, Any] = {"app_id": _bridge_app_id(pid) or None}
        bridge_ok, bridge_reason = _browser_bridge_allowed(pid)
        if not bridge_ok:
            self._audit.record("fill-confirm", vault, decision="deny",
                               reason=f"bridge-caller:{bridge_reason}",
                               caller=caller, **actx)
            return json.dumps({"ok": False, "error": "policy_denied"})
        try:
            req = json.loads(str(creds_json))
        except (json.JSONDecodeError, TypeError) as e:
            self._audit.record("fill-confirm", vault, decision="deny",
                               reason=f"invalid-json:{e}", caller=caller,
                               **actx)
            return json.dumps({"ok": False, "error": "invalid_request"})
        if not isinstance(req, dict):
            self._audit.record("fill-confirm", vault, decision="deny",
                               reason="non-object-json", caller=caller,
                               **actx)
            return json.dumps({"ok": False, "error": "invalid_request"})

        ext_app_id, app_context = _request_app_context(req)
        if ext_app_id and actx.get("app_id"):
            actx["app_id"] = f"{actx['app_id']} {ext_app_id}"
        elif ext_app_id:
            actx["app_id"] = ext_app_id
        actx["app_context"] = app_context

        url = req.get("url")
        username = req.get("username")
        fill_token = req.get("fill_token")
        if not (isinstance(url, str) and url
                and isinstance(username, str) and username):
            self._audit.record("fill-confirm", vault, decision="deny",
                               reason="missing-fields", caller=caller, **actx)
            return json.dumps({"ok": False, "error": "invalid_request"})

        origin = _normalize_url_origin(url)
        if not origin:
            self._audit.record("fill-confirm", vault, decision="deny",
                               reason="bad-url-origin", caller=caller, **actx)
            return json.dumps({"ok": False, "error": "invalid_request"})
        actx["origin"] = origin

        if vault not in self._unlocked:
            self._audit.record("fill-confirm", vault, decision="deny",
                               reason="vault-locked", caller=caller, **actx)
            return json.dumps({"ok": False, "error": "vault_locked"})
        self._touch(vault)

        token_key = f"{fill_token}:{username}" if fill_token else ""
        if not hasattr(self, "_fill_tokens"):
            self._fill_tokens = {}
        # Peek (do NOT pop yet): an identity-binding mismatch must not
        # let a wrong/forged caller burn the legitimate peer's pending
        # approval (that would be a DoS — the genuine bridge could no
        # longer redeem it). The token is consumed only once all bound
        # fields match, immediately before we release the secret.
        token_entry = self._fill_tokens.get(token_key)
        if not token_entry or token_entry["expires"] < int(time.time()):
            # An expired entry is dead weight — drop it.
            self._fill_tokens.pop(token_key, None)
            self._audit.record("fill-confirm", vault, decision="deny",
                               reason="invalid-or-expired-fill-token",
                               caller=caller, **actx)
            return json.dumps({"ok": False, "error": "invalid_token"})
        if (token_entry["origin"] != origin
                or token_entry["username"] != username
                or token_entry.get("pid") != pid):
            self._audit.record("fill-confirm", vault, decision="deny",
                               reason="fill-token-mismatch", caller=caller,
                               **actx)
            return json.dumps({"ok": False, "error": "invalid_token"})
        # Daemon-session / originating-peer binding: the fill_token was
        # minted for the bridge process (pid) that called Fill. A
        # different process — even one that satisfies the per-item pin
        # set (same exe/uid) — must NOT be able to consume another
        # peer's approval. Bind the confirm to the originating peer pid
        # captured at Fill time so the token is single-target, not just
        # single-use. (pin_match below still re-validates exe/selinux/uid
        # of the live caller; this is the orthogonal session axis.)
        if token_entry.get("pid") != pid:
            self._audit.record("fill-confirm", vault, decision="deny",
                               reason="fill-token-peer-mismatch",
                               caller=caller, **actx)
            return json.dumps({"ok": False, "error": "invalid_token"})
        # All bound fields match — consume the single-use approval now.
        self._fill_tokens.pop(token_key, None)

        tag = f"pwd:{origin}/{username}"
        # Verify caller identity against item pins
        try:
            pins = get_item_pins(VAULT_DIR, vault, tag)
        except VaultNotFound:
            self._audit.record("fill-confirm", vault, item_tag=tag,
                               decision="deny", reason="no-such-item",
                               caller=caller, **actx)
            return json.dumps({"ok": False, "error": "no_match"})

        ok, reason = pin_match(pins, caller)
        if not ok:
            self._audit.record("fill-confirm", vault, item_tag=tag,
                               decision="deny", reason=reason,
                               caller=caller, **actx)
            return json.dumps({"ok": False, "error": "policy_denied"})

        try:
            payload = get_item_payload(
                VAULT_DIR, vault, bytes(self._unlocked[vault]["key"]), tag)
        except VaultIntegrityError:
            self._audit.record("fill-confirm", vault, item_tag=tag,
                               decision="deny", reason="integrity-fail",
                               caller=caller, **actx)
            return json.dumps({"ok": False, "error": "integrity_error"})
        except VaultNotFound:
            self._audit.record("fill-confirm", vault, item_tag=tag,
                               decision="deny", reason="no-such-item",
                               caller=caller, **actx)
            return json.dumps({"ok": False, "error": "no_match"})

        try:
            password_str = payload.decode("utf-8")
        except UnicodeDecodeError:
            self._audit.record("fill-confirm", vault, item_tag=tag,
                               decision="deny", reason="decode-error",
                               caller=caller, **actx)
            return json.dumps({"ok": False, "error": "integrity_error"})

        self._audit.record("fill-confirm", vault, item_tag=tag,
                           decision="allow", reason=reason, caller=caller,
                           **actx)
        return json.dumps({
            "ok": True,
            "credentials": [{
                "username": username,
                "password": password_str,
                "url": origin,
            }],
        })

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="s",
                         sender_keyword="sender")
    def Save(self, creds_json: str, sender=None) -> str:
        """Store or update a browser-managed credential."""
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        vault = BROWSER_PWD_VAULT
        actx: dict[str, Any] = {"app_id": _bridge_app_id(pid) or None}
        bridge_ok, bridge_reason = _browser_bridge_allowed(pid)
        if not bridge_ok:
            self._audit.record("save", vault, decision="deny",
                               reason=f"bridge-caller:{bridge_reason}",
                               caller=caller, **actx)
            return json.dumps({"ok": False, "error": "policy_denied"})
        try:
            req = json.loads(str(creds_json))
        except (json.JSONDecodeError, TypeError) as e:
            self._audit.record("save", vault, decision="deny",
                               reason=f"invalid-json:{e}", caller=caller,
                               **actx)
            return json.dumps({"ok": False, "error": "invalid_request"})
        if not isinstance(req, dict):
            self._audit.record("save", vault, decision="deny",
                               reason="non-object-json", caller=caller,
                               **actx)
            return json.dumps({"ok": False, "error": "invalid_request"})

        url = req.get("url")
        username = req.get("username")
        password = req.get("password")
        # Self-reported extension/silo context for the audit row. NB:
        # _request_app_context only reads extension_id / silo / app_context
        # keys — never `password` — so credential material can't leak into
        # the audit row through that path. Save is the one autofill RPC that
        # *does* see the password, so as belt-and-suspenders we also redact
        # any advisory-context value that mirrors the submitted password:
        # a hostile/buggy bridge that copies the password into extension_id
        # or silo must not get it persisted to audit (proven in
        # test_pwd_autofill_audit::...advisory_context_password_redacted).
        ext_app_id, app_context = _request_app_context(
            req, redact=password if isinstance(password, str) else None)
        if ext_app_id and actx.get("app_id"):
            actx["app_id"] = f"{actx['app_id']} {ext_app_id}"
        elif ext_app_id:
            actx["app_id"] = ext_app_id
        actx["app_context"] = app_context

        if not (isinstance(url, str) and url
                and isinstance(username, str) and username
                and isinstance(password, str) and password):
            self._audit.record("save", vault, decision="deny",
                               reason="missing-fields", caller=caller, **actx)
            return json.dumps({"ok": False, "error": "invalid_request"})

        origin = _normalize_url_origin(url)
        if not origin:
            self._audit.record("save", vault, decision="deny",
                               reason="bad-url-origin", caller=caller, **actx)
            return json.dumps({"ok": False, "error": "invalid_request"})
        actx["origin"] = origin

        if vault not in self._unlocked:
            self._audit.record("save", vault, decision="deny",
                               reason="vault-locked", caller=caller, **actx)
            return json.dumps({"ok": False, "error": "vault_locked"})
        self._touch(vault)

        tag = f"pwd:{origin}/{username}"
        # Audit-only tag: item_tag is stored in cleartext (admins need to
        # know which credential was touched), but a hostile/buggy bridge
        # could set username == password to launder the secret through the
        # tag. Mask the username in the *audited* tag when it equals the
        # submitted password; the real `tag` used for storage/lookup is
        # untouched. (Proven in
        # test_pwd_autofill_audit::...username_equal_password_redacted.)
        audit_tag = (f"pwd:{origin}/<redacted>"
                     if username == password else tag)
        master_key = bytes(self._unlocked[vault]["key"])

        # Check if credential already exists — if so, verify caller
        # identity against existing pins before allowing an update.
        try:
            existing_pins = get_item_pins(VAULT_DIR, vault, tag)
            ok, reason = pin_match(existing_pins, caller)
            if not ok:
                self._audit.record("save", vault, item_tag=audit_tag,
                                   decision="deny", reason=reason,
                                   caller=caller, **actx)
                return json.dumps({"ok": False, "error": "policy_denied"})
        except VaultNotFound:
            pass  # new credential — no existing pins to check

        # Store with app-identity pins derived from the kernel-attested
        # caller identity, not the self-reported parent_exe from the request.
        try:
            add_item(VAULT_DIR, vault, master_key,
                     tag, password.encode("utf-8"),
                     pin_app_exe=caller.get("exe", ""),
                     # snapshot_caller() exposes the SELinux context under
                     # "selinux_label" (see qdistro_pwd_identity); a stale
                     # "selinux" key silently dropped the pin so browser-
                     # saved creds were never SELinux-bound. Fail-closed fix.
                     pin_selinux=caller.get("selinux_label", ""),
                     pin_uid=uid,
                     replace=True)
        except (VaultDuplicate, VaultNotFound, ValueError) as e:
            self._audit.record("save", vault, item_tag=audit_tag,
                               decision="error", reason=str(e),
                               caller=caller, **actx)
            return json.dumps({"ok": False, "error": "invalid_request"})

        self._audit.record("save", vault, item_tag=audit_tag,
                           decision="allow", reason="saved",
                           caller=caller, **actx)
        return json.dumps({"ok": True})

    # -- browser-bridge ExportCookies (Bridge Phase 9d) ----------------
    #
    # The browser bridge forwards a sensitive "Export session cookies"
    # action here.  The cookies are the browser's own session cookies;
    # the daemon does NOT mint or store them — its job is to be the
    # kernel-attested, audited, fail-closed gate for the operation.
    #
    # Replay / web-triggered-call defense (the intent-token check) lives
    # in the bridge (verify_intent_token in qdistro_browser_bridge.py):
    # the token is consumed there and is NOT forwarded on the D-Bus call,
    # so the daemon never sees it.  The daemon re-verifies the caller is
    # the genuine native bridge (SO_PEERCRED uid/pid + /proc/<pid>/exe +
    # parent-is-browser, same as Fill/Save) and audits every export with
    # origin + cookie count — never cookie values.

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="s",
                         sender_keyword="sender")
    def ExportCookies(self, req_json: str, sender=None) -> str:
        """Audit-logged, kernel-attested gate for a browser session
        cookie export.  Returns JSON ``{"ok": True, "exported": <n>}``
        on success or ``{"ok": False, "error": <code>}`` on failure.

        Cookie values are never logged; only the origin and the count.
        """
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        # Audit under the browser-pwd vault name so export rows live in
        # the same audit stream as Fill/Save (no vault is read/written).
        vault = BROWSER_PWD_VAULT
        bridge_ok, bridge_reason = _browser_bridge_allowed(pid)
        if not bridge_ok:
            self._audit.record("cookies-export", vault, decision="deny",
                               reason=f"bridge-caller:{bridge_reason}",
                               caller=caller)
            return json.dumps({"ok": False, "error": "policy_denied"})
        try:
            req = json.loads(str(req_json))
        except (json.JSONDecodeError, TypeError) as e:
            self._audit.record("cookies-export", vault, decision="deny",
                               reason=f"invalid-json:{e}", caller=caller)
            return json.dumps({"ok": False, "error": "invalid_request"})
        if not isinstance(req, dict):
            # json.loads accepts non-objects ([], "x", 123, null); a
            # later req.get() would raise — fail closed and audit.
            self._audit.record("cookies-export", vault, decision="deny",
                               reason="non-object-json", caller=caller)
            return json.dumps({"ok": False, "error": "invalid_request"})

        domain = req.get("domain")
        if not isinstance(domain, str) or not domain:
            self._audit.record("cookies-export", vault, decision="deny",
                               reason="missing-domain", caller=caller)
            return json.dumps({"ok": False, "error": "invalid_request"})

        origin = _normalize_cookie_origin(domain)
        if not origin:
            # Rejects file:/data:/about: and other non-http(s) schemes.
            self._audit.record("cookies-export", vault, decision="deny",
                               reason="bad-origin", caller=caller)
            return json.dumps({"ok": False, "error": "invalid_request"})

        cookies = req.get("cookies")
        if not isinstance(cookies, list):
            self._audit.record("cookies-export", vault, item_tag=origin,
                               decision="deny", reason="bad-cookies",
                               caller=caller)
            return json.dumps({"ok": False, "error": "invalid_request"})

        count = len(cookies)
        # Audit the export: who (caller), when (ts), origin, cookie count.
        # Cookie names/values are deliberately NOT recorded.
        self._audit.record("cookies-export", vault, item_tag=origin,
                           decision="allow", reason=f"count:{count}",
                           caller=caller)
        return json.dumps({"ok": True, "exported": count})

    # -- portal-keys PIN stash + auto-unlock (spec/13 §"auto-unlock") ---
    #
    # Admin sets the portal-keys vault PIN once via
    # `qdistro-pwd-admin store-portal-pin`; the daemon TPM-seals it
    # and writes the sealed blob to /var/lib/qdistro/portal-keys-pin.tpm.
    # A session systemd unit calls AutoUnlockPortalKeys at login so
    # unmodified Flatpak apps can immediately fetch portal Secret keys.

    @dbus.service.method(BUS_NAME, in_signature="ay", out_signature="b",
                         sender_keyword="sender")
    def StashPortalPin(self, pin, sender=None) -> bool:
        """Admin-only: TPM-seal the portal-keys vault PIN onto disk for
        later auto-unlock."""
        self._require_admin(sender)
        pin_bytes = bytes(bytearray(int(b) for b in pin))
        if not pin_bytes:
            raise PwdPolicyError("portal-keys PIN must be non-empty")
        if len(pin_bytes) > PORTAL_PIN_MAX_BYTES:
            raise PwdPolicyError(
                f"portal-keys PIN too long ({len(pin_bytes)} bytes; "
                f"max {PORTAL_PIN_MAX_BYTES})")
        backend = select_backend()
        if not backend.is_available():
            raise PwdPolicyError(
                f"TPM backend {backend.name!r} not available — "
                f"portal-keys PIN stash needs a working TPM")
        try:
            meta = portal_pin_stash(pin_bytes, backend,
                                    path=PORTAL_PIN_STASH_PATH)
        except (TpmUnavailable, ValueError) as e:
            raise PwdPolicyError(f"PIN stash failed: {e}") from e
        self._audit.record("portal-pin-stash", PORTAL_KEYS_VAULT,
                           decision="allow",
                           reason=f"sealed via {meta['backend']}")
        return True

    @dbus.service.method(BUS_NAME, out_signature="a{sv}",
                         sender_keyword="sender")
    def PortalPinStashInfo(self, sender=None) -> dict:
        """Admin-only: return stash metadata (no unseal)."""
        self._require_admin(sender)
        meta = portal_pin_meta(PORTAL_PIN_STASH_PATH)
        return {
            "present":         dbus.Boolean(bool(meta["present"])),
            "backend":         dbus.String(meta["backend"]),
            "created_at_unix": dbus.Int64(int(meta["created_at_unix"])),
            "stash_path":      dbus.String(PORTAL_PIN_STASH_PATH),
        }

    @dbus.service.method(BUS_NAME, out_signature="b",
                         sender_keyword="sender")
    def AutoUnlockPortalKeys(self, sender=None) -> bool:
        """Admin-only: unseal the stashed PIN + unlock the portal-keys
        vault. Idempotent — returns True if already unlocked.

        When ``QDISTRO_PORTAL_REQUIRE_FPRINT=1``, requires a fprintd
        verify cycle (touch the sensor) BEFORE unsealing the PIN. This
        shifts the load-bearing factor from "stashed PIN alone" to
        "stashed PIN + live presence", at the cost of needing a touch
        at every login. Set ``QDISTRO_PORTAL_FPRINT_OPTIONAL=1`` to
        soft-skip when fprintd is unreachable (mixed-fleet recovery).
        """
        self._require_admin(sender)
        if PORTAL_KEYS_VAULT in self._unlocked:
            return True
        if PORTAL_REQUIRE_FPRINT:
            sysbus = dbus.SystemBus()
            user = fprint_admin_username(ADMIN_UID)
            if not fprint_is_available(sysbus):
                if PORTAL_FPRINT_OPTIONAL:
                    self._audit.record(
                        "portal-auto-unlock", PORTAL_KEYS_VAULT,
                        decision="allow",
                        reason="fprint-skip:fprintd-unreachable")
                else:
                    self._audit.record(
                        "portal-auto-unlock", PORTAL_KEYS_VAULT,
                        decision="deny",
                        reason="fprint-required:fprintd-unreachable")
                    raise PwdPolicyError(
                        "fprintd unreachable but "
                        "QDISTRO_PORTAL_REQUIRE_FPRINT=1")
            else:
                matched, fpr_reason = fprint_verify(user, sysbus)
                if not matched:
                    self._audit.record(
                        "portal-auto-unlock", PORTAL_KEYS_VAULT,
                        decision="deny",
                        reason=f"fprint-deny:{fpr_reason}")
                    raise PwdBadPassword(
                        f"fingerprint verification failed: {fpr_reason}")
                self._audit.record(
                    "portal-auto-unlock", PORTAL_KEYS_VAULT,
                    decision="allow",
                    reason=f"fprint-pass:{fpr_reason}")
        try:
            pin = portal_pin_unseal(lookup_backend,
                                    path=PORTAL_PIN_STASH_PATH)
        except PinStashError as e:
            self._audit.record("portal-auto-unlock", PORTAL_KEYS_VAULT,
                               decision="deny", reason=f"stash:{e}")
            raise PwdNotFound(str(e)) from e
        except TpmAuthFailed as e:
            self._audit.record("portal-auto-unlock", PORTAL_KEYS_VAULT,
                               decision="deny", reason=f"tpm-auth:{e}")
            raise PwdBadPassword(str(e)) from e
        except TpmBackendError as e:
            self._audit.record("portal-auto-unlock", PORTAL_KEYS_VAULT,
                               decision="deny", reason=f"tpm-error:{e}")
            raise PwdIntegrityError(str(e)) from e
        try:
            v = vault_version(VAULT_DIR, PORTAL_KEYS_VAULT)
            if v == VAULT_FORMAT_VERSION_TPM:
                key = unlock_vault_tpm(VAULT_DIR, PORTAL_KEYS_VAULT,
                                       pin, lookup_backend)
                kind = "v2"
            else:
                key = unlock_vault(VAULT_DIR, PORTAL_KEYS_VAULT, pin)
                kind = "v1"
        except VaultNotFound as e:
            self._audit.record("portal-auto-unlock", PORTAL_KEYS_VAULT,
                               decision="deny", reason="no-such-vault")
            raise PwdNotFound(str(e)) from e
        except VaultBadPassword as e:
            self._audit.record("portal-auto-unlock", PORTAL_KEYS_VAULT,
                               decision="deny", reason="stale-pin")
            raise PwdBadPassword(str(e)) from e
        except VaultIntegrityError as e:
            self._audit.record("portal-auto-unlock", PORTAL_KEYS_VAULT,
                               decision="deny", reason=f"integrity:{e}")
            raise PwdIntegrityError(str(e)) from e
        finally:
            # Always wipe the unsealed PIN bytes — only the master key
            # lives on past this point.
            ba_pin = bytearray(pin)
            _wipe_bytearray(ba_pin)
        ba = bytearray(key)
        now = int(time.time())
        self._unlocked[PORTAL_KEYS_VAULT] = {
            "key": ba, "unlocked_at": now, "last_use": now,
        }
        self._audit.record("portal-auto-unlock", PORTAL_KEYS_VAULT,
                           decision="allow",
                           reason=f"auto-unsealed {kind}")
        self.VaultUnlocked(PORTAL_KEYS_VAULT)
        return True

    # -- portal Secret backend bridge (spec/13 Phase-8.3) ---------------

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="ay",
                         sender_keyword="sender")
    def GetPortalKey(self, app_id: str, sender=None):
        """Return the per-app-id portal Secret key as bytes.

        Drives the org.freedesktop.impl.portal.Secret.RetrieveSecret
        flow. The portal backend daemon (running per-user in the
        session) calls this method on the system bus; we look up
        `portal/<app_id>` in the configured PORTAL_KEYS_VAULT, auto-
        provisioning a 32-byte random key on first use.

        Caller identity (uid/pid + exe via /proc) is captured for the
        audit log. The key is delivered to the per-user portal daemon
        which writes it to the fd the app handed in.

        Constraints:
          - The portal-keys vault MUST be unlocked first (admin task).
          - Caller identity must be the installed qdistro-pwd-portal helper.
          - App-id format is loosely validated (no path separators,
            non-empty). A production portal backend must derive this from
            portal metadata instead of trusting a caller-provided string.
        """
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        portal_ok, portal_reason = _portal_backend_allowed(pid)
        if not portal_ok:
            self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                               item_tag=str(app_id), decision="deny",
                               reason=f"portal-caller:{portal_reason}",
                               caller=caller)
            raise PwdPolicyError("GetPortalKey requires qdistro-pwd-portal")
        app_id = str(app_id)
        if not app_id or "/" in app_id or app_id.startswith("."):
            self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                               item_tag=app_id, decision="deny",
                               reason="bad-app-id", caller=caller)
            raise PwdPolicyError(f"invalid app_id: {app_id!r}")
        if PORTAL_KEYS_VAULT not in self._unlocked:
            self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                               item_tag=app_id, decision="deny",
                               reason="vault-locked", caller=caller)
            raise PwdNotUnlocked(
                f"portal-keys vault {PORTAL_KEYS_VAULT!r} is locked")
        self._touch(PORTAL_KEYS_VAULT)
        master_key = bytes(self._unlocked[PORTAL_KEYS_VAULT]["key"])
        tag = f"portal/{app_id}"
        try:
            existing = get_item_payload(VAULT_DIR, PORTAL_KEYS_VAULT,
                                        master_key, tag)
            self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                               item_tag=tag, decision="allow",
                               reason="existing-key", caller=caller)
            return [dbus.Byte(b) for b in existing]
        except VaultNotFound:
            pass  # fall through to auto-provision
        except VaultIntegrityError as e:
            self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                               item_tag=tag, decision="deny",
                               reason="integrity-fail", caller=caller)
            raise PwdIntegrityError(str(e)) from e
        # Auto-provision a fresh per-app key. Tag is admin-readable so
        # admin can inspect / revoke individual app keys.
        new_key = os.urandom(PORTAL_KEY_BYTES)
        try:
            add_item(VAULT_DIR, PORTAL_KEYS_VAULT, master_key,
                     tag, new_key,
                     pin_uid=int(uid), replace=False)
        except (VaultDuplicate, VaultIntegrityError, ValueError) as e:
            # Race or corruption — re-read on duplicate so concurrent
            # provisioning of the same app_id converges.
            try:
                existing = get_item_payload(VAULT_DIR, PORTAL_KEYS_VAULT,
                                            master_key, tag)
                self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                                   item_tag=tag, decision="allow",
                                   reason="race-recovered", caller=caller)
                return [dbus.Byte(b) for b in existing]
            except Exception:
                self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                                   item_tag=tag, decision="deny",
                                   reason=f"provision-fail:{e}",
                                   caller=caller)
                raise PwdPolicyError(f"portal-key provision failed: {e}") from e
        self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                           item_tag=tag, decision="allow",
                           reason="provisioned", caller=caller)
        return [dbus.Byte(b) for b in new_key]

    @dbus.service.method(BUS_NAME, in_signature="i", out_signature="aa{sv}",
                         sender_keyword="sender")
    def ListAuditLog(self, limit: int, sender=None) -> list[dict]:
        self._require_admin(sender)
        rows = self._audit.tail(int(limit))
        return [self._wrap_audit_row(r) for r in rows]

    @staticmethod
    def _wrap_audit_row(r: dict) -> dict:
        return {
            "id":             dbus.Int64(r["id"]),
            "ts":             dbus.Int64(r["ts"]),
            "op":             dbus.String(r["op"] or ""),
            "vault":          dbus.String(r["vault"] or ""),
            "item_tag":       dbus.String(r["item_tag"] or ""),
            "decision":       dbus.String(r["decision"] or ""),
            "reason":         dbus.String(r["reason"] or ""),
            "caller_uid":     dbus.Int32(r["caller_uid"] if r["caller_uid"] is not None else -1),
            "caller_pid":     dbus.Int32(r["caller_pid"] if r["caller_pid"] is not None else -1),
            "caller_exe":     dbus.String(r["caller_exe"] or ""),
            "caller_sha":     dbus.String(r["caller_sha"] or ""),
            "caller_selinux": dbus.String(r["caller_selinux"] or ""),
            "caller_cgroup":  dbus.String(r["caller_cgroup"] or ""),
            "origin":         dbus.String(r.get("origin") or ""),
            "app_id":         dbus.String(r.get("app_id") or ""),
            "app_context":    dbus.String(r.get("app_context") or ""),
        }

    # -- signals -------------------------------------------------------

    @dbus.service.signal(BUS_NAME, signature="s")
    def VaultUnlocked(self, name: str):
        pass

    @dbus.service.signal(BUS_NAME, signature="ss")
    def VaultLocked(self, name: str, reason: str):
        pass

    @dbus.service.signal(BUS_NAME, signature="sss")
    def SecretDeliveredToWorkflow(self, run_id: str, vault: str, tag: str):
        """Emitted when an item is released to a workflow run. Carries
        only metadata (run_id, vault, tag) — never the secret value — so
        the broker/workflow audit chain can record the delivery."""
        pass


def _on_term(*_user_data):
    # ``GLib.unix_signal_add(priority, signum, handler, *user_data)`` invokes
    # the handler with ``*user_data`` — NOT with the Python ``signal``-module
    # ``(signum, frame)`` convention. The pre-fix signature
    # ``def _on_term(signum, frame)`` raised ``TypeError: _on_term() missing
    # 1 required positional argument: 'frame'`` on SIGTERM/SIGINT, leaving
    # the daemon hanging in ``stop-sigterm`` after ``systemctl restart``
    # (Round-7 fix-pass review). Accept ``*user_data`` and return ``False``
    # so GLib removes the source after the loop quits.
    print("[qdistro-pwd] caught signal, shutting down", flush=True)
    if _LOOP is not None:
        GLib.MainLoop.quit(_LOOP)
    return False


_LOOP: GLib.MainLoop | None = None


def main():
    global _LOOP
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)  # noqa: F841
    daemon = PwdDaemon(bus)  # noqa: F841
    print(f"[qdistro-pwd] listening on {BUS_NAME} {OBJ_PATH}", flush=True)
    _LOOP = GLib.MainLoop()
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, _on_term, None)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, _on_term, None)
    _LOOP.run()


if __name__ == "__main__":
    main()
