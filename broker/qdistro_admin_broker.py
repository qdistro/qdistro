#!/usr/bin/env python3
"""qdistro admin broker.

System-bus D-Bus service that mediates permission requests from user-uid
silos to the admin role (uid 1000, OS user `admin`). The admin app calls
`GetPending`/`DecideRequest`; user apps via the SDK call
`RequestPermission`/`WaitForDecision`.

Phase 1 was a skinny channel proof. Phase 2 slice A adds a persistent
sqlite approval cache: scopes other than 'once' write rows that future
RequestPermission calls match against and short-circuit (no admin prompt
within the scope's lifetime). See .
"""
from __future__ import annotations

import os
import pwd as _pwd_mod
import re
import signal
import threading
from typing import Any

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib, Gio

from qdistro_admin_cache import ApprovalCache  # type: ignore[import-not-found]
from qdistro_admin_audit import AuditLog  # type: ignore[import-not-found]
from qdistro_admin_ratelimit import RateLimiter  # type: ignore[import-not-found]
from qdistro_admin_rules import RulesEngine  # type: ignore[import-not-found]
from qdistro_audisp_parser import is_qdistro_subj_type  # type: ignore[import-not-found]
from qdistro_hook_client import HookClient  # type: ignore[import-not-found]

BUS_NAME = "org.qdistro.AdminBroker1"
OBJ_PATH = "/org/qdistro/AdminBroker1"
# Discover the admin UID at startup. QDISTRO_ADMIN_USER overrides the
# username for images where the admin account has a non-standard UID;
# it does NOT rename the admin role (D-Bus policies still reference
# user="admin", and changing that requires updating the .conf files).
try:
    ADMIN_UID = _pwd_mod.getpwnam(
        os.environ.get("QDISTRO_ADMIN_USER", "admin")).pw_uid
except KeyError:
    ADMIN_UID = 1000
DB_PATH = "/var/lib/qdistro/approvals/approvals.sqlite"
AUDIT_PATH = "/var/lib/qdistro/audit/audit.sqlite"

# Audit rows older than this are deleted by a daily timer (and on
# demand via RunAuditGc). 90 days is long enough for "what happened
# last quarter?" investigations without unbounded disk growth on
# workstations that live for years. Override with QDISTRO_AUDIT_RETENTION_DAYS
# for testing or stricter policies; set to 0 to disable GC entirely.
AUDIT_RETENTION_DAYS_DEFAULT = 90
AUDIT_GC_INTERVAL_S = 86400  # once per day

# If True, an audit-log failure on the prompt path (admin pressed
# Approve/Deny) forces a deny: waiters get False, the admin app gets
# a DBusException, and the request is marked denied. Cache-hit path
# is read-only auditing and continues on failure with a loud log.
# Override via QDISTRO_AUDIT_REQUIRED=0 for incident recovery.
AUDIT_REQUIRED = os.environ.get("QDISTRO_AUDIT_REQUIRED", "1") != "0"

# Scope vocabulary. Kept in sync with qdistro_admin_rules._VALID_SCOPES
# and qdistro_admin_cache.scope_to_row — a decision that names any
# other string is rejected at DecideRequest.
#
# task(072): forever_argv / forever_basename / forever_prefix close the
# qsu argv-leak by tightening the (uid, action) → cache mapping to also
# pin the argv tuple / basename / prefix. The cache backend has carried
# them since task(069); this set lifts the broker's own gate.
_VALID_SCOPES = frozenset((
    "once", "1h", "24h",
    "forever", "forever_exe",
    "forever_argv", "forever_basename", "forever_prefix",
))

# Delegated-identity requests (RequestPermissionAs, called by
# qdistro-root-exec on behalf of an unauthenticated peer identity)
# can't use argv-blind long-lived scopes. The delegator attests to
# the caller's uid/pid/exe — fine for deciding *this* call — but the
# broker can't re-authenticate that identity on future calls (a
# different process as the same uid could be asking). For scopes
# that DON'T pin argv (`forever`, `forever_exe`, plus the timed
# variants `1h`/`24h` which currently store as argv_exact-with-empty
# argv when the caller doesn't pass one), one approval becomes a
# wildcard for the (uid, action) pair: a `1h` approval of `qsu id`
# would implicitly approve `qsu anything-else` at root for the next
# hour.
#
# task(078): the argv-aware scopes (forever_argv / forever_basename /
# forever_prefix) ARE permitted for delegated requests because they
# pin argv. A `forever_argv` approval of `[apt-get, update]` only
# matches future delegated calls with that exact argv tuple — a
# different process at the same uid asking for `[apt-get, install]`
# still re-prompts. This is precisely the qsu argv-leak fix the cache
# vocabulary in task(069) was scoped against; tasks(072)/(077)
# extended the broker + UI for the non-delegated case but left this
# delegated guard unchanged. With the argv-aware scopes now reachable
# from a delegated DecideRequest, qsu admins can issue durable
# argv-pinned approvals safely.
_DELEGATED_FORBIDDEN_SCOPES = frozenset((
    "1h", "24h",
    "forever", "forever_exe",
))

# One-shot actions are gated to scope='once' regardless of delegation.
# RelayMessage is the initial use case: every cross-user send goes to
# admin on its own, and the (target_uid, target_service) pair is too
# fine-grained to make a persistent grant meaningful — the sender
# probably won't reuse the exact same (target, kind, payload) twice.
_ONESHOT_FORBIDDEN_SCOPES = frozenset((
    "1h", "24h",
    "forever", "forever_exe",
    "forever_argv", "forever_basename", "forever_prefix",
))

USER_RELAY_OBJ_PATH = "/org/qdistro/UserRelay"
USER_RELAY_IFACE = "org.qdistro.UserRelay"
# Per-uid bus name on the SYSTEM bus. dbus-broker session instances
# refuse non-owner-uid peers, so the broker (root) reaches each
# user's relay on the system bus; the relay itself bridges onto its
# own session bus for actual receiver lookup and delivery.
USER_RELAY_SYSTEM_NAME_FMT = "org.qdistro.UserRelay.uid{uid}"

# P02 session-manager gate. The broker asks the manager for the
# target silo's state before letting a cross-uid relay proceed.
# A missing/unknown manager means "no silo registry yet" → fall back
# to the pre-P02 behaviour of trusting the target uid; an explicit
# "not Active" answer is the load-bearing reject path.
SESSION_MANAGER_BUS_NAME = "org.qdistro.SessionManager1"
SESSION_MANAGER_OBJ_PATH = "/org/qdistro/SessionManager1"
SESSION_MANAGER_IFACE = "org.qdistro.SessionManager1"

# When set, _silo_state errors (manager offline, timeout, parse error)
# stop falling through to the legacy trust-the-uid path. Instead they
# return the sentinel "Unreachable" which RelayMessage rejects with
# SiloManagerUnreachable. Hosts that have rolled out the session
# manager should flip this on; legacy bakes keep the fail-open default.
# Read at broker start from $QDISTRO_BROKER_REQUIRE_SILO_ACTIVE or
# /etc/qdistro/broker.conf (key = require_silo_active = true).
_REQUIRE_SILO_ACTIVE_ENV = "QDISTRO_BROKER_REQUIRE_SILO_ACTIVE"
_BROKER_CONF_PATH = "/etc/qdistro/broker.conf"


def _read_require_silo_active() -> bool:
    val = os.environ.get(_REQUIRE_SILO_ACTIVE_ENV, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    try:
        with open(_BROKER_CONF_PATH, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "require_silo_active":
                    return v.strip().lower() in ("1", "true", "yes", "on")
    except OSError:
        pass
    return False


REQUIRE_SILO_ACTIVE = _read_require_silo_active()


# Option A (secctx-identity-contract.md): when True the broker treats
# secctx strings as launcher-attested because qdwin gates the
# wp_security_context_manager_v1 bind to the shell/allowed-uid.  Audit
# entries include the provenance tag so admins can distinguish
# "launcher-gated" from "self-asserted" when reviewing same-silo
# decisions.  When False the secctx is treated as advisory-only and a
# warning is logged on every same-silo gate that relies on it.
# Read from $QDISTRO_SECCTX_LAUNCHER_GATED or broker.conf key
# secctx_launcher_gated.  Default: True (Option A is the production
# posture once qdwin ships the bind gate).
_SECCTX_LAUNCHER_GATED_ENV = "QDISTRO_SECCTX_LAUNCHER_GATED"


def _read_secctx_launcher_gated() -> bool:
    val = os.environ.get(_SECCTX_LAUNCHER_GATED_ENV, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    try:
        with open(_BROKER_CONF_PATH, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "secctx_launcher_gated":
                    return v.strip().lower() in ("1", "true", "yes", "on")
    except OSError:
        pass
    return True  # default on — Option A is the production posture


SECCTX_LAUNCHER_GATED = _read_secctx_launcher_gated()


def _read_hooks_enabled() -> bool:
    """Check whether Python hooks are enabled.

    Reads QDISTRO_HOOKS_ENABLED env or hooks_enabled key from
    /etc/qdistro/broker.conf.  Default: True (hooks consulted when
    the executor socket is reachable).
    """
    val = os.environ.get("QDISTRO_HOOKS_ENABLED", "").strip().lower()
    if val in ("0", "false", "no", "off"):
        return False
    if val in ("1", "true", "yes", "on"):
        return True
    try:
        with open(_BROKER_CONF_PATH, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "hooks_enabled":
                    return v.strip().lower() not in ("0", "false", "no", "off")
    except OSError:
        pass
    return True  # enabled by default


HOOKS_ENABLED = _read_hooks_enabled()

# Receiver service names must match this shape. Used by RelayMessage
# to reject obviously hostile target_service strings before they hit
# the user relay.
_SERVICE_NAME_RE = re.compile(r"^org\.qdistro\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$")


def _sanitize_details(raw) -> dict[str, str]:
    """Coerce an a{sv} details dict to {str: str} with control-char
    scrubbing + per-value truncation.

    Rendering raw caller-supplied text inside the admin's TUI detail
    pane lets a hostile app inject ANSI escapes that draw fake
    approval banners or erase the cursor line. We strip anything not
    printable + common whitespace, and cap each value to keep a
    single request from hogging the pane.
    """
    MAX_VAL = 1024
    MAX_KEYS = 32
    out: dict[str, str] = {}
    for k, v in dict(raw).items():
        if len(out) >= MAX_KEYS:
            break
        key = _scrub(str(k))[:64]
        val = _scrub(str(v))[:MAX_VAL]
        if key:
            out[key] = val
    return out


def _scrub(s: str) -> str:
    return "".join(c for c in s if c == "\t" or c == " " or c.isprintable())


_ARGV_KEY_RE = re.compile(r"^argv\[(\d{2,4})\]$")


def _argv_from_details(details: dict) -> list[str] | None:
    """Reconstruct argv from `argv[NN]` details keys.

    qsu (scripts/vm/qsu/qdistro_root_exec.py) ships argv to
    the broker as one key per element — `argv[00]`, `argv[01]`, etc.
    The shlex-joined `details.argv` is human-only (lossy on argv
    elements containing whitespace) and not used for rule matching.

    Returns None when no `argv[NN]` keys are present, so callers that
    don't carry argv (clipboard / handoff checks) skip argv-selector
    rules entirely. Returns a (possibly empty) list when keys are
    present but malformed — preserves caller-as-given semantics for
    audit while still letting argv-selector rules fail-to-match.

    Sparse / out-of-order indices are tolerated (sorted by index) so
    a hostile caller skipping `argv[03]` can't cause a missing-element
    quirk to bypass an argv_exact selector — the resulting list is
    "what was actually passed," and a rule expecting a specific
    sequence won't match it. Indices beyond a reasonable cap (1024)
    are dropped to keep the reconstruction bounded.
    """
    indexed: list[tuple[int, str]] = []
    for k, v in details.items():
        m = _ARGV_KEY_RE.match(str(k))
        if m is None:
            continue
        idx = int(m.group(1))
        if idx > 1024:
            continue
        indexed.append((idx, str(v)))
    if not indexed:
        return None
    indexed.sort(key=lambda kv: kv[0])
    return [v for _, v in indexed]


def _selector_from_details(details: dict, key: str) -> str:
    value = dict(details or {}).get(key, "")
    return str(value or "")[:128]


def _read_proc_uid(pid: int) -> int | None:
    """Return the real uid of pid from /proc/<pid>/status, or None if the
    process is gone. Used by VerifyClientIdentity to cross-check the
    uid qdwin observed via SO_PEERCRED.
    """
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("Uid:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1])
                    break
    except (OSError, ValueError):
        return None
    return None


def _read_proc_selinux_label(pid: int) -> str:
    """Return the SELinux label for pid from /proc/<pid>/attr/current,
    or "" if the file is unreadable (SELinux off, process gone). Used
    by VerifyClientIdentity to re-check the qdshell-forwarded tuple
    against the live process — see todo/decisions/
    secctx-identity-contract.md.
    """
    try:
        with open(f"/proc/{pid}/attr/current", "rb") as f:
            label = f.read(4096)
        return label.rstrip(b"\x00\n\r ").decode("utf-8", "replace")
    except OSError:
        return ""


def _read_proc_identity(pid: int) -> tuple[str, int]:
    """Return (exe_path, starttime_ticks) for pid, or ("?", 0) if gone.

    starttime is read from /proc/<pid>/stat field 22. The stat file's
    comm field (field 2) is wrapped in parens and can contain spaces,
    so we split from the *right* of the closing paren to avoid a
    maliciously-named comm breaking the parse.
    """
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exe = "?"
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        rparen = data.rfind(b")")
        if rparen < 0:
            return exe, 0
        fields = data[rparen + 2:].split()
        # starttime is field 22 overall; after splitting past (comm)
        # it lands at fields[19].
        return exe, int(fields[19])
    except (OSError, ValueError, IndexError):
        return exe, 0


# Cap how much of an exe we hash. Most binaries are well under 64 MiB;
# anything bigger is almost certainly a self-extracting bundle whose
# trailing payload doesn't change identity assertions for the wrapping
# binary. Bounded reads keep _enqueue under a hundred ms even on the
# pathological "200 MiB monolith with one hot-path mtime tick" case.
_EXE_HASH_BYTES_MAX = 64 * 1024 * 1024

_proc_layered_cache: dict[tuple[int, int, str], dict[str, str]] = {}
_proc_layered_lock = threading.Lock()
_PROC_LAYERED_CACHE_MAX = 256


def _read_proc_layered(pid: int) -> dict[str, str]:
    """Snapshot the layered identity attributes spec/25 §Phase-2 wants
    surfaced on the admin pane: exe SHA-256, SELinux label, cgroup.

    Results are cached by (pid, starttime, exe_path) so repeat
    lookups for the same process skip the expensive exe-hash IO.
    The exe_path is included so an exec into a different binary
    invalidates the cache. Note: the cache does NOT help when N
    different qsu clients arrive concurrently (each has a distinct
    pid); the proper fix for that case is async D-Bus method
    dispatch so exe hashing runs in parallel on a thread pool.
    """
    exe_path, start_time = _read_proc_identity(pid)
    key = (pid, start_time, exe_path)
    with _proc_layered_lock:
        cached = _proc_layered_cache.get(key)
        if cached is not None:
            return cached
    out = {"exe_sha256": "", "selinux_label": "", "cgroup": ""}

    # Hash via the kernel's /proc/<pid>/exe symlink. Reading through
    # /proc means a process re-exec'ing into a different binary between
    # request and hash will be reflected — we don't snapshot the path
    # then re-open; we open through the live link.
    try:
        import hashlib
        h = hashlib.sha256()
        remaining = _EXE_HASH_BYTES_MAX
        with open(f"/proc/{pid}/exe", "rb") as f:
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
        out["exe_sha256"] = h.hexdigest()
    except OSError:
        pass

    # SELinux process label. On non-SELinux systems the file simply
    # doesn't exist; on permissive systems it's still populated and
    # is informative for the admin. The kernel terminates the value
    # with a NUL byte; strip it for cleaner display.
    try:
        with open(f"/proc/{pid}/attr/current", "rb") as f:
            label = f.read(4096)
        out["selinux_label"] = label.rstrip(b"\x00\n").decode(
            "utf-8", "replace")
    except OSError:
        pass

    # cgroup v2 unified path is the last line of /proc/<pid>/cgroup
    # ("0::/path/...") on a unified hierarchy. On hybrid hosts there
    # are multiple lines; surface the unified one when present, else
    # fall back to the first non-empty line so the admin still sees
    # something useful.
    try:
        with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f.readlines()]
        unified = next(
            (ln.split("::", 1)[1] for ln in lines if ln.startswith("0::")),
            None)
        if unified:
            out["cgroup"] = unified
        elif lines:
            out["cgroup"] = lines[0]
    except OSError:
        pass

    with _proc_layered_lock:
        if len(_proc_layered_cache) >= _PROC_LAYERED_CACHE_MAX:
            _proc_layered_cache.clear()
        _proc_layered_cache[key] = out
    return out


class _Request:
    __slots__ = (
        "id", "uid", "pid", "exe", "start_time", "action", "details",
        "decision", "waiters", "delegated", "one_shot",
        "exe_sha256", "selinux_label", "cgroup",
    )

    def __init__(self, rid: int, uid: int, pid: int, exe: str,
                 start_time: int, action: str, details: dict,
                 delegated: bool = False, one_shot: bool = False,
                 exe_sha256: str = "", selinux_label: str = "",
                 cgroup: str = ""):
        self.id = rid
        self.uid = uid
        self.pid = pid
        self.exe = exe
        # /proc/<pid>/stat field 22 (starttime) in clock ticks since
        # boot. Captured at request-time; re-checked at decide-time so
        # pid recycling between prompt and admin click can't switch the
        # target identity under us. 0 means "not available" (typically
        # because the peer exited between request and verification —
        # the request stays pending but decides deny).
        self.start_time = int(start_time)
        self.action = action
        self.details = details
        self.decision: bool | None = None
        # callbacks waiting for a decision; each is (reply_cb, error_cb)
        self.waiters: list[tuple] = []
        # True if identity was claimed by a trusted delegator
        # (RequestPermissionAs) rather than the dbus connection's own
        # peer creds. Limits what kinds of cache rows DecideRequest
        # may produce.
        self.delegated = bool(delegated)
        # True for per-call-approved actions that never write a cache
        # row regardless of admin's scope pick. RelayMessage sets this;
        # DecideRequest force-rejects non-'once' scopes for these.
        self.one_shot = bool(one_shot)
        # spec/25 §Phase-2 layered identity. Populated at _enqueue
        # time (best-effort; empty strings if process gone or kernel
        # interface absent). Surfaced via GetPending so the admin app
        # can render alongside uid/pid/exe in the request detail pane.
        self.exe_sha256 = str(exe_sha256 or "")
        self.selinux_label = str(selinux_label or "")
        self.cgroup = str(cgroup or "")


class Broker(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, OBJ_PATH)
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, _Request] = {}
        self.cache = ApprovalCache(DB_PATH)
        self.audit = AuditLog(AUDIT_PATH)
        # Declarative pre-approval rules. Broken YAML or an empty
        # rules dir leaves the list empty; the broker degrades to the
        # Phase-1 cache+prompt path. Admin can see parse errors via
        # `rules.load_errors()` (no GUI yet — log them on startup).
        self.rules = RulesEngine()
        for err in self.rules.load_errors():
            print(f"[broker] rules: {err}", flush=True)
        if self.rules.rules():
            print(f"[broker] rules: loaded {len(self.rules.rules())} rule(s)", flush=True)
        # 50/sec/(uid,action) hard cap. Chosen so a legitimate UI with a
        # couple of rapid retries stays unaffected; a tight-loop attacker
        # hits the cap in ~20ms and gets audited per rejection.
        self.ratelimit = RateLimiter(limit=50, window_s=1.0)
        # Sandboxed Python hook executor client.  When enabled, the
        # broker consults the hook executor after declarative rules are
        # inconclusive and before falling back to the admin prompt.
        # The executor runs in a separate process; if its socket is
        # unreachable, the broker falls through to admin prompt silently.
        self.hooks = HookClient(enabled=HOOKS_ENABLED)
        if HOOKS_ENABLED:
            print("[broker] hooks: enabled, executor socket at "
                  f"{self.hooks._socket_path}", flush=True)
        # Option A: log the secctx provenance posture so admins see it
        # in journalctl at broker start.
        print(f"[broker] secctx_launcher_gated="
              f"{SECCTX_LAUNCHER_GATED}", flush=True)
        # Retention knob: env override wins for tests; 0 disables GC.
        try:
            self._audit_retention_days = int(
                os.environ.get("QDISTRO_AUDIT_RETENTION_DAYS",
                               AUDIT_RETENTION_DAYS_DEFAULT))
        except ValueError:
            self._audit_retention_days = AUDIT_RETENTION_DAYS_DEFAULT
        # GC expired rows once per minute
        GLib.timeout_add_seconds(60, self._gc_tick)
        # GC the audit log once on startup (so short-lived services
        # still sweep) and then once per day.
        if self._audit_retention_days > 0:
            self._audit_gc_tick()
            GLib.timeout_add_seconds(AUDIT_GC_INTERVAL_S, self._audit_gc_tick)
        # spec/10 v14 follow-up: emit RulesReloaded once at startup so
        # subscribers (qdshell) drop any cross-silo dedup state cached
        # against the *previous* broker instance's rule set. The signal
        # is deferred a tick so the bus name is fully registered.
        # No-op when no rules are loaded — same broker contract as
        # before, just one extra emit per service start.
        GLib.timeout_add(50, self._emit_startup_rules_reloaded)

        # task(059): SIGHUP triggers a rules reload + RulesReloaded
        # emit, mirroring auditd / dbus-daemon convention. Coalesced
        # via GLib.unix_signal_add so the actual reload runs on the
        # mainloop thread (RulesEngine.reload isn't reentrant).
        try:
            GLib.unix_signal_add(
                GLib.PRIORITY_DEFAULT, signal.SIGHUP,
                self._on_sighup, None)
        except Exception as e:  # noqa: BLE001
            # Old GLib versions or non-Linux fall through to D-Bus
            # ReloadRules and inotify only.
            print(f"[broker] SIGHUP wiring skipped: {e!r}", flush=True)

        # task(059): inotify watch on /etc/qdistro/rules.d so admin
        # `cp some.yaml /etc/qdistro/rules.d/` (or vim-save with its
        # `.swp.<file>` rename dance) triggers reload without a
        # SIGHUP or a D-Bus call. Debounced so a flurry of CHANGED
        # events (a single file save can fire 3-5 of them) only
        # reloads once. GFileMonitor uses the kernel's inotify under
        # the hood and integrates with the GLib mainloop.
        self._reload_debounce_id: int = 0
        self._rules_dir_monitor: Gio.FileMonitor | None = None
        try:
            rules_dir = self.rules.directory()
        except AttributeError:
            # Older RulesEngine without .directory(); fall back to
            # the default. Keeps the broker startable against an
            # un-upgraded rules module.
            rules_dir = "/etc/qdistro/rules.d"
        try:
            os.makedirs(rules_dir, mode=0o755, exist_ok=True)
            gfile = Gio.File.new_for_path(rules_dir)
            self._rules_dir_monitor = gfile.monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._rules_dir_monitor.set_rate_limit(200)
            self._rules_dir_monitor.connect(
                "changed", self._on_rules_dir_changed)
            print(f"[broker] inotify watching {rules_dir} for hot reload",
                  flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[broker] inotify watch on {rules_dir} skipped: {e!r}",
                  flush=True)

    def _on_sighup(self, _unused) -> bool:
        # Always returns True — keep the handler installed across
        # repeated SIGHUPs.
        try:
            self.reload_rules_from_disk(source="sighup")
        except Exception as e:  # noqa: BLE001
            print(f"[broker] SIGHUP reload failed: {e!r}", flush=True)
        return True

    def _on_rules_dir_changed(self, _monitor, _file, _other_file,
                              event_type) -> None:
        # Filter to events that actually mean a rule file changed.
        # ATTRIBUTE_CHANGED fires on chmod/chown which doesn't change
        # YAML content; ignore. CHANGES_DONE_HINT is the signal that
        # an in-flight write has committed (Gio batches CHANGED → ...
        # → CHANGES_DONE_HINT) — debouncing on it gives one reload
        # per save instead of three.
        relevant = {
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.MOVED_OUT,
            Gio.FileMonitorEvent.RENAMED,
        }
        if event_type not in relevant:
            return
        # 200ms debounce — coalesces a vim atomic save (write tmp
        # → rename → unlink swp) into one reload.
        if self._reload_debounce_id:
            try:
                GLib.source_remove(self._reload_debounce_id)
            except Exception:  # noqa: BLE001
                pass
        self._reload_debounce_id = GLib.timeout_add(
            200, self._inotify_reload_tick)

    def _inotify_reload_tick(self) -> bool:
        self._reload_debounce_id = 0
        try:
            self.reload_rules_from_disk(source="inotify")
        except Exception as e:  # noqa: BLE001
            print(f"[broker] inotify reload failed: {e!r}", flush=True)
        return False  # one-shot

    def _emit_startup_rules_reloaded(self):
        try:
            self.RulesReloaded(int(len(self.rules.rules())))
        except Exception as e:  # noqa: BLE001
            print(f"[broker] startup RulesReloaded emit failed: {e!r}",
                  flush=True)
        return False  # one-shot

    def reload_rules_from_disk(self, *, source: str) -> int:
        """Re-walk the rules directory, log + emit RulesReloaded.
        Returns the number of rules now loaded. `source` is a short
        tag ("sighup", "inotify", "dbus", "startup") that lands in
        the broker log so `journalctl` can attribute the trigger.
        Centralised so SIGHUP, inotify, and the D-Bus ReloadRules
        method all share one code path."""
        self.rules.reload()
        errs = self.rules.load_errors()
        n = len(self.rules.rules())
        for err in errs:
            print(f"[broker] rules: {err}", flush=True)
        print(f"[broker] rules reloaded ({source}): "
              f"{n} rule(s), {len(errs)} error(s)", flush=True)
        try:
            self.RulesReloaded(int(n))
        except Exception as e:  # noqa: BLE001
            print(f"[broker] RulesReloaded signal emit failed ({source}): "
                  f"{e!r}", flush=True)
        return n

    def _gc_tick(self) -> bool:
        try:
            self.cache.gc()
        except Exception as e:  # noqa: BLE001
            print(f"[broker] cache.gc failed: {e}", flush=True)
        return True  # keep firing

    def _audit_gc_tick(self) -> bool:
        seconds = self._audit_retention_days * 86400
        try:
            n = self.audit.gc(seconds)
            if n:
                print(f"[broker] audit.gc deleted {n} rows older than "
                      f"{self._audit_retention_days}d", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[broker] audit.gc failed: {e}", flush=True)
        return True

    def _peer_info(self, sender: str, conn) -> tuple[int, int, str, int]:
        """Resolve the calling peer's (uid, pid, exe, start_time).

        start_time is /proc/<pid>/stat field 22 — clock ticks since
        boot. It lets the broker detect pid recycling between prompt
        and decide: if the starttime at DecideRequest differs from the
        one captured here, the pid has been reused by a different
        process and the admin's click must not grant trust to that
        process's action.
        """
        bus = dbus.SystemBus()
        dbus_proxy = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
        dbus_iface = dbus.Interface(dbus_proxy, "org.freedesktop.DBus")
        uid = int(dbus_iface.GetConnectionUnixUser(sender))
        pid = int(dbus_iface.GetConnectionUnixProcessID(sender))
        exe, start_time = _read_proc_identity(pid)
        return uid, pid, exe, start_time

    @dbus.service.method(BUS_NAME, in_signature="sa{sv}", out_signature="i", sender_keyword="sender", connection_keyword="conn")
    def RequestPermission(self, action: str, details: dict, sender=None, conn=None) -> int:
        uid, pid, exe, start_time = self._peer_info(sender, conn)
        return self._enqueue(uid, pid, exe, start_time,
                             str(action), details, delegated=False)

    @dbus.service.method(BUS_NAME, in_signature="sa{sv}", out_signature="s",
                         sender_keyword="sender", connection_keyword="conn")
    def CheckPermission(self, action: str, details: dict,
                        sender=None, conn=None) -> str:
        """Fast-path permission lookup with no admin prompt.

        Returns one of "allow" / "deny" / "unknown". Rules first,
        cache second — exactly the resolution order `_enqueue` uses —
        but if neither tier fires this method returns "unknown"
        without touching `_pending` or spending admin's attention.

        Callers use this as the synchronous gate on a user action.
        When the answer is "unknown", they typically refuse the
        immediate attempt and separately fire `RequestPermission`
        (fire-and-forget) so admin eventually sees a policy-change
        prompt for next time. See  §S4.

        Rate-limited the same way `_enqueue` is: a tight-loop caller
        hitting CheckPermission still consumes CPU in the broker's
        rules/cache lookups.

        task(069): if `details` carries an argv (per-element argv[NN]
        keys, qsu encoding) it is forwarded to both the rules engine
        AND the cache lookup so the gate respects argv-anchored
        approvals.
        """
        uid, pid, exe, _st = self._peer_info(sender, conn)
        action_s = str(action)
        argv = _argv_from_details(details)
        if not self.ratelimit.check(uid, action_s):
            raise dbus.DBusException(
                f"Rate limit exceeded for uid={uid} "
                f"action={action_s!r} (>{self.ratelimit.limit}/"
                f"{self.ratelimit.window_s}s). Check rejected.",
                name=BUS_NAME + ".RateLimited",
            )
        rule = self.rules.match(
            uid=uid, action=action_s, exe=exe,
            app_id=_selector_from_details(details, "app_id"),
            sandbox_engine=_selector_from_details(details, "sandbox_engine"),
            mime_type=_selector_from_details(details, "mime_type"),
            argv=argv,
        )
        if rule is not None:
            return "allow" if rule.decision == "allow" else "deny"
        row = self.cache.lookup_detail(uid, action_s, exe, argv)
        if row is not None:
            return "allow" if bool(row["decision"]) else "deny"
        # Consult Python hooks when rules and cache are both
        # inconclusive. CheckPermission is a fast-path; the hook
        # query adds an AF_UNIX round-trip but stays within the 2s
        # D-Bus ceiling thanks to the hook timeout (default 5s, but
        # CheckPermission callers expect <2s — the hook executor
        # timeout is capped at HOOK_CALL_TIMEOUT_S which is 4s on
        # the executor side). Treat errors as "unknown" (fall through).
        try:
            hook_event = dict(_sanitize_details(details))
            hook_event["caller_uid"] = uid
            hook_event["caller_pid"] = pid
            hook_event["caller_exe"] = exe
            hook_event["action_full"] = action_s
            hook_resp = self.hooks.query(action_s, hook_event)
            if hook_resp is not None:
                verdict = hook_resp.get("verdict")
                reason = hook_resp.get("reason", "")[:256]
                try:
                    self.audit.log(
                        action=action_s, uid=uid, pid=pid, exe=exe,
                        decision=(verdict in ("allow", "transform")),
                        scope=None,
                        source=f"hook verdict={verdict} reason={reason}")
                except Exception:  # noqa: BLE001
                    pass
                if verdict in ("allow", "transform"):
                    return "allow"
                if verdict == "deny":
                    return "deny"
        except Exception:  # noqa: BLE001
            pass
        return "unknown"

    @dbus.service.method(BUS_NAME,
                         in_signature="utusssss", out_signature="b",
                         sender_keyword="sender", connection_keyword="conn")
    def VerifyClientIdentity(self, pid: int, starttime: int, uid: int,
                             exe: str, selinux_label: str,
                             claimed_sandbox_engine: str,
                             claimed_app_id: str,
                             claimed_instance_id: str,
                             sender=None, conn=None) -> bool:
        """Option-B identity re-verification for qdshell-mediated gates.

        Confirms that the (pid, starttime, uid, exe, selinux_label)
        tuple qdwin observed at secctx-bind time still names a live
        process with the same attributes. Returns True iff every live
        check matches; False on any mismatch or if the process is gone.
        Anti-PID-reuse is anchored on starttime — a PID that was
        recycled into a different process will have a different field-22.

        SELinux is checked only when both sides carry a non-empty label
        (kernel off / unconfined → skip that axis but still match the
        rest). Empty `exe` from the caller is treated as "qdwin could
        not read it"; we then skip the exe match instead of failing it.

        See todo/decisions/secctx-identity-contract.md (Option B).
        """
        caller_uid, caller_pid, caller_exe, _ = self._peer_info(sender, conn)
        # Defensive sanitisation — same envelope as the other broker
        # methods. The dbus policy file already pins this method to
        # admin uid; keep the in-method check as defense-in-depth.
        try:
            pid_i = int(pid)
            start_i = int(starttime)
            uid_i = int(uid)
        except (TypeError, ValueError):
            return False
        exe_s = str(exe or "")[:4096]
        label_s = str(selinux_label or "")[:512]
        seng_s = str(claimed_sandbox_engine or "")[:128]
        sapp_s = str(claimed_app_id or "")[:128]
        # instance_id is correlation-only (see doc/containers.md
        # §"Secctx contract").  It is logged in the audit action string
        # for traceability but never used as an identity assertion or
        # auth credential — identity verification is anchored on
        # (pid, starttime, uid, exe, selinux_label).
        sinst_s = str(claimed_instance_id or "")[:128]

        verdict = True
        reasons: list[str] = []
        live_exe, live_start = _read_proc_identity(pid_i)
        if live_start == 0:
            verdict = False
            reasons.append("proc-gone")
        else:
            if int(live_start) != start_i:
                verdict = False
                reasons.append(
                    f"starttime-mismatch live={live_start} claimed={start_i}")
            # Exe match: skip if caller couldn't read /proc/<pid>/exe at
            # qdwin time (empty / "?"). Otherwise require equality.
            if exe_s and exe_s != "?" and live_exe and live_exe != "?":
                if live_exe != exe_s:
                    verdict = False
                    reasons.append(
                        f"exe-mismatch live={live_exe!r} claimed={exe_s!r}")
            # UID match: from /proc/<pid>/status (cheap-enough; reuse
            # the existing helper that already reads /proc/<pid>/status).
            live_uid = _read_proc_uid(pid_i)
            if live_uid is not None and int(live_uid) != uid_i:
                verdict = False
                reasons.append(
                    f"uid-mismatch live={live_uid} claimed={uid_i}")
            # SELinux: only meaningful when both sides report a label.
            live_label = _read_proc_selinux_label(pid_i)
            if label_s and live_label:
                if live_label != label_s:
                    verdict = False
                    reasons.append(
                        f"label-mismatch live={live_label!r} "
                        f"claimed={label_s!r}")

        # Structured audit line — broker-wide grep target.
        try:
            self.audit.log(
                caller_uid=caller_uid, caller_pid=caller_pid,
                caller_exe=caller_exe,
                action=f"qdistro.identity.verify:{seng_s}:{sapp_s}:{sinst_s}",
                decision=bool(verdict), scope=None,
                source=(f"verify_client_identity pid={pid_i} "
                        f"starttime={start_i} uid={uid_i} "
                        f"exe={exe_s!r} label={label_s!r} "
                        f"reasons={'|'.join(reasons) or 'ok'}"),
                approver_uid=None,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[broker] qdistro.audit.failure: identity_verify,"
                  f" reason={e!r}", flush=True)
        return bool(verdict)

    @dbus.service.method(BUS_NAME,
                         in_signature="ssassssb", out_signature="s",
                         sender_keyword="sender", connection_keyword="conn")
    def CheckClipboardTransfer(self, source_silo: str, dest_silo: str,
                               mime_types: list,
                               source_app_id: str = "",
                               dest_app_id: str = "",
                               source_sandbox_engine: str = "",
                               identity_verified: bool = False,
                               sender=None, conn=None) -> str:
        """Cross-silo clipboard policy gate. Spec/10.

        Returns "allow" or "deny" (never "unknown" — clipboard hits a
        default policy when no rule matches, since the user is mid-flow
        and synchronous answer is required).

        Same-silo transfers (source==dest) are unconditionally allowed
        without consulting rules — a silo's own apps share its clipboard
        by Wayland-compositor semantics.

        Cross-silo transfers consult the rules engine via the synthetic
        action `qdistro.clipboard.transfer:<source>:<dest>`. An admin
        rule like:

          - decision: allow
            match:
              action: qdistro.clipboard.transfer:user1:admin
            rationale: dev silo can paste into admin terminal

        opts in. The default-when-no-rule decision is "deny" — qdistro's
        principle is that cross-uid data movement is opt-in.

        Each call writes one audit row carrying the synthetic action and
        the (sanitized) joined mime types in the `source` field, so the
        admin History tab surfaces every cross-silo attempt regardless
        of decision.

        Caller is qdshell (uid 1000). Other uids cannot call this — the
        dbus policy file pins the method to the admin uid. Rate-limited
        per the standard envelope.
        """
        uid, pid, exe, _st = self._peer_info(sender, conn)
        src = str(source_silo or "").strip()
        dst = str(dest_silo or "").strip()
        # Defensive: collapse whitespace + cap length to keep audit rows
        # readable. Real silo identifiers are short — `user1`, `admin`,
        # or `vm-<vm_name>` for tier-4 (vm_name max 63 → silo max 66).
        if len(src) > 80 or len(dst) > 80:
            raise dbus.DBusException(
                f"silo identifier too long (src={len(src)} dst={len(dst)}, max 80)",
                name=BUS_NAME + ".InvalidArgument",
            )
        # Normalize mime types: list of strings, sorted, cap at 32 entries
        # and 128 chars each — matches what wl_data_source advertises in
        # practice and keeps audit readable.
        mimes = []
        for m in (mime_types or [])[:32]:
            ms = str(m)[:128]
            if ms:
                mimes.append(ms)
        mimes_joined = ",".join(sorted(set(mimes)))
        sapp = (str(source_app_id or "")[:128])
        dapp = (str(dest_app_id or "")[:128])
        seng = (str(source_sandbox_engine or "")[:128])
        action_s = f"qdistro.clipboard.transfer:{src}:{dst}"
        if not self.ratelimit.check(uid, action_s):
            raise dbus.DBusException(
                f"Rate limit exceeded for uid={uid} "
                f"action={action_s!r} (>{self.ratelimit.limit}/"
                f"{self.ratelimit.window_s}s). Transfer rejected.",
                name=BUS_NAME + ".RateLimited",
            )
        audit_id = (f" src_app={sapp or '(unknown)'}"
                    f" dst_app={dapp or '(unknown)'}"
                    f" src_engine={seng or '(unknown)'}")
        # Option A provenance tag: when the qdwin bind gate is active
        # (SECCTX_LAUNCHER_GATED), secctx strings are launcher-attested
        # by construction.  Include the provenance in every audit row so
        # admins can filter "launcher_gated" vs "advisory" decisions.
        provenance = ("launcher_gated" if SECCTX_LAUNCHER_GATED
                      else "advisory")
        # Same-silo: trivial allow IFF the caller (qdshell) has already
        # independently verified the source AND destination process
        # identity against the broker via VerifyClientIdentity. Without
        # that flag, the same-uid-spoof window from
        # qdwin-secctx-self-asserted strings is open, so we fall through
        # to the cross-silo rule path (default-deny). See
        # todo/decisions/secctx-identity-contract.md (Option B).
        if src == dst and src and bool(identity_verified):
            try:
                self.audit.log(
                    caller_uid=uid, caller_pid=pid, caller_exe=exe,
                    action=action_s, decision=True, scope=None,
                    source=(f"clipboard_same_silo_verified "
                            f"secctx_provenance={provenance} "
                            f"mime={mimes_joined}{audit_id}"),
                    approver_uid=None,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[broker] qdistro.audit.failure: clipboard_same_silo,"
                      f" reason={e!r}", flush=True)
            return "allow"
        # Option A: when secctx is advisory-only (launcher gate off) and
        # a same-silo check arrives without identity verification, log a
        # warning — the silo classification is unattested.
        if src == dst and src and not bool(identity_verified):
            if not SECCTX_LAUNCHER_GATED:
                print(f"[broker] WARN secctx advisory: same-silo "
                      f"clipboard transfer {src} without identity "
                      f"verification; secctx is self-asserted",
                      flush=True)
        # Cross-silo: rule lookup, then default-deny. The rule matcher
        # gets the source-side secctx attributes so admin can author
        # per-app / per-sandbox-engine rules. Dest-side identity is
        # audited but not in the matcher today — most clipboard policies
        # are about who *gives*, not who *receives*.
        #
        # Tier-2 admission note (audit 2026-05-27): this is the primary
        # gate for tier-2 clipboard exfil.  With caps=0 + network=none,
        # clipboard transfer is the highest-bandwidth channel a
        # compromised tier-2 workload has to the host.  The default is
        # "deny" — tier-2 traffic requires an explicit allow rule.
        rule = self.rules.match(uid=uid, action=action_s, exe=exe,
                                app_id=sapp, sandbox_engine=seng)
        decision = "deny"
        rule_path = None
        source_label = "clipboard_default_deny"
        if rule is not None:
            decision = "allow" if rule.decision == "allow" else "deny"
            rule_path = rule.source_path
            source_label = "clipboard_rule"
        try:
            self.audit.log(
                caller_uid=uid, caller_pid=pid, caller_exe=exe,
                action=action_s, decision=(decision == "allow"),
                scope=None,
                source=(f"{source_label} secctx_provenance={provenance} "
                        f"mime={mimes_joined}{audit_id}"),
                approver_uid=None, rule_path=rule_path,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[broker] qdistro.audit.failure: clipboard, "
                  f"reason={e!r}", flush=True)
        return decision

    @dbus.service.method(BUS_NAME,
                         in_signature="ssssssb", out_signature="s",
                         sender_keyword="sender", connection_keyword="conn")
    def CheckClipboardReceive(self, source_silo: str, dest_silo: str,
                              mime_type: str,
                              source_app_id: str = "",
                              dest_app_id: str = "",
                              source_sandbox_engine: str = "",
                              identity_verified: bool = False,
                              sender=None, conn=None) -> str:
        """Per-MIME, per-recipient clipboard receive gate. Spec/10 v15.

        Returns "allow" or "deny" (never "unknown" — receive is
        synchronous: the destination's `wl_data_offer.receive` is
        suspended on the compositor side until we answer).

        Same shape as CheckClipboardTransfer but called per
        `wl_data_offer.receive` rather than once at set-selection
        time. Fires the synthetic action
        ``qdistro.clipboard.receive:<source>:<dest>`` so admin can
        author a different policy for receive than for set (typical
        defense-in-depth: allow set, deny receive on specific
        target silos).

        Same-silo receives are unconditionally allowed without
        consulting rules — a silo's own apps share its clipboard by
        Wayland-compositor semantics.

        Cross-silo receives consult the rules engine; ``mime_type``
        is recorded in the audit row's source-tag so the History tab
        surfaces which mime triggered the policy hit. Admin can author
        per-MIME rules via the ``mime_type:`` selector — e.g. allow
        ``text/plain`` and deny ``image/png`` between the same silo
        pair. Selector presence implies "must equal": rules naming
        ``mime_type`` only match receive calls (transfer / handoff
        gates do not carry a single mime).

        Caller is qdshell (uid 1000). Other uids cannot call this —
        the dbus policy file pins the method to the admin uid.
        Rate-limited per the standard envelope.
        """
        uid, pid, exe, _st = self._peer_info(sender, conn)
        src = str(source_silo or "").strip()
        dst = str(dest_silo or "").strip()
        if len(src) > 80 or len(dst) > 80:
            raise dbus.DBusException(
                f"silo identifier too long (src={len(src)} dst={len(dst)}, max 80)",
                name=BUS_NAME + ".InvalidArgument",
            )
        mime_s = str(mime_type or "")[:128]
        sapp = str(source_app_id or "")[:128]
        dapp = str(dest_app_id or "")[:128]
        seng = str(source_sandbox_engine or "")[:128]
        action_s = f"qdistro.clipboard.receive:{src}:{dst}"
        if not self.ratelimit.check(uid, action_s):
            raise dbus.DBusException(
                f"Rate limit exceeded for uid={uid} "
                f"action={action_s!r} (>{self.ratelimit.limit}/"
                f"{self.ratelimit.window_s}s). Receive rejected.",
                name=BUS_NAME + ".RateLimited",
            )
        audit_id = (f" mime={mime_s or '(none)'} "
                    f"src_app={sapp or '(unknown)'} "
                    f"dst_app={dapp or '(unknown)'} "
                    f"src_engine={seng or '(unknown)'}")
        provenance = ("launcher_gated" if SECCTX_LAUNCHER_GATED
                      else "advisory")
        # Same-silo: same Option-B gate as CheckClipboardTransfer.
        if src == dst and src and bool(identity_verified):
            try:
                self.audit.log(
                    caller_uid=uid, caller_pid=pid, caller_exe=exe,
                    action=action_s, decision=True, scope=None,
                    source=(f"clipboard_receive_same_silo_verified "
                            f"secctx_provenance={provenance}"
                            f"{audit_id}"),
                    approver_uid=None,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[broker] qdistro.audit.failure: clipboard_receive_same_silo,"
                      f" reason={e!r}", flush=True)
            return "allow"
        if src == dst and src and not bool(identity_verified):
            if not SECCTX_LAUNCHER_GATED:
                print(f"[broker] WARN secctx advisory: same-silo "
                      f"clipboard receive {src} without identity "
                      f"verification; secctx is self-asserted",
                      flush=True)
        rule = self.rules.match(uid=uid, action=action_s, exe=exe,
                                app_id=sapp, sandbox_engine=seng,
                                mime_type=mime_s)
        decision = "deny"
        rule_path = None
        source_label = "clipboard_receive_default_deny"
        if rule is not None:
            decision = "allow" if rule.decision == "allow" else "deny"
            rule_path = rule.source_path
            source_label = "clipboard_receive_rule"
        try:
            self.audit.log(
                caller_uid=uid, caller_pid=pid, caller_exe=exe,
                action=action_s, decision=(decision == "allow"),
                scope=None,
                source=(f"{source_label} "
                        f"secctx_provenance={provenance}"
                        f"{audit_id}"),
                approver_uid=None, rule_path=rule_path,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[broker] qdistro.audit.failure: clipboard_receive, "
                  f"reason={e!r}", flush=True)
        return decision

    @dbus.service.method(BUS_NAME,
                         in_signature="sssssb", out_signature="s",
                         sender_keyword="sender", connection_keyword="conn")
    def CheckHandoffActivation(self, source_silo: str, dest_silo: str,
                               source_app_id: str, dest_app_id: str,
                               source_sandbox_engine: str = "",
                               identity_verified: bool = False,
                               sender=None, conn=None) -> str:
        """Cross-silo window-activation policy gate. Spec/09.

        Returns "allow" or "deny" (never "unknown" — activation is
        synchronous: the silo's xdg_activation_v1.activate just fired
        and we either honor it now or drop the token).

        Same-silo activations are unconditionally allowed. Cross-silo
        consults the rules engine via the synthetic action
        `qdistro.handoff.activate:<source_silo>:<dest_silo>` (app-ids
        carried in the audit but not in the rule selector — admin can
        author a per-app rule by adding `exe: <app_id>` once we wire
        app_id into rule matching). Default-deny when no rule matches.

        Each call writes one audit row recording source_silo, dest_silo,
        and both app-ids in the source field.

        Caller is qdshell (uid 1000). The dbus policy file pins the
        method to the admin uid.
        """
        uid, pid, exe, _st = self._peer_info(sender, conn)
        src = str(source_silo or "").strip()
        dst = str(dest_silo or "").strip()
        if len(src) > 80 or len(dst) > 80:
            raise dbus.DBusException(
                f"silo identifier too long (src={len(src)} dst={len(dst)}, max 80)",
                name=BUS_NAME + ".InvalidArgument",
            )
        sapp_raw = str(source_app_id or "")[:128]
        dapp_raw = str(dest_app_id or "")[:128]
        sapp = sapp_raw or "(unknown)"
        dapp = dapp_raw or "(unknown)"
        seng_raw = str(source_sandbox_engine or "")[:128]
        seng = seng_raw or "(unknown)"
        action_s = f"qdistro.handoff.activate:{src}:{dst}"
        if not self.ratelimit.check(uid, action_s):
            raise dbus.DBusException(
                f"Rate limit exceeded for uid={uid} "
                f"action={action_s!r} (>{self.ratelimit.limit}/"
                f"{self.ratelimit.window_s}s). Activation rejected.",
                name=BUS_NAME + ".RateLimited",
            )
        provenance = ("launcher_gated" if SECCTX_LAUNCHER_GATED
                      else "advisory")
        # Same-silo: same Option-B gate as the clipboard methods.
        if src == dst and src and bool(identity_verified):
            try:
                self.audit.log(
                    caller_uid=uid, caller_pid=pid, caller_exe=exe,
                    action=action_s, decision=True, scope=None,
                    source=(f"handoff_same_silo_verified "
                            f"secctx_provenance={provenance} "
                            f"src_app={sapp} "
                            f"dst_app={dapp} src_engine={seng}"),
                    approver_uid=None,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[broker] qdistro.audit.failure: handoff_same_silo,"
                      f" reason={e!r}", flush=True)
            return "allow"
        if src == dst and src and not bool(identity_verified):
            if not SECCTX_LAUNCHER_GATED:
                print(f"[broker] WARN secctx advisory: same-silo "
                      f"handoff activation {src} without identity "
                      f"verification; secctx is self-asserted",
                      flush=True)
        # Pass source app_id + sandbox_engine to the rule matcher; rules
        # naming app_id or sandbox_engine selectors only match when the
        # caller propagated those (via qdwin_shell_v1@v13 secctx).
        rule = self.rules.match(uid=uid, action=action_s, exe=exe,
                                app_id=sapp_raw,
                                sandbox_engine=seng_raw)
        decision = "deny"
        rule_path = None
        source_label = "handoff_default_deny"
        if rule is not None:
            decision = "allow" if rule.decision == "allow" else "deny"
            rule_path = rule.source_path
            source_label = "handoff_rule"
        try:
            self.audit.log(
                caller_uid=uid, caller_pid=pid, caller_exe=exe,
                action=action_s, decision=(decision == "allow"),
                scope=None,
                source=(f"{source_label} "
                        f"secctx_provenance={provenance} "
                        f"src_app={sapp} dst_app={dapp} "
                        f"src_engine={seng}"),
                approver_uid=None, rule_path=rule_path,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[broker] qdistro.audit.failure: handoff, "
                  f"reason={e!r}", flush=True)
        return decision

    @dbus.service.method(BUS_NAME,
                         in_signature="ssssssiissd", out_signature="b",
                         sender_keyword="sender", connection_keyword="conn")
    def RecordSelinuxAvc(self, scontext: str, tcontext: str,
                         tclass: str, perms: str, verdict: str,
                         comm: str, permissive: int, pid: int,
                         exe: str, path: str, ts: float,
                         sender=None, conn=None) -> bool:
        """Append one audit row for an SELinux AVC denial routed via
        audispd. spec/30 step 7.

        Restricted to root callers — the audispd plugin runs as root
        as a child of the auditd service. Other uids cannot inject
        synthetic AVC rows. The dbus policy file pins the same boundary;
        this is defense-in-depth.

        Records whose ``scontext`` does NOT name a `qdistro_*_t`
        subject domain are silently dropped (return False) so a
        misconfigured plugin that forwards every AVC line cannot
        flood the audit table with unrelated kernel denials. The
        plugin already filters; the broker double-checks. Returns
        True iff the row was written.

        ``ts`` is the kernel-supplied audit message time (epoch
        seconds, fractional ms). The broker uses ``int(ts)`` for the
        audit row timestamp so ordering against prompt/cache rows
        stays monotonic; the high-precision value is preserved in
        the source-tag for forensic correlation against
        /var/log/audit/audit.log.

        action shape: ``selinux.avc:<tclass>:<perms>``. The colon-
        joined perms string survives admin-app filtering on the
        History tab; ``selinux:`` prefix means a wildcard search of
        "selinux" pulls every AVC row.
        """
        caller_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if caller_uid != 0:
            raise dbus.DBusException(
                f"RecordSelinuxAvc restricted to root caller; "
                f"got uid {caller_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        # Defensive: pull subj_type out of scontext directly rather
        # than trust the plugin. The format is well-known
        # (user:role:type:level); a malformed scontext skips the row.
        scontext_s = str(scontext or "")
        parts = scontext_s.split(":")
        if len(parts) < 3:
            return False
        subj_type = parts[2]
        if not is_qdistro_subj_type(subj_type):
            return False
        tclass_s = str(tclass or "")[:64]
        perms_s = str(perms or "")[:128]
        verdict_s = str(verdict or "denied")[:16]
        comm_s = str(comm or "")[:64]
        path_s = str(path or "")[:512]
        exe_s = str(exe or "")[:512]
        action_s = f"selinux.avc:{tclass_s}:{perms_s}"
        # decision: True for `granted` (informational), False for
        # `denied` (the load-bearing case). permissive=1 is still a
        # `denied` verdict with no enforcement; we preserve the
        # verdict in the source-tag so admin sees the distinction.
        decision = (verdict_s == "granted")
        try:
            ts_int = int(float(ts))
        except (TypeError, ValueError):
            ts_int = 0
        source = (f"selinux_avc verdict={verdict_s} "
                  f"permissive={int(permissive)} "
                  f"tcontext={str(tcontext or '')[:128]} "
                  f"path={path_s or '(none)'} "
                  f"comm={comm_s or '(none)'} "
                  f"audit_ts={ts_int}")
        try:
            self.audit.log(
                caller_uid=0,  # AVC is kernel-attributed, not uid-attributed
                caller_pid=int(pid) if int(pid) > 0 else 0,
                caller_exe=exe_s or comm_s or "(unknown)",
                action=action_s,
                decision=decision,
                scope=None,
                source=source,
                approver_uid=None,
                selinux_subj_type=subj_type,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[broker] qdistro.audit.failure: selinux_avc, "
                  f"reason={e!r}", flush=True)
            return False
        return True

    @dbus.service.method(BUS_NAME,
                         in_signature="iissa{sv}", out_signature="i",
                         sender_keyword="sender", connection_keyword="conn")
    def RequestPermissionAs(self, caller_uid: int, caller_pid: int,
                             caller_exe: str, action: str, details: dict,
                             sender=None, conn=None) -> int:
        """Delegated request: the calling process (policy-restricted to
        the qdistro-root-exec service running as root) attests to the
        real caller's uid/pid/exe. The broker treats this as authoritative
        for rules/cache/audit but flags the resulting request as
        delegated so DecideRequest can refuse long-lived scopes (otherwise
        one approve click persists trust against an identity the broker
        never authenticated directly).
        """
        delegator_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        # Root is the only delegator. Other uids, including ADMIN_UID,
        # cannot impersonate arbitrary callers — otherwise the admin app
        # could laundery any request. The dbus policy file enforces the
        # same boundary; this is defense-in-depth.
        if delegator_uid != 0:
            raise dbus.DBusException(
                f"RequestPermissionAs restricted to root delegator; "
                f"got uid {delegator_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        # Capture the claimed pid's current start_time so TOCTOU checks
        # at decide-time still work for delegated requests.
        _exe2, start_time = _read_proc_identity(int(caller_pid))
        return self._enqueue(int(caller_uid), int(caller_pid),
                             str(caller_exe), start_time,
                             str(action), details, delegated=True)

    @dbus.service.method(BUS_NAME, in_signature="", out_signature="a(iss)",
                         sender_keyword="sender", connection_keyword="conn")
    def ListReceivers(self, sender=None, conn=None):
        """Return [(uid, service_name, friendly_name)] for every
        org.qdistro.App1 receiver currently registered across all
        running user sessions.

        Readable by any uid — this is the data the "Send to…" menu
        is built from, so sender apps in non-admin uids need it.
        The approval gate is on RelayMessage; ListReceivers only
        leaks "uid X has an app named Y" which is inherent to any
        cross-user send-to UI.
        """
        out: list = []
        system_bus = dbus.SystemBus()
        try:
            entries = os.listdir("/run/user")
        except OSError:
            return dbus.Array([], signature="(iss)")
        for d in entries:
            try:
                uid = int(d)
            except ValueError:
                continue
            relay_name = USER_RELAY_SYSTEM_NAME_FMT.format(uid=uid)
            try:
                relay = system_bus.get_object(relay_name,
                                              USER_RELAY_OBJ_PATH)
                rows = relay.ListLocalReceivers(
                    dbus_interface=USER_RELAY_IFACE,
                    timeout=5.0)
                for r in rows:
                    svc = str(r[0])
                    friendly = str(r[1])
                    out.append((dbus.Int32(uid),
                                dbus.String(svc),
                                dbus.String(friendly)))
            except Exception as e:  # noqa: BLE001
                # A uid without the relay running (e.g. admin itself,
                # or a user that hasn't logged in) is normal — skip
                # quietly with one log line for diagnosis.
                print(f"[broker] ListReceivers: uid={uid} skipped: {e}",
                      flush=True)
        return dbus.Array(out, signature="(iss)")

    @dbus.service.method(BUS_NAME, in_signature="isss", out_signature="",
                         async_callbacks=("_reply", "_error"),
                         sender_keyword="sender", connection_keyword="conn")
    def RelayMessage(self, target_uid, target_service, kind, payload,
                     _reply, _error, sender=None, conn=None):
        """Cross-user app message. Admin approves each cross-silo send
        individually (one-shot; only scope='once' is permitted). On
        allow, broker opens the target uid's session bus and asks
        UserRelay.Forward to invoke <target_service>.Receive(kind,
        payload).

        Same-silo (caller_uid == target_uid) sends bypass the admin
        prompt — the two apps already share a unix uid + session bus,
        so the relay only saves the sender the cost of resolving the
        target service itself. The audit row is still written
        (source="same_silo") so admins can review what flowed.
        """
        try:
            caller_uid, caller_pid, caller_exe, start_time = self._peer_info(sender, conn)
            target_uid_i = int(target_uid)
            target_service_s = str(target_service)
            kind_s = str(kind)
            payload_s = str(payload)
            if target_uid_i < 0:
                raise dbus.DBusException(
                    f"target_uid must be >= 0, got {target_uid_i}",
                    name=BUS_NAME + ".BadArgument")
            if not _SERVICE_NAME_RE.match(target_service_s):
                raise dbus.DBusException(
                    f"target_service {target_service_s!r} does not match "
                    f"expected org.qdistro.* shape",
                    name=BUS_NAME + ".BadArgument")
            same_silo = (int(caller_uid) == target_uid_i)
            # P02 silo-active gate. Same-silo skips it: the caller is
            # already running as that uid, so "silo is not Active" is
            # impossible by construction (a frozen silo can't make
            # outbound D-Bus calls).
            if not same_silo:
                silo_state = self._silo_state(target_uid_i)
                if silo_state == "Unreachable":
                    raise dbus.DBusException(
                        f"session manager unreachable; refusing cross-uid "
                        f"relay to uid {target_uid_i} (require_silo_active=on)",
                        name=BUS_NAME + ".SiloManagerUnreachable")
                if silo_state is not None and silo_state != "Active":
                    raise dbus.DBusException(
                        f"target silo for uid {target_uid_i} is "
                        f"{silo_state!r}, not Active",
                        name=BUS_NAME + ".SiloNotActive")
            action_s = f"app.send-to:{target_uid_i}:{target_service_s}"
        except dbus.DBusException as e:
            _error(e)
            return
        except Exception as e:  # noqa: BLE001
            _error(dbus.DBusException(str(e),
                                      name=BUS_NAME + ".Internal"))
            return

        # Same-silo fast path — no admin prompt, just forward and
        # audit. Wrapped in a try so audit-log failures don't sink
        # delivery (audit failure mode mirrors the cross-silo path
        # below, which also surfaces the structured warning rather
        # than rejecting).
        if same_silo:
            try:
                self._relay_forward(target_uid_i, target_service_s,
                                    kind_s, payload_s)
            except dbus.DBusException as e:
                _error(e)
                return
            except Exception as e:  # noqa: BLE001
                _error(dbus.DBusException(
                    f"relay forward failed: {e}",
                    name=BUS_NAME + ".RelayFailed"))
                return
            try:
                self.audit.log(
                    caller_uid=caller_uid, caller_pid=caller_pid,
                    caller_exe=caller_exe,
                    action=action_s, decision=True, scope="once",
                    source=f"same_silo kind={kind_s} "
                           f"target={target_service_s}",
                    approver_uid=None,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[broker] qdistro.audit.failure: same_silo "
                      f"relay uid={caller_uid} target={target_service_s}: "
                      f"{e}", flush=True)
            _reply()
            return

        try:
            details = {
                "kind": kind_s,
                # Payload is rendered verbatim in the admin UI; the
                # detail sanitiser in _enqueue strips control chars
                # and truncates, so no extra escaping needed here.
                "payload": payload_s,
                "target_uid": str(target_uid_i),
                "target_service": target_service_s,
            }
            rid = self._enqueue(caller_uid, caller_pid, caller_exe,
                                start_time, action_s, details,
                                delegated=False, one_shot=True)
        except dbus.DBusException as e:
            _error(e)
            return
        except Exception as e:  # noqa: BLE001
            _error(dbus.DBusException(str(e),
                                      name=BUS_NAME + ".Internal"))
            return

        # Hook a waiter that, on allow, performs the forward on the
        # mainloop thread (DecideRequest invokes reply_cb from there,
        # holding no locks we also hold). On deny we surface a
        # Denied DBusException so the caller SDK doesn't need a second
        # "was it approved" boolean on the wire.
        def relay_reply(allowed: bool):
            if not allowed:
                _error(dbus.DBusException(
                    "admin denied relay",
                    name=BUS_NAME + ".Denied"))
                return
            try:
                self._relay_forward(target_uid_i, target_service_s,
                                    kind_s, payload_s)
            except dbus.DBusException as e:
                _error(e)
                return
            except Exception as e:  # noqa: BLE001
                _error(dbus.DBusException(
                    f"relay forward failed: {e}",
                    name=BUS_NAME + ".RelayFailed"))
                return
            _reply()

        with self._lock:
            req = self._pending.get(rid)
            if req is None:
                _error(dbus.DBusException(
                    "request vanished before waiter registration",
                    name=BUS_NAME + ".Internal"))
                return
            if req.decision is not None:
                # one_shot skips rules/cache, so this branch should
                # only fire on a fast-path deny via some future
                # mechanism. Handle it symmetrically.
                decided = bool(req.decision)
                dispatch = lambda: relay_reply(decided)  # noqa: E731
            else:
                req.waiters.append((relay_reply, _error))
                dispatch = None
        if dispatch is not None:
            dispatch()

    def _silo_state(self, target_uid: int) -> str | None:
        """Ask the session manager for the silo state of `target_uid`.

        Returns the state string ('Created'/'Active'/'Frozen'/...) when
        the manager has a row, None when the manager has no row but
        was reachable, or the sentinel "Unreachable" when REQUIRE_
        SILO_ACTIVE is on and the manager isn't responding. ADMIN_UID
        short-circuits to 'Active' — admin never appears in silos.yaml.

        Called by RelayMessage; overridable by the broker test stub.

        Fail-open vs fail-closed: the legacy (default) behaviour is to
        return None on every error so RelayMessage falls through to
        the pre-P02 trust path. Every fail-open branch logs a
        structured warning so operators auditing "why was this relay
        allowed?" have a breadcrumb. Hosts can flip to fail-closed by
        setting QDISTRO_BROKER_REQUIRE_SILO_ACTIVE=true or
        require_silo_active=true in /etc/qdistro/broker.conf — then
        every error becomes "Unreachable" and RelayMessage refuses.
        """
        if int(target_uid) == ADMIN_UID:
            return "Active"
        fail_token = "Unreachable" if REQUIRE_SILO_ACTIVE else None
        try:
            import json as _json
            system_bus = dbus.SystemBus()
            mgr = system_bus.get_object(SESSION_MANAGER_BUS_NAME,
                                        SESSION_MANAGER_OBJ_PATH)
            raw = mgr.ListSilos(dbus_interface=SESSION_MANAGER_IFACE,
                                timeout=2.0)
            for row in _json.loads(str(raw)):
                if int(row.get("uid", -1)) == int(target_uid):
                    return str(row.get("state", ""))
            return None
        except dbus.DBusException as e:
            name = e.get_dbus_name() or ""
            if name.endswith(".ServiceUnknown") or name.endswith(".NameHasNoOwner"):
                print(
                    f"[broker] WARN _silo_state({target_uid}): session "
                    f"manager not on bus ({name}); "
                    f"{'failing closed' if REQUIRE_SILO_ACTIVE else 'falling through to legacy trust'}",
                    flush=True)
                return fail_token
            print(
                f"[broker] WARN _silo_state({target_uid}) DBusException: "
                f"{e!r}; "
                f"{'failing closed' if REQUIRE_SILO_ACTIVE else 'falling through to legacy trust'}",
                flush=True)
            return fail_token
        except Exception as e:  # noqa: BLE001
            print(
                f"[broker] WARN _silo_state({target_uid}) error: {e!r}; "
                f"{'failing closed' if REQUIRE_SILO_ACTIVE else 'falling through to legacy trust'}",
                flush=True)
            return fail_token

    def _relay_forward(self, target_uid: int, target_service: str,
                       kind: str, payload: str) -> None:
        """Invoke UserRelay.Forward on `target_uid`'s relay via the
        system bus. The relay in turn addresses the receiver on its
        own session bus.
        """
        relay_name = USER_RELAY_SYSTEM_NAME_FMT.format(uid=int(target_uid))
        system_bus = dbus.SystemBus()
        try:
            relay = system_bus.get_object(relay_name, USER_RELAY_OBJ_PATH)
            relay.Forward(target_service, kind, payload,
                          dbus_interface=USER_RELAY_IFACE,
                          timeout=10.0)
        except dbus.DBusException as e:
            # Bubble up a cleaner error when the relay simply isn't
            # running for that uid (user hasn't logged in / linger off).
            name = e.get_dbus_name() or ""
            if name.endswith(".ServiceUnknown") or name.endswith(".NameHasNoOwner"):
                raise dbus.DBusException(
                    f"relay not running for uid {target_uid} "
                    f"({relay_name} unowned)",
                    name=BUS_NAME + ".TargetNotReady")
            raise

    def _enqueue(self, uid: int, pid: int, exe: str, start_time: int,
                 action_s: str, details: dict, *, delegated: bool,
                 one_shot: bool = False) -> int:
        if not self.ratelimit.check(uid, action_s):
            # Audit the rejection so admin sees the offender. We do not
            # fail-closed on audit failure here — rate-limit rejections
            # are high-volume by nature; letting a broken audit log stop
            # us from rejecting would only amplify the DOS.
            try:
                self.audit.log(
                    caller_uid=uid, caller_pid=pid, caller_exe=exe,
                    action=action_s, decision=False, scope=None,
                    source="rate_limit", approver_uid=None,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[broker] qdistro.audit.failure: rate_limit path, reason={e!r}", flush=True)
            raise dbus.DBusException(
                f"Rate limit exceeded for uid={uid} action={action_s!r} "
                f"(>{self.ratelimit.limit}/{self.ratelimit.window_s}s). "
                "Request rejected.",
                name=BUS_NAME + ".RateLimited",
            )
        # Resolution order per spec/07: rules first, cache second,
        # hooks third, prompt last. A rule-matched decision is
        # authoritative — it doesn't flow through the admin prompt even
        # for allows. one_shot actions skip all tiers: every call
        # reaches admin.
        #
        # Tier-2 admission security (audit 2026-05-27): when all four
        # tiers (rules, cache, hooks, prompt) are exhausted without a
        # pre-decision, the request stays pending in _pending until
        # admin acts — this is operationally default-deny.  There is no
        # hardcoded allow for sandbox_engine="qdistro.tier2" or any
        # other sandbox_engine value.  The cross-silo gates
        # (CheckClipboardTransfer, CheckClipboardReceive,
        # CheckHandoffActivation) are even stricter: they return
        # "deny" when rules.match() returns None, without reaching the
        # prompt queue at all.
        matched_rule = None
        cached_row = None
        hook_verdict = None
        if not one_shot:
            argv = _argv_from_details(details)
            matched_rule = self.rules.match(
                uid=uid, action=action_s, exe=exe,
                app_id=_selector_from_details(details, "app_id"),
                sandbox_engine=_selector_from_details(
                    details, "sandbox_engine"),
                mime_type=_selector_from_details(details, "mime_type"),
                argv=argv,
            )
            if matched_rule is None:
                cached_row = self.cache.lookup_detail(uid, action_s, exe, argv)

        # Sanitise caller-supplied details before storing them. The
        # admin UI (GUI/TUI) renders these verbatim; without scrubbing,
        # a hostile caller can inject ANSI escapes or newlines that
        # draw fake approval banners inside the detail pane.
        clean_details = _sanitize_details(details)

        # spec/25 §Phase-2: snapshot layered identity at request time
        # (process may exit before admin clicks Approve, in which case
        # the cached values are all admin gets). Done outside the lock
        # — exe-hash IO can stretch into the tens of milliseconds and
        # we don't want to serialise concurrent RequestPermission
        # callers behind it.
        layered = _read_proc_layered(pid) if pid > 0 else {
            "exe_sha256": "", "selinux_label": "", "cgroup": ""}

        # Hook consultation: when rules and cache are both inconclusive,
        # ask the sandboxed hook executor before falling through to the
        # admin prompt. Done outside the lock and before creating a
        # _Request — the hook query is I/O-bound (AF_UNIX round-trip)
        # and we don't want to hold the lock during it.
        if not one_shot and matched_rule is None and cached_row is None:
            try:
                hook_event = dict(clean_details)
                hook_event["caller_uid"] = uid
                hook_event["caller_pid"] = pid
                hook_event["caller_exe"] = exe
                hook_event["action_full"] = action_s
                hook_verdict = self.hooks.query(action_s, hook_event)
            except Exception as e:  # noqa: BLE001
                print(f"[broker] hook query failed: {e!r}", flush=True)
                hook_verdict = None

        with self._lock:
            rid = self._next_id
            self._next_id += 1
            req = _Request(rid, uid, pid, exe, start_time, action_s,
                           clean_details, delegated=delegated,
                           one_shot=one_shot,
                           exe_sha256=layered["exe_sha256"],
                           selinux_label=layered["selinux_label"],
                           cgroup=layered["cgroup"])
            if matched_rule is not None:
                req.decision = (matched_rule.decision == "allow")
            elif cached_row is not None:
                req.decision = bool(cached_row["decision"])
            elif hook_verdict is not None:
                verdict_val = hook_verdict.get("verdict")
                if verdict_val == "allow":
                    req.decision = True
                elif verdict_val == "deny":
                    req.decision = False
                # "transform" is treated as allow (the payload mutation
                # is out-of-band; the broker's decision is binary).
                elif verdict_val == "transform":
                    req.decision = True
            self._pending[rid] = req

        if req.decision is None:
            self.RequestPending(rid)
            return rid

        # Decided immediately — record it. We hold the lock during the
        # audit write so a concurrent RevokeApproval can't see "cache
        # row gone" before the audit row lands (the "waiter sees True
        # means trail exists" invariant).
        with self._lock:
            if matched_rule is not None:
                rule_scope = matched_rule.scope
                print(f"[broker] rule match: uid={uid} action={action_s!r} exe={exe!r} "
                      f"-> {req.decision} (rule={matched_rule.name!r} "
                      f"at {matched_rule.source_path}, scope={rule_scope})",
                      flush=True)
                try:
                    self.audit.log(
                        caller_uid=uid, caller_pid=pid, caller_exe=exe,
                        action=action_s, decision=req.decision,
                        scope=rule_scope, source="rule", approver_uid=None,
                        rule_path=matched_rule.source_path,
                        request_id=rid,
                        argv=argv if argv else None,
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"[broker] qdistro.audit.failure: rule path, reason={e!r}", flush=True)
                # If the rule specified a scope, materialize a cache row
                # so subsequent requests hit the cheaper cache path.
                if req.decision and rule_scope:
                    try:
                        self.cache.store(uid, action_s, exe, rule_scope,
                                         True, 0,  # approver_uid=0: rules have no human approver
                                         argv=argv)
                    except Exception as e:  # noqa: BLE001
                        print(f"[broker] rule cache.store failed: {e}", flush=True)
            elif cached_row is not None:
                scope_s = cached_row.get("scope") if cached_row else None
                print(f"[broker] cache hit: uid={uid} action={action_s!r} exe={exe!r} "
                      f"-> {req.decision} (scope={scope_s})", flush=True)
                try:
                    self.audit.log(
                        caller_uid=uid, caller_pid=pid, caller_exe=exe,
                        action=action_s, decision=req.decision,
                        scope=scope_s, source="cache", approver_uid=None,
                        request_id=rid,
                        argv=argv if argv else None,
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"[broker] qdistro.audit.failure: cache path, reason={e!r}", flush=True)
            elif hook_verdict is not None:
                verdict_val = hook_verdict.get("verdict", "")
                reason = hook_verdict.get("reason", "")
                print(f"[broker] hook verdict: uid={uid} action={action_s!r} "
                      f"exe={exe!r} -> {verdict_val} "
                      f"(reason={reason!r})", flush=True)
                try:
                    self.audit.log(
                        caller_uid=uid, caller_pid=pid, caller_exe=exe,
                        action=action_s, decision=req.decision,
                        scope=None, source=f"hook verdict={verdict_val} reason={reason}",
                        approver_uid=None,
                        request_id=rid,
                        argv=argv if argv else None,
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"[broker] qdistro.audit.failure: hook path, reason={e!r}", flush=True)
        return rid

    @dbus.service.method(BUS_NAME, in_signature="i", out_signature="b",
                        async_callbacks=("_reply", "_error"))
    def WaitForDecision(self, request_id: int, _reply, _error):
        with self._lock:
            req = self._pending.get(int(request_id))
            if req is None:
                _reply(False)
                return
            if req.decision is not None:
                _reply(bool(req.decision))
                return
            req.waiters.append((_reply, _error))

    @dbus.service.method(BUS_NAME, in_signature="", out_signature="aa{sv}")
    def GetPending(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for r in self._pending.values():
                if r.decision is not None:
                    continue
                out.append({
                    "id": dbus.Int32(r.id),
                    "uid": dbus.Int32(r.uid),
                    "pid": dbus.Int32(r.pid),
                    "exe": dbus.String(r.exe),
                    "action": dbus.String(r.action),
                    "details": dbus.Dictionary(r.details, signature="ss"),
                    # spec/25 §Phase-2 layered identity (always present;
                    # empty string when /proc data wasn't available at
                    # request time — process gone, no SELinux, etc.).
                    "exe_sha256":    dbus.String(r.exe_sha256),
                    "selinux_label": dbus.String(r.selinux_label),
                    "cgroup":        dbus.String(r.cgroup),
                })
            return out

    @dbus.service.method(BUS_NAME, in_signature="iss", out_signature="", sender_keyword="sender", connection_keyword="conn")
    def DecideRequest(self, request_id: int, decision: str, scope: str, sender=None, conn=None):
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid != ADMIN_UID:
            raise dbus.DBusException(f"DecideRequest restricted to admin uid {ADMIN_UID}; got {admin_uid}",
                                     name=BUS_NAME + ".AccessDenied")
        decision_s = str(decision)
        if decision_s not in ("allow", "deny"):
            raise dbus.DBusException(
                f"decision must be 'allow' or 'deny', got {decision_s!r}",
                name=BUS_NAME + ".BadArgument",
            )
        scope_s = str(scope)
        if scope_s not in _VALID_SCOPES:
            raise dbus.DBusException(
                f"scope must be one of {sorted(_VALID_SCOPES)}, got {scope_s!r}",
                name=BUS_NAME + ".BadArgument",
            )
        with self._lock:
            req = self._pending.get(int(request_id))
            if req is None or req.decision is not None:
                return
            # Delegated requests can't produce long-lived grants — the
            # broker never authenticated the claimed peer identity
            # itself, so persisting trust against it would let one
            # admin click persist past the original call's pid.
            if req.delegated and scope_s in _DELEGATED_FORBIDDEN_SCOPES:
                raise dbus.DBusException(
                    f"scope {scope_s!r} not permitted for delegated requests; "
                    f"use 'once' or an argv-pinned scope "
                    f"('forever_argv', 'forever_basename', 'forever_prefix')",
                    name=BUS_NAME + ".ScopeNotPermitted",
                )
            if req.one_shot and scope_s in _ONESHOT_FORBIDDEN_SCOPES:
                raise dbus.DBusException(
                    f"scope {scope_s!r} not permitted for one-shot requests; "
                    f"use 'once'",
                    name=BUS_NAME + ".ScopeNotPermitted",
                )
            # TOCTOU re-check: if the pid has been recycled between
            # RequestPermission and this DecideRequest, the admin's
            # click is about a different process than the UI showed.
            # Directly-authenticated requests only — delegated requests
            # never trusted pid alone.
            if not req.delegated and req.pid > 0:
                _exe_now, st_now = _read_proc_identity(req.pid)
                if req.start_time != 0 and st_now != 0 and st_now != req.start_time:
                    req.decision = False  # force deny
                    waiters = list(req.waiters); req.waiters.clear()
                    self.RequestDecided(int(request_id), "deny")
                    for reply_cb, _err in waiters:
                        try: reply_cb(False)
                        except Exception as e:  # noqa: BLE001
                            print(f"[broker] reply_cb failed: {e}", flush=True)
                    raise dbus.DBusException(
                        f"caller pid {req.pid} has been recycled "
                        f"(start_time changed); refusing to apply decision",
                        name=BUS_NAME + ".CallerGone",
                    )
            allowed = (decision_s == "allow")
            req.decision = allowed
            waiters = list(req.waiters)
            req.waiters.clear()
            cache_uid, cache_pid, cache_action, cache_exe = req.uid, req.pid, req.action, req.exe
            # task(069): argv comes from the cached details (qsu's
            # per-element argv[NN] keys). Empty list when the request
            # carried no argv (clipboard / handoff / qdistro.test.* —
            # those use exe_only / always scopes anyway).
            cache_argv = _argv_from_details(req.details)

        # Audit first — if it fails and AUDIT_REQUIRED, force-deny before
        # we touch the cache or release waiters. Otherwise an admin's
        # Approve can grant access without a durable record.
        try:
            self.audit.log(
                caller_uid=cache_uid, caller_pid=cache_pid, caller_exe=cache_exe,
                action=cache_action, decision=allowed,
                scope=scope_s, source="prompt", approver_uid=admin_uid,
                request_id=int(request_id),
                argv=cache_argv if cache_argv else None,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[broker] qdistro.audit.failure: prompt path, reason={e!r}", flush=True)
            if AUDIT_REQUIRED:
                # Downgrade to deny — waiters get False, cache is not
                # written, admin sees the failure as a DBusException.
                with self._lock:
                    req2 = self._pending.get(int(request_id))
                    if req2 is not None:
                        req2.decision = False
                for reply_cb, _err in waiters:
                    try:
                        reply_cb(False)
                    except Exception as re:  # noqa: BLE001
                        print(f"[broker] reply_cb failed: {re}", flush=True)
                self.RequestDecided(int(request_id), "deny")
                raise dbus.DBusException(
                    f"Audit log unavailable; decision not recorded ({e}). "
                    "Request denied.",
                    name=BUS_NAME + ".AuditUnavailable",
                )

        # Cache writes only happen on allow, and only after audit has
        # succeeded — we never extend trust past a failed audit.
        # one_shot explicitly skips the cache: scope_s is 'once' for
        # these (enforced above) so scope_to_row would already be None,
        # but an explicit guard makes the intent obvious.
        if allowed and not req.one_shot:
            try:
                wrote = self.cache.store(cache_uid, cache_action, cache_exe,
                                          scope_s, allowed, admin_uid,
                                          argv=cache_argv)
                if wrote:
                    print(f"[broker] cached: uid={cache_uid} action={cache_action!r} "
                          f"exe={cache_exe!r} scope={scope}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[broker] cache.store failed: {e}", flush=True)

        for reply_cb, _err in waiters:
            try:
                reply_cb(bool(allowed))
            except Exception as e:  # noqa: BLE001
                print(f"[broker] reply_cb failed: {e}", flush=True)
        self.RequestDecided(int(request_id), "allow" if allowed else "deny")

    @dbus.service.method(BUS_NAME, in_signature="", out_signature="aa{sv}",
                         sender_keyword="sender", connection_keyword="conn")
    def ListCache(self, sender=None, conn=None) -> list[dict]:
        """Return every cached approval row. Admin/root only.

        Read-only, no audit row — the cache tab will call this every
        time it refreshes. Same authz shape as GetPending / the revoke
        methods: policy deny for non-admin at the bus level, in-process
        uid check as defense-in-depth.
        """
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"ListCache restricted to admin/root; got uid {admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        out = []
        for r in self.cache.list_all():
            out.append({
                "id":            dbus.Int32(r["id"]),
                "caller_uid":    dbus.Int32(r["caller_uid"]),
                "action":        dbus.String(r["action"]),
                "match_kind":    dbus.String(r["match_kind"] or ""),
                "match_value":   dbus.String(r["match_value"] or ""),
                "decision":      dbus.Boolean(bool(r["decision"])),
                "scope":         dbus.String(r["scope"] or ""),
                "approver_uid":  dbus.Int32(r["approver_uid"] or 0),
                # sqlite INTEGER can be NULL for forever scopes; D-Bus
                # has no explicit null — encode absent expiry as 0 and
                # document at the receiver.
                "expires_at":    dbus.Int64(r["expires_at"] or 0),
                "created_at":    dbus.Int64(r["created_at"] or 0),
            })
        return out

    @dbus.service.method(BUS_NAME, in_signature="i", out_signature="b",
                         sender_keyword="sender", connection_keyword="conn")
    def RevokeApproval(self, approval_id: int, sender=None, conn=None) -> bool:
        """Delete one cached approval by id. Admin-only.

        Returns True if a row was deleted. An audit row is written even
        on a no-op (row absent) so the attempt is traceable; on audit
        failure we propagate the DBusException to the caller rather
        than silently revoking without a record.
        """
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        # root (uid 0) is the broker's own uid and historically the CLI's
        # uid; accept it alongside the admin role so `qdistro-approvals
        # revoke` works when invoked from a root shell.
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"RevokeApproval restricted to admin/root; got uid {admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        row = self.cache.delete_by_id(int(approval_id))
        if row is None:
            return False
        self._audit_revoke(row, admin_uid)
        self.ApprovalRevoked(int(row["caller_uid"]), str(row["action"]),
                             str(row["match_value"] or ""))
        print(f"[broker] revoked approval id={row['id']} uid={row['caller_uid']} "
              f"action={row['action']!r} by admin_uid={admin_uid}", flush=True)
        return True

    @dbus.service.method(BUS_NAME, in_signature="i", out_signature="i",
                         sender_keyword="sender", connection_keyword="conn")
    def RevokeAllForUid(self, caller_uid: int, sender=None, conn=None) -> int:
        """Delete every cached approval for a caller uid. Admin-only."""
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"RevokeAllForUid restricted to admin/root; got uid {admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        rows = self.cache.delete_by_uid(int(caller_uid))
        for row in rows:
            self._audit_revoke(row, admin_uid)
            self.ApprovalRevoked(int(row["caller_uid"]), str(row["action"]),
                                 str(row["match_value"] or ""))
        if rows:
            print(f"[broker] revoked {len(rows)} approval(s) for uid={caller_uid} "
                  f"by admin_uid={admin_uid}", flush=True)
        return len(rows)

    @dbus.service.method(BUS_NAME, in_signature="", out_signature="i",
                         sender_keyword="sender", connection_keyword="conn")
    def RunCacheGc(self, sender=None, conn=None) -> int:
        """Delete expired cache rows now. Admin/root only.

        Parallels RunAuditGc. The periodic `_gc_tick` does the same
        work every minute; this method lets the CLI + admin app
        trigger it on demand without writing to sqlite behind the
        broker's back.
        """
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"RunCacheGc restricted to admin/root; got uid {admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        try:
            n = self.cache.gc()
        except Exception as e:  # noqa: BLE001
            raise dbus.DBusException(
                f"cache.gc failed: {e}",
                name=BUS_NAME + ".CacheGcFailed",
            )
        print(f"[broker] cache.gc on-demand: {n} rows deleted by "
              f"admin_uid={admin_uid}", flush=True)
        return int(n)

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="s",
                         sender_keyword="sender", connection_keyword="conn")
    def SaveRule(self, filename: str, yaml_body: str,
                 sender=None, conn=None) -> str:
        """Write a YAML rule file to /etc/qdistro/rules.d/ and reload.

        Powered by the admin-approval-app's "Rule from this" path
        (spec/25 §Phase-2). Admin/root only.

        Validation:
          - filename must match `[A-Za-z0-9_-]+\\.yaml` (no slashes,
            no traversal, fixed extension).
          - yaml_body must parse via PyYAML and contain a top-level
            list whose entries match the rules-engine schema (delegated
            to a dry-run load on a tempfile).

        Returns the absolute path on success; raises a DBusException
        with `RulesEngineRefused` on validation failure.
        """
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"SaveRule restricted to admin/root; got uid {admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        import re, tempfile, shutil
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.yaml", filename):
            raise dbus.DBusException(
                f"SaveRule: filename {filename!r} must match "
                "[A-Za-z0-9_-]+.yaml",
                name=BUS_NAME + ".RulesEngineRefused",
            )
        # Reject allow-all rules (empty match + decision=allow) server-side.
        import yaml as _yaml
        try:
            entries = _yaml.safe_load(yaml_body) or []
        except Exception:
            entries = []
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("decision") != "allow":
                    continue
                match = entry.get("match") or {}
                if isinstance(match, dict) and not any(
                        v not in (None, "", []) for v in match.values()):
                    raise dbus.DBusException(
                        "SaveRule: rule with decision=allow must have at "
                        "least one non-empty match selector",
                        name=BUS_NAME + ".RulesEngineRefused",
                    )
        # Validate via a tempfile load through the same rules engine.
        from qdistro_admin_rules import RulesEngine  # type: ignore
        with tempfile.TemporaryDirectory(prefix="qd-rules-validate-") as td:
            tmp = os.path.join(td, filename)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(yaml_body)
            check = RulesEngine(td)
            errs = check.load_errors()
            if errs:
                raise dbus.DBusException(
                    f"SaveRule: rule validation failed: " + "; ".join(errs),
                    name=BUS_NAME + ".RulesEngineRefused",
                )
        # Write to whichever directory this broker's RulesEngine
        # watches — not always /etc/qdistro/rules.d (tests substitute a
        # tmp_path-backed RulesEngine; production wires it to the
        # standard path via Broker.__init__).
        target_dir = self.rules._dir
        os.makedirs(target_dir, mode=0o755, exist_ok=True)
        target = os.path.join(target_dir, filename)
        # Atomic replace via tempfile in the same dir.
        with tempfile.NamedTemporaryFile(
                mode="w", dir=target_dir, prefix=".save-rule-",
                suffix=".tmp", delete=False, encoding="utf-8") as f:
            f.write(yaml_body)
            tmp_path = f.name
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, target)
        print(f"[broker] SaveRule wrote {target} (admin_uid={admin_uid})",
              flush=True)
        # Hot reload via the shared helper. The inotify watcher will
        # also fire from the os.replace, but reload_rules_from_disk
        # is fast (re-parse YAML) and emitting twice is benign — both
        # qdshell and the admin app dedup against the rule set on
        # signal receipt.
        self.reload_rules_from_disk(source="dbus-saverule")
        return target

    @dbus.service.method(BUS_NAME, in_signature="", out_signature="ias",
                         sender_keyword="sender", connection_keyword="conn")
    def ReloadRules(self, sender=None, conn=None):
        """Re-walk /etc/qdistro/rules.d and rebuild the rule set.

        Returns (count_loaded, errors). Admin/root only. An empty
        error list means every file parsed cleanly; otherwise each
        entry is a human-readable `<path>: <reason>` string suitable
        for the admin app or CLI to display.
        """
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"ReloadRules restricted to admin/root; got uid {admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        n = self.reload_rules_from_disk(source=f"dbus-uid={admin_uid}")
        errs = self.rules.load_errors()
        return dbus.Int32(n), dbus.Array(errs, signature="s")

    @dbus.service.method(BUS_NAME, in_signature="", out_signature="aa{sv}",
                         sender_keyword="sender", connection_keyword="conn")
    def ListRules(self, sender=None, conn=None) -> list[dict]:
        """Return the loaded rule set for the admin Rules tab.

        Admin/root only. Each entry mirrors the YAML on disk:
          name (str), decision (allow/deny), source_path (str),
          uid (int, 0 = "don't care"), action (str, "" = "don't care"),
          exe (str, "" = "don't care"), scope (str, "" = inherit
          phase-1 'once'), rationale (str).

        D-Bus has no nullable primitive so the "don't care" sentinel
        is uid=-1 / action="" / exe="" / scope="". Callers should
        treat empty strings + uid==-1 as "match anything" — same
        convention RulesEngine uses internally.
        """
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"ListRules restricted to admin/root; got uid {admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        out = []
        for r in self.rules.rules():
            out.append({
                "name":        dbus.String(r.name or ""),
                "decision":    dbus.String(r.decision or ""),
                "source_path": dbus.String(r.source_path or ""),
                "uid":         dbus.Int32(r.uid if r.uid is not None else -1),
                "action":      dbus.String(r.action if r.action is not None else ""),
                "exe":         dbus.String(r.exe if r.exe is not None else ""),
                "app_id":      dbus.String(
                    r.app_id if r.app_id is not None else ""),
                "sandbox_engine": dbus.String(
                    r.sandbox_engine if r.sandbox_engine is not None else ""),
                "mime_type":   dbus.String(
                    r.mime_type if r.mime_type is not None else ""),
                "argv_exact":  dbus.Array(
                    [dbus.String(x) for x in (r.argv_exact or ())],
                    signature="s"),
                "argv_basename": dbus.String(
                    r.argv_basename if r.argv_basename is not None else ""),
                "argv_prefix": dbus.Array(
                    [dbus.String(x) for x in (r.argv_prefix or ())],
                    signature="s"),
                "scope":       dbus.String(r.scope if r.scope is not None else ""),
                "rationale":   dbus.String(r.rationale or ""),
            })
        return out

    @dbus.service.method(BUS_NAME, in_signature="i", out_signature="aa{sv}",
                         sender_keyword="sender", connection_keyword="conn")
    def ListHistory(self, limit: int, sender=None, conn=None) -> list[dict]:
        """Return the N most-recent audit rows for the admin History tab.

        Admin/root only. `limit` capped to [1, 10000] by the audit layer;
        0 or negative is treated as default 200. Nullable columns
        (scope, approver_uid, rule_path, request_id) come back as empty
        string / 0 since D-Bus has no nullable primitive; callers can
        distinguish absence from empty via ``source`` which is always
        populated.
        """
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"ListHistory restricted to admin/root; got uid {admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        n = int(limit) if int(limit) > 0 else 200
        out = []
        for r in self.audit.recent(n):
            # task(073): argv is decoded by audit.recent() as a list[str]
            # or None. D-Bus a{sv} can't carry null, so absent argv is
            # surfaced as a zero-length 'as' — clients distinguish "no
            # argv on this row" (empty) from "this argv element is empty"
            # (single-element list with ""). The broker preserves the
            # exact list emitted at log time (argv elements may contain
            # whitespace; clients must not re-split shlex-joined forms).
            argv_list = r.get("argv") or []
            argv_dbus = dbus.Array(
                [dbus.String(str(a)) for a in argv_list],
                signature="s")
            out.append({
                "ts":            dbus.Int64(r["ts"]),
                "caller_uid":    dbus.Int32(r["caller_uid"]),
                "caller_pid":    dbus.Int32(r["caller_pid"]),
                "caller_exe":    dbus.String(r["caller_exe"] or ""),
                "action":        dbus.String(r["action"] or ""),
                "decision":      dbus.Boolean(bool(r["decision"])),
                "scope":         dbus.String(r["scope"] or ""),
                "source":        dbus.String(r["source"] or ""),
                "approver_uid":  dbus.Int32(r["approver_uid"] or 0),
                "rule_path":     dbus.String(r["rule_path"] or ""),
                "request_id":    dbus.Int32(r["request_id"] or 0),
                "selinux_subj_type": dbus.String(
                    r.get("selinux_subj_type") or ""),
                "argv":          argv_dbus,
            })
        return out

    @dbus.service.method(BUS_NAME, in_signature="i", out_signature="aa{sv}",
                         sender_keyword="sender", connection_keyword="conn")
    def ListPrintAudit(self, limit: int, sender=None, conn=None) -> list[dict]:
        """Return the N most-recent print_audit rows for the admin app.

        Admin/root only — same gate as ListHistory. The print audit
        DB lives at /var/lib/qdistro/audit/print_audit.sqlite (override
        via QDISTRO_PRINT_AUDIT_DB) and is written by qdistro-print-proxy
        per-connection. The broker reads it read-only here so the admin
        UI doesn't need its own connection back to the proxy.

        Returns aa{sv}; nullable cols come back as 0 / "" (D-Bus has no
        null primitive). Returns an empty array if the DB does not yet
        exist (proxy never ran on this host).
        """
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"ListPrintAudit restricted to admin/root; got uid {admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        n = int(limit) if int(limit) > 0 else 200
        n = max(1, min(n, 10000))
        # The print_audit DB lives outside the broker's cache/audit/rule
        # surface — talk to it directly via PrintAuditLog. Module is
        # imported lazily so an old print_audit-less install still
        # boots the broker.
        try:
            import sys as _sys
            here = os.path.dirname(os.path.abspath(__file__))
            cand_dirs = [
                os.path.join(here, "..", "print"),
                "/usr/lib/qdistro/print",
                "/usr/libexec/qdistro/print",
            ]
            for d in cand_dirs:
                d = os.path.abspath(d)
                if os.path.isdir(d) and d not in _sys.path:
                    _sys.path.insert(0, d)
            from qdistro_print_audit import PrintAuditLog  # type: ignore[import-not-found]
        except ImportError:
            return []
        db_path = os.environ.get(
            "QDISTRO_PRINT_AUDIT_DB",
            "/var/lib/qdistro/audit/print_audit.sqlite")
        if not os.path.exists(db_path):
            return []
        try:
            log = PrintAuditLog(db_path)
            rows = log.tail(n)
        except Exception as e:  # noqa: BLE001
            raise dbus.DBusException(
                f"ListPrintAudit: {e}",
                name=BUS_NAME + ".PrintAuditError",
            )
        out = []
        for r in rows:
            out.append({
                "id":            dbus.Int64(int(r.get("id") or 0)),
                "ts":            dbus.Int64(int(r.get("ts") or 0)),
                "op":            dbus.String(str(r.get("op") or "")),
                "decision":      dbus.String(str(r.get("decision") or "")),
                "reason":        dbus.String(str(r.get("reason") or "")),
                "caller_uid":    dbus.Int32(
                    int(r["caller_uid"]) if r.get("caller_uid") is not None else -1),
                "caller_pid":    dbus.Int32(
                    int(r["caller_pid"]) if r.get("caller_pid") is not None else 0),
                "caller_exe":    dbus.String(str(r.get("caller_exe") or "")),
                "backend":       dbus.String(str(r.get("backend") or "")),
                "bytes_to_be":   dbus.Int64(
                    int(r["bytes_to_be"]) if r.get("bytes_to_be") is not None else -1),
                "bytes_from_be": dbus.Int64(
                    int(r["bytes_from_be"]) if r.get("bytes_from_be") is not None else -1),
            })
        return out

    @dbus.service.method(BUS_NAME, in_signature="i", out_signature="i",
                         sender_keyword="sender", connection_keyword="conn")
    def RunAuditGc(self, retention_days: int, sender=None, conn=None) -> int:
        """Delete audit rows older than retention_days ago. Admin/root
        only. Returns deleted count. 0 is valid (nothing expired)."""
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"RunAuditGc restricted to admin/root; got uid {admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        days = int(retention_days)
        if days < 0:
            raise dbus.DBusException(
                f"retention_days must be >= 0, got {days}",
                name=BUS_NAME + ".BadArgument",
            )
        n = self.audit.gc(days * 86400)
        print(f"[broker] audit.gc on-demand: {n} rows older than {days}d "
              f"by admin_uid={admin_uid}", flush=True)
        return n

    def _audit_revoke(self, row: dict, admin_uid: int) -> None:
        """Record a revoke. Not fail-closed — if we get this far the row
        is already gone; re-inserting to preserve audit invariants would
        be worse than the observability gap. Log loudly instead."""
        try:
            self.audit.log(
                caller_uid=int(row["caller_uid"]),
                caller_pid=0,  # historical row — live pid no longer available
                caller_exe=str(row["match_value"] or ""),
                action=str(row["action"]),
                decision=False,  # revocation removes a prior allow
                scope=str(row["scope"]) if row.get("scope") else None,
                source="revoke",
                approver_uid=int(admin_uid),
            )
        except Exception as e:  # noqa: BLE001
            print(f"[broker] qdistro.audit.failure: revoke path, "
                  f"approval_id={row['id']}, reason={e!r}", flush=True)

    # spec/25 §"Notification audit" — passive recorder for org.freedesktop.
    # Notifications traffic, surfaced from each silo's qdshell via
    # Services/Qdshell/Notifications.qml. The shell stays the
    # FreeDesktop server (per-uid session bus); the broker only sees
    # what the shell chooses to forward. Per-silo identification is
    # automatic via the caller's uid — the broker doesn't trust a
    # client-supplied silo string.
    #
    # Decision is recorded as True (notification was shown). action
    # is namespaced "notification.posted" so admin's existing
    # rules-engine UI can policy this same surface in future without
    # schema churn (e.g. silence a noisy app via deny rule, with the
    # shell respecting CheckPermission verdict before showing).
    @dbus.service.method(BUS_NAME, in_signature="sssi", out_signature="",
                         sender_keyword="sender", connection_keyword="conn")
    def RecordNotification(self, app_name: str, summary: str, body: str,
                           urgency: int, sender=None, conn=None) -> None:
        uid, pid, exe, _st = self._peer_info(sender, conn)
        urgency_i = int(urgency) if urgency in (0, 1, 2) else 1
        # Truncate aggressively — audit.log is not a notification archive.
        # Long bodies bloat the SQLite store and slow History queries.
        app = (str(app_name) or "")[:128]
        summ = (str(summary) or "")[:256]
        bod = (str(body) or "")[:512]
        urgency_label = ("low", "normal", "critical")[urgency_i]
        try:
            self.audit.log(
                caller_uid=uid, caller_pid=pid, caller_exe=exe,
                action="notification.posted",
                decision=True, scope=None,
                source=(f"app={app or '(unknown)'} "
                        f"urgency={urgency_label} "
                        f"summary={summ!r} "
                        f"body={bod!r}"),
                approver_uid=None,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[broker] qdistro.audit.failure: notification, "
                  f"reason={e!r}", flush=True)

    @dbus.service.signal(BUS_NAME, signature="i")
    def RequestPending(self, request_id: int):
        pass

    @dbus.service.signal(BUS_NAME, signature="is")
    def RequestDecided(self, request_id: int, decision: str):
        pass

    @dbus.service.signal(BUS_NAME, signature="iss")
    def ApprovalRevoked(self, caller_uid: int, action: str, exe: str):
        """Emitted when a cache row is revoked (RevokeApproval or
        RevokeAllForUid). Subscribers observe revocations to tear down
        resources that were granted on the strength of the row — e.g.
        qdshell listens for `qdistro.view-stream.subscribe:<slug>`
        actions and destroys matching open streams so remote peers
        lose access immediately, rather than continuing until the
        stream is destroyed for unrelated reasons.

        `exe` is the cache row's match_value — empty string when the
        row's match_kind was 'always' (forever scope). Subscribers
        matching on action alone may ignore it; the ones that need to
        distinguish a specific binary can filter.
        """
        pass

    # ---- spec/19 §"Phase-8 MVP scope" — Snapper bridge methods ----
    #
    # Three surfaces:
    #   - SnapshotBefore(config, description) — non-admin SDK helper.
    #     Any caller can ask for a snapshot of a configured Snapper
    #     volume (rate-limited via the same RateLimiter used for
    #     RequestPermission). Returns the new snapshot number.
    #   - ListSnapshots(config) — admin only. Read-only listing.
    #   - GetFiles(config, n1, n2) — admin only. Per-file diff.
    #
    # The transport binds lazily so the broker still starts on hosts
    # without the Snapper system service installed (e.g. tests +
    # build VMs); the methods then surface a clean
    # ``SnapperUnavailable`` D-Bus error instead of a stack trace.

    def _snapper_client(self):
        cli = getattr(self, "_snapper_cached", None)
        if cli is not None:
            return cli
        try:
            from qdistro_snapshots import SnapperClient  # type: ignore
        except ImportError as e:
            raise dbus.DBusException(
                f"qdistro_snapshots module unavailable: {e}",
                name=BUS_NAME + ".SnapperUnavailable")
        try:
            sb = dbus.SystemBus()
            obj = sb.get_object("org.opensuse.Snapper",
                                "/org/opensuse/Snapper")
            iface = dbus.Interface(obj, "org.opensuse.Snapper")

            def _xport(method: str, *args):
                return iface.get_dbus_method(method)(*args)
        except Exception as e:
            raise dbus.DBusException(
                f"failed to bind org.opensuse.Snapper: {e}",
                name=BUS_NAME + ".SnapperUnavailable")
        cli = SnapperClient(_xport)
        self._snapper_cached = cli
        return cli

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="i",
                        sender_keyword="sender", connection_keyword="conn")
    def SnapshotBefore(self, config: str, description: str,
                       sender=None, conn=None) -> int:
        """SDK helper — record a single snapshot tagged with the
        caller's identity. Useful for `before <op>` paired snapshots
        in a multi-step admin task.
        """
        caller_uid, caller_pid, caller_exe, _st = self._peer_info(
            sender, conn)
        if not self.ratelimit.check(int(caller_uid), "snapshot.before"):
            raise dbus.DBusException(
                f"rate-limited: uid={caller_uid} action=snapshot.before",
                name=BUS_NAME + ".RateLimited")
        cli = self._snapper_client()
        # _snapper_client already raised SnapperUnavailable on import
        # failure; here the module is guaranteed importable.
        from qdistro_snapshots import snapshot_before  # type: ignore
        try:
            n = snapshot_before(
                cli, str(config), str(description),
                caller_uid=int(caller_uid),
                caller_exe=str(caller_exe))
        except Exception as e:
            raise dbus.DBusException(
                f"SnapshotBefore failed: {e}",
                name=BUS_NAME + ".SnapshotFailed")
        return int(n)

    @dbus.service.method(BUS_NAME, in_signature="s",
                        out_signature="aa{sv}",
                        sender_keyword="sender", connection_keyword="conn")
    def ListSnapshots(self, config: str,
                      sender=None, conn=None) -> list[dict]:
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid != ADMIN_UID:
            raise dbus.DBusException(
                f"ListSnapshots restricted to admin uid {ADMIN_UID}; "
                f"got {admin_uid}",
                name=BUS_NAME + ".AccessDenied")
        cli = self._snapper_client()
        rows = cli.list(str(config))
        out: list[dict] = []
        for r in rows:
            out.append({
                "num":         dbus.Int32(int(r["num"])),
                "type":        dbus.String(r.get("type", "")),
                "pre_num":     dbus.Int32(int(r.get("pre_num", 0))),
                "ts":          dbus.Double(float(r.get("ts", 0.0))),
                "uid":         dbus.Int32(int(r.get("uid", 0))),
                "description": dbus.String(r.get("description", "")),
                "cleanup":     dbus.String(r.get("cleanup", "")),
                "qdistro_origin": dbus.Boolean(
                    bool(r.get("qdistro_origin"))),
            })
        return out

    @dbus.service.method(BUS_NAME, in_signature="sii",
                        out_signature="aa{sv}",
                        sender_keyword="sender", connection_keyword="conn")
    def GetFiles(self, config: str, num1: int, num2: int,
                 sender=None, conn=None) -> list[dict]:
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid != ADMIN_UID:
            raise dbus.DBusException(
                f"GetFiles restricted to admin uid {ADMIN_UID}; "
                f"got {admin_uid}",
                name=BUS_NAME + ".AccessDenied")
        cli = self._snapper_client()
        rows = cli.get_files(str(config), int(num1), int(num2))
        out: list[dict] = []
        for r in rows:
            out.append({
                "path":   dbus.String(r.get("path", "")),
                "status": dbus.String(r.get("status", "")),
            })
        return out

    @dbus.service.signal(BUS_NAME, signature="i")
    def RulesReloaded(self, rule_count: int):
        """Emitted after the rules engine has been rebuilt — either from
        SaveRule or ReloadRules. Subscribers (qdshell) re-check any
        live state that depends on a recent CheckClipboardTransfer or
        CheckHandoffActivation verdict so a freshly-authored deny
        invalidates an already-active selection or pending handoff.

        The argument is the post-reload rule count, which is purely
        informational (subscribers shouldn't gate behaviour on it).
        Errors during reload are surfaced via load_errors() on the
        broker side; subscribers can call ListRules to refresh their
        view.
        """
        pass


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)
    broker = Broker(bus)
    print(f"[qdistro-admin-broker] listening on {BUS_NAME} {OBJ_PATH}", flush=True)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
