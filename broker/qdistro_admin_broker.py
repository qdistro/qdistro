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

import concurrent.futures
import json
import math
import os
import pwd as _pwd_mod
import re
import signal
import sys
import threading
import time
from typing import Any

import dbus
import dbus.mainloop.glib
import dbus.service
import qdistro_commit_lineage as _commit_lineage  # type: ignore[import-not-found]
import qdistro_disposable_classes as _dispclasses  # type: ignore[import-not-found]
import qdistro_disposables as _disp  # type: ignore[import-not-found]
import qdistro_export_lineage as _export_lineage  # type: ignore[import-not-found]
import qdistro_proc_identity as _pi  # type: ignore[import-not-found]
import qdistro_upload_lineage as _upload_lineage  # type: ignore[import-not-found]
import qdistro_upload_lineage_entry as _upload_entry  # type: ignore[import-not-found]
from gi.repository import Gio, GLib
from qdistro_admin_audit import AuditLog  # type: ignore[import-not-found]
from qdistro_admin_cache import ApprovalCache  # type: ignore[import-not-found]
from qdistro_admin_ratelimit import RateLimiter  # type: ignore[import-not-found]
from qdistro_admin_rules import RulesEngine  # type: ignore[import-not-found]
from qdistro_audisp_parser import is_qdistro_subj_type  # type: ignore[import-not-found]
from qdistro_hook_client import HookClient  # type: ignore[import-not-found]
from qdistro_launch_record import LaunchRecordStore  # type: ignore[import-not-found]
from qdistro_resolver import resolve_subject  # type: ignore[import-not-found]

BUS_NAME = "org.qdistro.AdminBroker1"
OBJ_PATH = "/org/qdistro/AdminBroker1"
# qdistro is single-tenant: the admin role is the fixed 'admin' account, which
# must be uid 1000. Resolve leniently at import (default 1000 when the account
# is absent) so this module stays importable for unit tests on hosts without
# the admin user; the invariant is enforced fail-closed at daemon startup via
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
DB_PATH = "/var/lib/qdistro/approvals/approvals.sqlite"
AUDIT_PATH = "/var/lib/qdistro/audit/audit.sqlite"
LINEAGE_DB_PATH = os.environ.get(
    "QDISTRO_EXPORT_LINEAGE_DB",
    "/var/lib/qdistro/lineage/export-lineage.sqlite",
)
LINEAGE_ISSUER = "qdistro-broker"


def _username_for_uid(uid: int) -> str:
    """Resolve a uid to its silo username for use in share-to rule
    actions. Falls back to ``uid:<n>`` when no passwd entry exists so
    the synthetic action stays well-formed and rule-addressable."""
    try:
        return _pwd_mod.getpwuid(int(uid)).pw_name
    except (KeyError, ValueError, OverflowError):
        return f"uid:{int(uid)}"


# Audit rows older than this are deleted by a daily timer (and on
# demand via RunAuditGc). 90 days is long enough for "what happened
# last quarter?" investigations without unbounded disk growth on
# workstations that live for years. Override with QDISTRO_AUDIT_RETENTION_DAYS
# for testing or stricter policies; set to 0 to disable GC entirely.
AUDIT_RETENTION_DAYS_DEFAULT = 90
AUDIT_GC_INTERVAL_S = 86400  # once per day

# Decided _Request entries are reaped this many seconds after their
# decision lands. They must outlive any late WaitForDecision caller: a
# qsu client that races the admin's click can call WaitForDecision a
# moment after the decision is recorded and still expects the real
# verdict (a reaped request replies False, silently turning an Approve
# into a deny). 300s is far longer than the prompt→wait round-trip yet
# bounds _pending growth to "decisions in the last 5 minutes" instead of
# "every decision since boot". Reaped on the existing once-a-minute
# _gc_tick. Override with QDISTRO_PENDING_RETENTION_S for testing or
# tighter memory targets; set to 0 to disable reaping entirely.
PENDING_RETENTION_S_DEFAULT = 300

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
# variants `1h`/`24h` when argv capture is absent), one approval
# becomes a wildcard for the (uid, action) pair: a `1h` approval of
# `qsu id` would implicitly approve `qsu anything-else` at root for
# the next hour.
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

_ARGV_AWARE_DELEGATED_SCOPES = frozenset((
    "forever_argv", "forever_basename", "forever_prefix",
))

_ARGV_REQUIRED_SCOPES = frozenset((
    "forever_argv", "forever_basename", "forever_prefix",
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
# An explicit "not Active" answer is the load-bearing reject path; a
# manager error (offline/timeout/parse) refuses by default
# (REQUIRE_SILO_ACTIVE, below) since the standard bootstrap always
# ships the manager. A reachable manager with no row for the uid
# falls through to the pre-P02 trust path (still admin-gated).
SESSION_MANAGER_BUS_NAME = "org.qdistro.SessionManager1"
SESSION_MANAGER_OBJ_PATH = "/org/qdistro/SessionManager1"
SESSION_MANAGER_IFACE = "org.qdistro.SessionManager1"

# When set, _silo_state errors (manager offline, timeout, parse error)
# stop falling through to the legacy trust-the-uid path. Instead they
# return the sentinel "Unreachable" which RelayMessage rejects with
# SiloManagerUnreachable.
#
# Default: fail-CLOSED (True). The standard qdistro bootstrap installs
# and enables qdistro-session-manager alongside the broker
# (scripts/install/qdistro-bootstrap.sh, fresh-vm-bootstrap.sh), so the
# silo registry is reachable by construction; an error means something
# is actually wrong and a cross-uid relay should be refused rather than
# trusted. Operators on a legacy bake that ships the broker WITHOUT the
# session manager can restore the old permissive behaviour with
# QDISTRO_BROKER_REQUIRE_SILO_ACTIVE=0 or require_silo_active = false in
# /etc/qdistro/broker.conf. (Note: this gate only fires on a manager
# *error*; when the manager is reachable but simply has no row for the
# target uid, _silo_state still returns None and the relay falls through
# to the legacy trust path — and that path still requires per-message
# admin approval.)
# Read at broker start from $QDISTRO_BROKER_REQUIRE_SILO_ACTIVE or
# /etc/qdistro/broker.conf (key = require_silo_active = true).
_REQUIRE_SILO_ACTIVE_ENV = "QDISTRO_BROKER_REQUIRE_SILO_ACTIVE"
_BROKER_CONF_PATH = "/etc/qdistro/broker.conf"


_TRUE_TOKENS = ("1", "true", "yes", "on")
_FALSE_TOKENS = ("0", "false", "no", "off")


def _read_require_silo_active() -> bool:
    # Fail-closed by default: only an explicitly recognized *false* token
    # turns the gate off. An unrecognized value (typo like "ture", "2")
    # must NOT silently fail open — it warns and keeps the closed default.
    val = os.environ.get(_REQUIRE_SILO_ACTIVE_ENV, "").strip().lower()
    if val in _TRUE_TOKENS:
        return True
    if val in _FALSE_TOKENS:
        return False
    if val:
        print(
            f"[broker] WARN {_REQUIRE_SILO_ACTIVE_ENV}={val!r} is not a "
            f"recognized boolean; defaulting require_silo_active to ON "
            f"(fail-closed)",
            flush=True)
        return True
    try:
        with open(_BROKER_CONF_PATH, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "require_silo_active":
                    cval = v.strip().lower()
                    if cval in _TRUE_TOKENS:
                        return True
                    if cval in _FALSE_TOKENS:
                        return False
                    print(
                        f"[broker] WARN {_BROKER_CONF_PATH}: "
                        f"require_silo_active={cval!r} is not a recognized "
                        f"boolean; defaulting to ON (fail-closed)",
                        flush=True)
                    return True
    except OSError:
        pass
    # No explicit setting → fail closed.
    return True


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
        with open(_BROKER_CONF_PATH, encoding="utf-8") as fh:
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


# Permission lineage (issues/qdistro/permission-lineage-findings.md):
# when True, gates resolve the live caller to an authoritative subject
# (qdistro_resolver) and use the *launcher-attested* sandbox_engine /
# app_id / silo from the broker launch record instead of the
# client-supplied secctx strings (closing finding P0-1). An unverified
# caller resolves to the `unknown` subject: its sandbox_engine / app_id /
# silo are empty, so a forged claim can only ever FAIL a non-empty rule
# selector, never satisfy one.
#
# Default OFF (shadow mode): gates behave exactly as before, but the
# resolver still runs and the broker logs/audits when the resolved
# identity would differ from the claimed one. This lets the launch-record
# registration roll out across all tiers before enforcement is switched
# on, so enabling lineage never silently breaks a legitimately-sandboxed
# app that hasn't been registered yet. Read from
# $QDISTRO_LINEAGE_ENFORCE or broker.conf key lineage_enforce.
_LINEAGE_ENFORCE_ENV = "QDISTRO_LINEAGE_ENFORCE"


def _read_lineage_enforce() -> bool:
    val = os.environ.get(_LINEAGE_ENFORCE_ENV, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    try:
        with open(_BROKER_CONF_PATH, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "lineage_enforce":
                    return v.strip().lower() in ("1", "true", "yes", "on")
    except OSError:
        pass
    return False  # default off — shadow mode until launchers register


LINEAGE_ENFORCE = _read_lineage_enforce()


# Strict-identity profile (security-hardening-carryforward.md §"Unresolved
# executable/starttime identity should deny in strict profiles"). When True
# the broker's delegated-claim verification refuses any RequestPermissionAs
# whose caller exe OR starttime could not be resolved from /proc — rather
# than accepting the claim on whichever single anchor happened to read.
# Under SELinux enforcing a denied /proc/<pid>/stat read silently zeroes the
# starttime anchor; in strict mode that becomes a hard deny instead of a
# fall-back-open. Same toggle name/semantics as qsu's QDISTRO_IDENTITY_STRICT
# so a deployment sets one flag and both the privileged-exec daemon and the
# broker fail closed in lockstep. Read from $QDISTRO_IDENTITY_STRICT or
# broker.conf key identity_strict. Default OFF (single-anchor fallback) for
# permissive bakes; tier-1/enforcing bakes flip it on.
_IDENTITY_STRICT_ENV = "QDISTRO_IDENTITY_STRICT"


def _read_identity_strict() -> bool:
    val = os.environ.get(_IDENTITY_STRICT_ENV, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    try:
        with open(_BROKER_CONF_PATH, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "identity_strict":
                    return v.strip().lower() in ("1", "true", "yes", "on")
    except OSError:
        pass
    return False


IDENTITY_STRICT = _read_identity_strict()


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
        with open(_BROKER_CONF_PATH, encoding="utf-8") as fh:
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

# Workflow orchestration engine. Loaded inside the broker process so it
# shares the GLib main loop, audit log, and lifecycle. Disabled with
# QDISTRO_WORKFLOW_ENABLED=0; any init failure degrades silently to "no
# workflows" so the core permission broker always starts.
WORKFLOW_ENABLED = os.environ.get(
    "QDISTRO_WORKFLOW_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _workflow_dir_candidates() -> list[str]:
    """Where the workflow package may live, repo vs. installed layout.

    In the source tree ``broker/`` and ``workflow/`` are siblings
    (``../workflow``). The VM installer flattens the broker into
    ``/usr/libexec/qdistro/`` and drops the workflow modules in a
    ``workflow/`` subdir beside it. Try both; first existing wins.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return [
        os.path.join(here, "workflow"),                       # installed
        os.path.normpath(os.path.join(here, "..", "workflow")),  # repo
    ]

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

    Out-of-order indices are tolerated (sorted by index) so a hostile
    caller shuffling key order can't cause a quirk. Indices beyond a
    reasonable cap (1024) are dropped to keep the reconstruction
    bounded.

    Fail-closed on a missing `argv[00]`: argv-aware approval scopes
    (forever_argv / forever_basename / forever_prefix) and the cache's
    basename/prefix matching all key off argv[0] (the program). A
    caller that supplies `argv[01]`/`argv[02]` but omits `argv[00]`
    would otherwise have its keys collapsed into a dense list whose
    element 0 is the *second* real arg — silently turning an
    argv-pinned scope into one matched against an attacker-chosen,
    program-blind tuple. We treat "no argv[00]" as "argv not captured"
    (return None) so those scopes are rejected at DecideRequest and the
    cache writes/matches no argv-aware row. Once argv[00] is present,
    interior gaps still collapse to "what was actually passed" — a rule
    expecting a specific sequence won't match a sparse one.
    """
    indexed: list[tuple[int, str]] = []
    have_zero = False
    for k, v in details.items():
        m = _ARGV_KEY_RE.match(str(k))
        if m is None:
            continue
        idx = int(m.group(1))
        if idx > 1024:
            continue
        if idx == 0 and str(v) != "":
            have_zero = True
        indexed.append((idx, str(v)))
    if not indexed or not have_zero:
        return None
    indexed.sort(key=lambda kv: kv[0])
    return [v for _, v in indexed]


def _selector_from_details(details: dict, key: str) -> str:
    value = dict(details or {}).get(key, "")
    return str(value or "")[:128]


# These three readers now delegate to the shared qdistro_proc_identity
# module (permission-lineage consolidation) but keep their broker-level
# names + signatures: tests monkeypatch B._read_proc_identity /
# _read_proc_uid / _read_proc_selinux_label, and the broker's own methods
# call the module-global names so those patches take effect.
def _read_proc_uid(pid: int) -> int | None:
    """Return the real uid of pid from /proc/<pid>/status, or None if the
    process is gone. Used by VerifyClientIdentity to cross-check the
    uid qdwin observed via SO_PEERCRED.
    """
    return _pi.read_uid(pid)


def _read_proc_selinux_label(pid: int) -> str:
    """Return the SELinux label for pid from /proc/<pid>/attr/current,
    or "" if the file is unreadable (SELinux off, process gone). Used
    by VerifyClientIdentity to re-check the qdshell-forwarded tuple
    against the live process — see todo/decisions/
    secctx-identity-contract.md.
    """
    return _pi.read_selinux_label(pid)


def _read_proc_identity(pid: int) -> tuple[str, int]:
    """Return (exe_path, starttime_ticks) for pid, or ("?", 0) if gone.

    starttime is read from /proc/<pid>/stat field 22. The stat file's
    comm field (field 2) is wrapped in parens and can contain spaces,
    so we split from the *right* of the closing paren to avoid a
    maliciously-named comm breaking the parse.
    """
    return _pi.read_exe_and_starttime(pid)


# Cap how much of an exe we hash. Most binaries are well under 64 MiB;
# anything bigger is almost certainly a self-extracting bundle whose
# trailing payload doesn't change identity assertions for the wrapping
# binary. Bounded reads keep _enqueue under a hundred ms even on the
# pathological "200 MiB monolith with one hot-path mtime tick" case.
# Kept as a broker-level alias (tests reference B._EXE_HASH_BYTES_MAX).
_EXE_HASH_BYTES_MAX = _pi.EXE_HASH_BYTES_MAX

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
    # Reads go through the shared qdistro_proc_identity readers (each
    # fail-closed to ""); the exe hash reads through the live
    # /proc/<pid>/exe link so a re-exec into a different binary between
    # request and hash is reflected rather than masked.
    out = {
        "exe_sha256": _pi.read_exe_sha256(pid),
        "selinux_label": _pi.read_selinux_label(pid),
        "cgroup": _pi.read_cgroup(pid),
    }

    with _proc_layered_lock:
        if len(_proc_layered_cache) >= _PROC_LAYERED_CACHE_MAX:
            _proc_layered_cache.clear()
        _proc_layered_cache[key] = out
    return out


def _read_proc_layered_checked(pid: int, expected_start_time: int,
                                expected_exe: str) -> dict[str, str]:
    """Thread-pool wrapper around _read_proc_layered that verifies the
    process identity hasn't changed between request time and the
    deferred IO.

    Guards against three races:
    1. PID reuse: start_time changes when a different process gets the
       same pid slot. Detected by comparing expected_start_time.
    2. exec before hash: the process calls execve() into a different
       binary before we read /proc. start_time does NOT change across
       exec; instead the exe path changes. Detected pre-hash.
    3. exec during hash: the process exec's between our pre-check and
       _read_proc_layered's actual /proc/<pid>/exe open. Mitigated by
       a post-hash revalidation — if the identity changed between the
       pre-check and the post-check, discard the results.

    When neither start_time nor exe is usable as an anchor
    (expected_start_time == 0 AND expected_exe is empty/"?"), fail
    closed: return empty strings rather than hashing an unverifiable
    process.

    Returns empty strings when verification fails or the process is
    gone, so the admin doesn't see a misleading hash.
    """
    _empty = {"exe_sha256": "", "selinux_label": "", "cgroup": ""}
    has_start_anchor = (expected_start_time != 0)
    has_exe_anchor = bool(expected_exe) and expected_exe != "?"

    # Fail closed when neither anchor is usable — we can't verify
    # the process identity at all.
    if not has_start_anchor and not has_exe_anchor:
        return _empty

    # Pre-hash identity check.
    exe_pre, st_pre = _read_proc_identity(pid)
    if st_pre == 0:
        return _empty  # process gone
    if has_start_anchor and st_pre != expected_start_time:
        return _empty  # PID reuse
    if has_exe_anchor and exe_pre != expected_exe:
        return _empty  # exec'd before we got here

    # Do the expensive IO (hash, SELinux, cgroup).
    out = _read_proc_layered(pid)

    # Post-hash revalidation: catch exec-during-hash TOCTOU.
    exe_post, st_post = _read_proc_identity(pid)
    if st_post == 0:
        return _empty  # process exited during hash
    if has_start_anchor and st_post != expected_start_time:
        return _empty  # PID recycled during hash
    if exe_post != exe_pre:
        return _empty  # exec'd during hash — discard

    return out


def _verify_delegated_claim(caller_uid: int, caller_pid: int,
                            caller_exe: str,
                            expected_start_time: int = 0) -> tuple[str, int]:
    """Verify a RequestPermissionAs caller identity claim against /proc.

    qsu captures uid/pid/exe via SO_PEERCRED immediately after accept.
    The broker is a second line of defense: before accepting the
    delegated tuple, re-read the claimed pid and reject stale claims
    when the process disappeared, changed executable, or no longer has
    the claimed uid. Returns the live (exe, start_time) for enqueueing.
    """
    pid_i = int(caller_pid)
    uid_i = int(caller_uid)
    exe_s = str(caller_exe or "")
    if pid_i <= 0:
        raise dbus.DBusException(
            f"invalid delegated caller pid {pid_i}",
            name=BUS_NAME + ".BadArgument",
        )

    live_exe, live_start = _read_proc_identity(pid_i)
    if live_start == 0:
        raise dbus.DBusException(
            f"delegated caller pid {pid_i} is not a live process",
            name=BUS_NAME + ".CallerGone",
        )
    # Strict profile: refuse the claim unless BOTH the live exe and the
    # claimed starttime anchor are resolvable. live_start==0 is already a
    # CallerGone above; here we also reject an unreadable /proc/<pid>/exe
    # (live_exe "?"/empty) and a missing claimed starttime. In a strict
    # deployment the SELinux policy is expected to grant both /proc reads,
    # so a dropped anchor is a regression or an attack — fail closed
    # rather than accept the delegated claim on a single anchor. (security-
    # hardening-carryforward: do not fall back open in strict profiles.)
    if IDENTITY_STRICT:
        missing = []
        if not live_exe or live_exe == "?":
            missing.append("live-exe")
        if not int(expected_start_time):
            missing.append("claimed-starttime")
        if missing:
            raise dbus.DBusException(
                f"strict profile: delegated caller pid {pid_i} identity "
                f"not fully resolvable (missing {'+'.join(missing)}); "
                f"refusing",
                name=BUS_NAME + ".CallerIdentityMismatch",
            )
    if expected_start_time and live_start != int(expected_start_time):
        raise dbus.DBusException(
            f"delegated caller pid {pid_i} start time mismatch",
            name=BUS_NAME + ".CallerIdentityMismatch",
        )
    if exe_s and exe_s != "?" and live_exe != exe_s:
        raise dbus.DBusException(
            f"delegated caller pid {pid_i} executable changed",
            name=BUS_NAME + ".CallerIdentityMismatch",
        )

    live_uid = _read_proc_uid(pid_i)
    if live_uid is None:
        raise dbus.DBusException(
            f"delegated caller pid {pid_i} uid could not be verified",
            name=BUS_NAME + ".CallerGone",
        )
    if int(live_uid) != uid_i:
        raise dbus.DBusException(
            f"delegated caller pid {pid_i} uid mismatch",
            name=BUS_NAME + ".CallerIdentityMismatch",
        )

    return live_exe, live_start


class _Request:
    __slots__ = (
        "id", "uid", "pid", "exe", "start_time", "action", "details",
        "decision", "waiters", "delegated", "one_shot",
        "exe_sha256", "selinux_label", "cgroup", "layered_pending",
        "decided_at",
    )

    def __init__(self, rid: int, uid: int, pid: int, exe: str,
                 start_time: int, action: str, details: dict,
                 delegated: bool = False, one_shot: bool = False,
                 exe_sha256: str = "", selinux_label: str = "",
                 cgroup: str = "", layered_pending: bool = False):
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
        # When layered_pending is True, the IO thread hasn't delivered
        # these fields yet; they will be empty until the idle callback
        # applies the results.
        self.exe_sha256 = str(exe_sha256 or "")
        self.selinux_label = str(selinux_label or "")
        self.cgroup = str(cgroup or "")
        self.layered_pending = bool(layered_pending)
        # Monotonic-ish wall-clock (time.time()) when this request was
        # decided, used by the broker's periodic reaper to drop decided
        # entries after a retention window. None while undecided. The
        # reaper stamps this lazily the first time it sees a decided
        # request with decided_at still None, so every decision site
        # (rule/cache/hook fast path, admin prompt, TOCTOU force-deny,
        # audit-failure downgrade) gets reaped without each one having
        # to remember to set it.
        self.decided_at: float | None = None


class Broker(dbus.service.Object):
    # Debounce window (ms) for coalescing per-uid UserRelay
    # LocalReceiversChanged signals into a single ReceiversChanged. A
    # class attribute so tests can zero it out and exercise the
    # coalesce/emit path without a live GLib mainloop. 250ms mirrors the
    # relay's own debounce: long enough to swallow a silo's startup
    # burst, short enough to feel instant in the launcher.
    RECEIVERS_CHANGED_DEBOUNCE_MS = 250

    def __init__(self, bus):
        super().__init__(bus, OBJ_PATH)
        self._lock = threading.Lock()
        self._next_id = 1
        # GLib timeout id of a pending (debounced) ReceiversChanged emit,
        # 0 when none is armed.
        self._receivers_changed_timer = 0
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
        # Thread pool for deferred IO (layered-identity reads). The
        # _enqueue fast path returns the rid immediately; the slow
        # _read_proc_layered IO runs here and delivers results via
        # GLib.idle_add back to the mainloop thread. 4 workers handle
        # the common burst of concurrent qsu clients without starving
        # the system.
        self._io_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="broker-io")
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
        print(f"[broker] require_silo_active={REQUIRE_SILO_ACTIVE} "
              f"({'fail-closed' if REQUIRE_SILO_ACTIVE else 'fail-open/legacy'})",
              flush=True)
        # Permission-lineage launch-record store (Phase 1). Trusted
        # launchers register via RegisterLaunch; gates resolve live pids
        # against it (Phase 2/3). Reaped once a minute alongside the
        # cache GC.
        self.launch_records = LaunchRecordStore()
        self._lineage_store: Any | None = None
        print(f"[broker] lineage_enforce={LINEAGE_ENFORCE} "
              f"(False=shadow/audit-only)", flush=True)
        # Retention knob: env override wins for tests; 0 disables GC.
        try:
            self._audit_retention_days = int(
                os.environ.get("QDISTRO_AUDIT_RETENTION_DAYS",
                               AUDIT_RETENTION_DAYS_DEFAULT))
        except ValueError:
            self._audit_retention_days = AUDIT_RETENTION_DAYS_DEFAULT
        # Retention window (seconds) for decided _pending entries; env
        # override wins for tests, <=0 disables reaping. A non-finite
        # value (inf/nan) would silently neuter the reaper — every
        # `now - decided_at >= retention` comparison is False against
        # nan/inf — re-introducing the leak this fix exists to close, so
        # reject it and fall back to the default rather than fail open.
        try:
            retention = float(
                os.environ.get("QDISTRO_PENDING_RETENTION_S",
                               PENDING_RETENTION_S_DEFAULT))
            if not math.isfinite(retention):
                raise ValueError("non-finite retention")
            self._pending_retention_s = retention
        except ValueError:
            self._pending_retention_s = float(PENDING_RETENTION_S_DEFAULT)
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

        # Observe every per-uid UserRelay's LocalReceiversChanged on the
        # system bus. All relays share the same iface + object path, so a
        # single sender-agnostic match catches them all; the handler
        # debounces and re-emits the payload-free ReceiversChanged that
        # qdshell's launcher subscribes to. Per-uid session-bus
        # NameOwnerChanged never reaches the system bus, hence this relay
        # → broker → qdshell signal chain.
        try:
            bus.add_signal_receiver(
                self._on_relay_receivers_changed,
                signal_name="LocalReceiversChanged",
                dbus_interface=USER_RELAY_IFACE,
                path=USER_RELAY_OBJ_PATH)
        except Exception as e:  # noqa: BLE001
            # A bus without signal support (the test stub bypasses
            # __init__ entirely) or an old dbus-python: degrade to the
            # safety-net poll on the qdshell side rather than failing to
            # start the broker.
            print(f"[broker] LocalReceiversChanged subscription skipped: "
                  f"{e!r}", flush=True)

        # Workflow orchestration engine — shares this process + main loop.
        self.workflow_engine = None
        self._setup_workflow_engine()

    def _setup_workflow_engine(self) -> None:
        """Load the workflow engine inside the broker, sharing the GLib
        main loop. Best-effort: any failure leaves workflow_engine None
        and the core broker unaffected."""
        if not WORKFLOW_ENABLED:
            return
        try:
            wf_dir = next((d for d in _workflow_dir_candidates()
                           if os.path.isdir(d)), None)
            if wf_dir is None:
                print("[broker] workflow engine: package dir not found; "
                      "skipping", flush=True)
                return
            if wf_dir not in sys.path:
                sys.path.insert(0, wf_dir)
            from audit_logger import WorkflowAuditLogger
            from pwd_secret_source import PwdSecretSource
            from workflow_engine import WorkflowEngine
            wf_audit = WorkflowAuditLogger(broker_audit=self.audit)
            # Zero-coordination git-signing relay (OPT-IN, off by default).
            # When QDISTRO_SIGN_AGENT_RELAY names a fixed per-user agent
            # socket path, stand up an ssh-agent relay there and inject its
            # registrar so a plain `git -S` (no qsu, no run_id) blocked on
            # that path starts relaying the moment a run publishes its per-run
            # agent. The dev points SSH_AUTH_SOCK / ~/.ssh IdentityAgent at
            # this same path once, ahead of time. See
            # workflow/examples/git-sign-zero-coord.yaml. Best-effort: a relay
            # bind failure must not take down the engine.
            channel_registrar = None
            self._sign_agent_relay = None
            relay_path = os.environ.get("QDISTRO_SIGN_AGENT_RELAY", "").strip()
            if relay_path:
                try:
                    from agent_relay import build_relay_registrar
                    relay, channel_registrar = build_relay_registrar(relay_path)
                    self._sign_agent_relay = relay
                    print(f"[broker] ssh-agent sign relay listening on "
                          f"{relay_path}", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[broker] ssh-agent sign relay init skipped: {e!r}",
                          flush=True)
                    channel_registrar = None
            engine = WorkflowEngine(
                broker_proxy=self,
                audit_logger=wf_audit,
                secret_source=PwdSecretSource(),
                # The broker owns the process main loop; D-Bus triggers
                # must reuse it instead of starting their own.
                own_dbus_loop=False,
                channel_registrar=channel_registrar,
            )
            for err in engine.load_workflows():
                print(f"[broker] workflow load error: {err}", flush=True)
            engine.register_triggers()
            self.workflow_engine = engine
            print(f"[broker] workflow engine: "
                  f"{len(engine.list_workflow_defs())} workflow(s) loaded",
                  flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[broker] workflow engine init skipped: {e!r}", flush=True)
            self.workflow_engine = None

    def _reload_workflows(self) -> None:
        """Reload workflow definitions + re-register triggers. Guarded so
        it's a no-op when the engine isn't running (e.g. in tests)."""
        engine = getattr(self, "workflow_engine", None)
        if engine is None:
            return
        try:
            for err in engine.load_workflows():
                print(f"[broker] workflow load error: {err}", flush=True)
            engine.register_triggers()
        except Exception as e:  # noqa: BLE001
            print(f"[broker] workflow reload failed: {e!r}", flush=True)

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
        # A reload trigger (SIGHUP / ReloadRules / inotify) also refreshes
        # workflow definitions so admins get one reload convention. No-op
        # when the workflow engine isn't running.
        self._reload_workflows()
        return n

    def _gc_tick(self) -> bool:
        try:
            self.cache.gc()
        except Exception as e:  # noqa: BLE001
            print(f"[broker] cache.gc failed: {e}", flush=True)
        try:
            store = getattr(self, "launch_records", None)
            if store is not None:
                store.reap_expired()
        except Exception as e:  # noqa: BLE001
            print(f"[broker] launch_records.reap failed: {e}", flush=True)
        try:
            self._reap_pending()
        except Exception as e:  # noqa: BLE001
            print(f"[broker] _pending reap failed: {e}", flush=True)
        return True  # keep firing

    def _reap_pending(self, now: float | None = None) -> int:
        """Drop decided _pending entries older than the retention window.

        Runs on the GLib mainloop thread (via _gc_tick), which is the
        only thread that mutates _pending, but takes the lock anyway to
        keep the single-writer invariant explicit and consistent with
        the rest of the broker.

        Decided requests are stamped lazily: the first reap pass that
        sees a decided request with decided_at == None records the
        current time, and a later pass reaps it once retention has
        elapsed. This keeps the leak fix off the hot decision paths —
        no decision site has to remember to set decided_at — at the cost
        of at most one extra _gc_tick interval of retention, which is
        immaterial against a 5-minute window.

        Undecided requests are never reaped: they may still have waiters
        blocked on a verdict, and the admin approvals UI lists them.
        Returns the number of entries removed (for tests/logging)."""
        retention = getattr(self, "_pending_retention_s", 0.0)
        if not retention or retention <= 0:
            return 0  # reaping disabled
        if now is None:
            now = time.time()
        removed = 0
        with self._lock:
            for rid, req in list(self._pending.items()):
                if req.decision is None:
                    continue  # still awaiting a verdict; keep it
                if req.decided_at is None:
                    # First sighting after the decision landed: stamp
                    # now, reap on a subsequent pass once retention
                    # elapses.
                    req.decided_at = now
                    continue
                if now - req.decided_at >= retention:
                    del self._pending[rid]
                    removed += 1
        if removed:
            print(f"[broker] _pending reap removed {removed} decided "
                  f"request(s) older than {retention:g}s", flush=True)
        return removed

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

    def _get_lineage_store(self):
        """Lazily open the broker-owned export lineage store."""
        if self._lineage_store is None:
            from qdistro_lineage_store import LineageStore

            parent = os.path.dirname(LINEAGE_DB_PATH)
            if parent:
                created = not os.path.exists(parent)
                os.makedirs(parent, exist_ok=True)
                if created:
                    try:
                        os.chmod(parent, 0o700)
                    except OSError as e:
                        print(f"[broker] lineage dir chmod failed: {e}", flush=True)
            self._lineage_store = LineageStore(LINEAGE_DB_PATH)
        return self._lineage_store

    def _require_root_lineage_peer(self, sender, conn, method: str) -> tuple[int, int, str, int]:
        uid, pid, exe, st = self._peer_info(sender, conn)
        if uid != 0:
            raise dbus.DBusException(
                f"{method} restricted to root callers; got uid {uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        return uid, pid, exe, st

    def _check_export_lineage_policy(
        self,
        desc: _export_lineage.ExportLineageDescriptor,
        *,
        caller_exe: str,
    ) -> None:
        try:
            cls = _dispclasses.resolve_from_registry(desc.open_class)
        except _dispclasses.RegistryError as e:
            raise dbus.DBusException(
                f"disposable-class registry unreadable: {e}",
                name=BUS_NAME + ".LineagePolicyDenied",
            ) from e
        except (_dispclasses.UnknownClass, _dispclasses.ClassDisabled) as e:
            raise dbus.DBusException(
                f"open class {desc.open_class!r} is not importable: {e}",
                name=BUS_NAME + ".LineagePolicyDenied",
            ) from e
        if not cls.export or (desc.mode == "edit" and not cls.edit):
            raise dbus.DBusException(
                f"open class {desc.open_class!r} does not permit {desc.mode}",
                name=BUS_NAME + ".LineagePolicyDenied",
            )
        try:
            gate = _dispclasses.export_action(desc.open_class)
        except _dispclasses.RegistryError as e:
            raise dbus.DBusException(
                f"invalid open class {desc.open_class!r}: {e}",
                name=BUS_NAME + ".LineagePolicyDenied",
            ) from e
        rule = self.rules.match(uid=0, action=gate, exe=caller_exe)
        if rule is None or rule.decision != "allow":
            verdict = "unknown" if rule is None else rule.decision
            raise dbus.DBusException(
                f"broker did not allow {gate} (verdict={verdict})",
                name=BUS_NAME + ".LineagePolicyDenied",
            )

    @staticmethod
    def _normalize_commit_lineage_descriptor(payload: str) -> dict:
        """Strictly re-validate a commit-lineage descriptor broker-side.

        Defense-in-depth on top of ``record_commit``'s own checks: the broker
        never trusts the caller's shape. Returns a kwargs dict for
        ``_commit_lineage.record_commit``. The descriptor may carry ONLY a
        source ``{eid}`` (never caller-supplied guards/compartments/conflict
        classes — the authoritative security snapshot is read from the store by
        the handler). Raises :class:`dbus.DBusException` ``.BadArgument`` on any
        malformed shape.
        """
        def bad(msg: str) -> dbus.DBusException:
            return dbus.DBusException(msg, name=BUS_NAME + ".BadArgument")

        if not isinstance(payload, str) or not payload:
            raise bad("descriptor must be a non-empty JSON string")
        try:
            raw = json.loads(payload)
        except (TypeError, ValueError) as e:
            raise bad(f"descriptor is not valid JSON: {e}") from e
        if not isinstance(raw, dict):
            raise bad("descriptor must be a JSON object")
        allowed = {
            "version", "message", "commit_eid", "sources", "tree_digest",
            "branch", "dest_compartments", "dest_conflict_classes", "agent_gid",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise bad(f"descriptor has unknown keys: {sorted(unknown)}")
        if raw.get("version") != 1:
            raise bad(f"unsupported descriptor version {raw.get('version')!r}")
        message = raw.get("message")
        if not isinstance(message, str):
            raise bad("message must be a string")

        commit_eid = raw.get("commit_eid")
        if commit_eid is None:
            commit_eid = _commit_lineage.new_commit_eid()
        elif not isinstance(commit_eid, str) or not commit_eid:
            raise bad("commit_eid must be a non-empty string or null")

        sources_raw = raw.get("sources")
        if not isinstance(sources_raw, list) or not sources_raw:
            raise bad("sources must be a non-empty list")
        sources = []
        for s in sources_raw:
            if not isinstance(s, dict) or set(s) != {"eid"}:
                raise bad("each source must be an object {'eid': str} only")
            eid = s.get("eid")
            if not isinstance(eid, str) or not eid:
                raise bad("source eid must be a non-empty string")
            sources.append(_commit_lineage.CommitSource(eid=eid))

        tree_digest = raw.get("tree_digest")
        if tree_digest is not None and not (
            isinstance(tree_digest, str) and _commit_lineage.lr.is_hex_digest(tree_digest)
        ):
            raise bad("tree_digest must be a sha256 hex string or null")

        branch = raw.get("branch")
        if branch is not None and (not isinstance(branch, str) or not branch):
            raise bad("branch must be a non-empty string or null")
        agent_gid = raw.get("agent_gid")
        if agent_gid is not None and (not isinstance(agent_gid, str) or not agent_gid):
            raise bad("agent_gid must be a non-empty string or null")

        def _str_list(key: str) -> list[str]:
            val = raw.get(key, [])
            if not isinstance(val, list) or not all(
                isinstance(x, str) and x for x in val
            ):
                raise bad(f"{key} must be a list of non-empty strings")
            return list(val)

        return {
            "message": message,
            "commit_eid": commit_eid,
            "sources": sources,
            "tree_digest": tree_digest,
            "branch": branch,
            "dest_compartments": _str_list("dest_compartments"),
            "dest_conflict_classes": _str_list("dest_conflict_classes"),
            "agent_gid": agent_gid,
        }

    # ---- permission lineage (findings.md Phases 2/3) -------------------
    def _resolve_subject(self, pid: int):
        """Resolve a live pid to an authoritative Subject against the
        launch-record store. Always returns a Subject (never raises);
        an unverified caller resolves to the `unknown` subject."""
        store = getattr(self, "launch_records", None)
        return resolve_subject(pid, store)

    def _lineage_selectors(self, pid: int, claimed_engine: str,
                           claimed_app_id: str, action_s: str,
                           uid: int, exe: str) -> tuple[str, str]:
        """Decide which (sandbox_engine, app_id) the rules engine sees.

        Closes finding P0-1: the claimed secctx strings are caller-
        controlled, so they must never be trusted on their own. Resolve
        the live caller and:

        - LINEAGE_ENFORCE on  → return the *launcher-attested* values from
          the verified launch record; for an unverified caller return
          ("", "") so a forged claim can only fail a non-empty selector.
        - LINEAGE_ENFORCE off → return the claimed values unchanged
          (legacy behaviour) but, when the resolved identity disagrees
          with the claim, emit one audit/log line so the lineage gap is
          observable before enforcement is switched on (shadow mode).
        """
        subj = self._resolve_subject(pid)
        resolved_engine = subj.sandbox_engine if subj.verified else ""
        resolved_app = subj.app_id if subj.verified else ""
        claimed_engine = str(claimed_engine or "")
        claimed_app_id = str(claimed_app_id or "")
        mismatch = (not subj.verified and (claimed_engine or claimed_app_id)) \
            or (subj.verified and (resolved_engine != claimed_engine
                                   or resolved_app != claimed_app_id))
        if mismatch:
            mode = "ENFORCE" if LINEAGE_ENFORCE else "shadow"
            # Audit the mismatch only in enforce mode — that is when the
            # broker actually changed the decision inputs and an admin
            # needs the row. Shadow mode is print-only so it can be rolled
            # out without perturbing audit-history expectations.
            if LINEAGE_ENFORCE:
                try:
                    self.audit.log(
                        caller_uid=uid, caller_pid=pid, caller_exe=exe,
                        action=f"qdistro.lineage.mismatch:{action_s}",
                        decision=False, scope=None,
                        source=(f"lineage_{mode} verified={subj.verified} "
                                f"claimed_engine={claimed_engine!r} "
                                f"resolved_engine={resolved_engine!r} "
                                f"claimed_app={claimed_app_id!r} "
                                f"resolved_app={resolved_app!r} "
                                f"reason={subj.reason}"),
                        approver_uid=None,
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"[broker] qdistro.audit.failure: lineage_mismatch, "
                          f"reason={e!r}", flush=True)
            print(f"[broker] lineage {mode}: caller pid={pid} uid={uid} "
                  f"claimed engine={claimed_engine!r}/app={claimed_app_id!r} "
                  f"resolved engine={resolved_engine!r}/app={resolved_app!r} "
                  f"({subj.reason})", flush=True)
        if LINEAGE_ENFORCE:
            return resolved_engine, resolved_app
        return claimed_engine, claimed_app_id

    def _cache_sandboxed(self, pid: int) -> bool:
        """True iff ``pid`` resolves to a VERIFIED sandboxed launch record.

        Security guard for the approval cache (issue
        broker-forever-cache-scope): an authenticated sandboxed (tier-2)
        caller must not inherit a uid-wide argv-blind ``forever`` /
        ``forever_exe`` grant minted for a different exe/argv/tier. The
        cache lookup skips the argv-blind kinds when this is True.

        Crucially this is anchored on the *verified* launch-record
        ``sandbox_engine`` — NOT the claimed selector returned by
        ``_lineage_selectors`` (which, in lineage *shadow* mode, is the
        forgeable client-supplied string). Using the claimed value would
        let a sandboxed caller claim an empty engine and keep hitting the
        blind row, defeating the guard in the default shadow posture. An
        unverified subject yields False — a forged claim can never make
        the guard MORE permissive, only less.
        """
        try:
            subj = self._resolve_subject(pid)
        except Exception as e:  # noqa: BLE001
            print(f"[broker] _cache_sandboxed resolve failed pid={pid}: {e!r}",
                  flush=True)
            return False
        return bool(subj.verified and subj.sandbox_engine)

    def _cross_silo_source(self, *, source_pid: int, source_starttime: int,
                           claimed_src: str, claimed_app: str,
                           claimed_engine: str, gate: str,
                           uid: int, caller_pid: int, caller_exe: str):
        """Resolve the SOURCE subject of a cross-silo gate to the
        launcher-attested (silo, app_id, sandbox_engine) — finding P1-1.

        On the clipboard/handoff gates the D-Bus caller is qdshell, not the
        source app, so resolving the *caller* pid (as _lineage_selectors
        does for CheckPermission) would attest qdshell, not the app whose
        data is moving. Instead qdshell relays the source app's
        kernel-authenticated ``(pid, starttime)`` — the tuple qdwin captured
        via SO_PEERCRED at secctx-bind (qdwin.c:13551) and already feeds to
        VerifyClientIdentity (ClipboardGate.qml). We resolve *that* pid
        against the launch-record store, anchoring on the relayed starttime
        so a recycled PID cannot inherit an old app's silo.

        Returns ``(src, app_id, sandbox_engine, hard_deny)``:

        - LINEAGE_ENFORCE off (shadow): the claimed values are returned
          unchanged (legacy behaviour) and ``hard_deny`` is always False; a
          divergence between the claim and the resolved subject is logged so
          the gap is observable before enforcement is switched on.
        - LINEAGE_ENFORCE on:
            * a source pid is relayed AND resolves to a *verified* subject
              whose live starttime matches the relayed one → the
              launcher-attested ``(silo, app_id, sandbox_engine)`` is
              returned; ``hard_deny`` False.
            * a source pid is relayed but the subject is unverified (no
              launch record, axis mismatch, starttime drift, or the process
              is gone), OR no source pid is relayed at all → ``hard_deny``
              True. A cross-silo decision must rest on an attested source;
              an unattested or forged source can only ever be denied, never
              satisfy a rule. This is the failure posture from
              ``permission-lineage-findings.md`` applied to the source axis.
        """
        claimed_src = str(claimed_src or "")
        claimed_app = str(claimed_app or "")
        claimed_engine = str(claimed_engine or "")
        try:
            spid = int(source_pid)
        except (TypeError, ValueError):
            spid = 0
        try:
            sstart = int(source_starttime)
        except (TypeError, ValueError):
            sstart = 0

        if not LINEAGE_ENFORCE:
            # Shadow: never change the decision inputs, but surface a
            # divergence so the lineage gap is observable pre-enforce.
            if spid > 0:
                subj = self._resolve_subject(spid)
                live_exe, live_start = _read_proc_identity(spid)
                drift = (sstart and live_start and int(live_start) != sstart)
                if drift or not subj.verified or subj.silo != claimed_src:
                    print(f"[broker] lineage shadow ({gate}): source "
                          f"pid={spid} claimed silo={claimed_src!r}/"
                          f"app={claimed_app!r}/engine={claimed_engine!r} "
                          f"resolved silo={subj.silo!r}/app={subj.app_id!r}/"
                          f"engine={subj.sandbox_engine!r} "
                          f"verified={subj.verified} starttime_drift={bool(drift)} "
                          f"({subj.reason})", flush=True)
            return claimed_src, claimed_app, claimed_engine, False

        # Enforce. A cross-silo decision requires an attested source.
        if spid <= 0:
            self._audit_cross_silo_deny(
                gate, uid, caller_pid, caller_exe,
                reason="no-source-pid-relayed", claimed_src=claimed_src)
            return claimed_src, "", "", True

        # Anchor on the relayed starttime: if the live process no longer has
        # the starttime qdwin observed, the PID was recycled — fail closed
        # before even consulting the record.
        _live_exe, live_start = _read_proc_identity(spid)
        if sstart and (not live_start or int(live_start) != sstart):
            self._audit_cross_silo_deny(
                gate, uid, caller_pid, caller_exe,
                reason=(f"source-starttime-drift live={live_start} "
                        f"relayed={sstart}"),
                claimed_src=claimed_src)
            return claimed_src, "", "", True

        subj = self._resolve_subject(spid)
        if not subj.verified:
            self._audit_cross_silo_deny(
                gate, uid, caller_pid, caller_exe,
                reason=f"source-unverified ({subj.reason})",
                claimed_src=claimed_src)
            return claimed_src, "", "", True

        # Verified source: use the launcher-attested identity, never the
        # qdshell-relayed claim. Log when they diverge (a forged claim that
        # enforcement just overrode).
        if subj.silo != claimed_src or subj.app_id != claimed_app \
                or subj.sandbox_engine != claimed_engine:
            print(f"[broker] lineage ENFORCE ({gate}): source pid={spid} "
                  f"claim silo={claimed_src!r}/app={claimed_app!r}/"
                  f"engine={claimed_engine!r} overridden with attested "
                  f"silo={subj.silo!r}/app={subj.app_id!r}/"
                  f"engine={subj.sandbox_engine!r}", flush=True)
        return subj.silo, subj.app_id, subj.sandbox_engine, False

    def _audit_cross_silo_deny(self, gate: str, uid: int, caller_pid: int,
                               caller_exe: str, *, reason: str,
                               claimed_src: str) -> None:
        """Audit a cross-silo gate denied for want of an attested source."""
        try:
            self.audit.log(
                caller_uid=uid, caller_pid=caller_pid, caller_exe=caller_exe,
                action=f"qdistro.lineage.source_deny:{gate}:{claimed_src}",
                decision=False, scope=None,
                source=f"cross_silo_source_unattested reason={reason}",
                approver_uid=None,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[broker] qdistro.audit.failure: cross_silo_source_deny, "
                  f"reason={e!r}", flush=True)
        print(f"[broker] lineage ENFORCE ({gate}): cross-silo DENY — "
              f"{reason}", flush=True)

    def _journal_cross_silo_decision(self, *, gate: str, src: str, dst: str,
                                     decision: str, src_app: str,
                                     src_engine: str) -> None:
        """Emit one concise journal line per cross-silo clipboard/handoff
        decision.

        The full audit row lands in the sqlite audit DB (AuditLog.log), which
        is the system of record for history/forensics. But that DB is not
        readable from the journal, so an operator tailing `journalctl -u
        qdistro-admin-broker` (and the VM integration suite that observes the
        live broker that way) had no line tying a cross-silo verdict to the
        *source window identity* it was decided against. This prints that
        attribution — the same ``src_app=/src_engine=`` shape the audit row
        carries — so the decision is observable without opening the DB.

        stdout only; never raises (best-effort observability).
        """
        try:
            print(
                f"[broker] clipboard/{gate} cross-silo decision: "
                f"{src} -> {dst} verdict={decision} "
                f"src_app={src_app or '(unknown)'} "
                f"src_engine={src_engine or '(unknown)'}",
                flush=True,
            )
        except Exception:  # noqa: BLE001
            pass

    @dbus.service.method(BUS_NAME,
                         in_signature="ssssstst", out_signature="s",
                         sender_keyword="sender", connection_keyword="conn")
    def RegisterLaunch(self, silo: str, sandbox_engine: str, app_id: str,
                       instance_id: str, exe: str, target_pid: int,
                       namespace: str, target_starttime: int,
                       sender=None, conn=None) -> str:
        """Trusted launcher registers a process it is about to expose as a
        silo workload (permission-lineage Phase 1). Returns the opaque
        record id.

        Restricted to **root** launchers (D-Bus policy + the in-method
        uid-0 check below), exactly like RequestPermissionAs: only a
        more-privileged component may attest a child's intended silo. The
        broker re-reads the registered pid from /proc and verifies the
        (starttime, uid, exe) the launcher supplied still names that live
        process before storing — a launcher cannot register a record for a
        process that already changed identity. The stored record binds
        (pid, starttime) → (silo, sandbox_engine, app_id, uid, exe, label,
        cgroup); a later gate resolves a live pid to it and revalidates
        the kernel facts (qdistro_resolver).

        target_starttime==0 means "trust /proc"; otherwise it is checked
        against the live value (anti-PID-reuse at registration time).
        """
        launcher_uid, launcher_pid, launcher_exe, _ = self._peer_info(
            sender, conn)
        if launcher_uid != 0:
            raise dbus.DBusException(
                f"RegisterLaunch restricted to root launchers; "
                f"got uid {launcher_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        try:
            pid_i = int(target_pid)
            expected_start = int(target_starttime)
        except (TypeError, ValueError) as e:
            raise dbus.DBusException(
                "target_pid and target_starttime must be integers",
                name=BUS_NAME + ".BadArgument",
            ) from e
        if pid_i <= 0:
            raise dbus.DBusException(
                f"invalid target_pid {pid_i}",
                name=BUS_NAME + ".BadArgument",
            )
        # Re-read the live process and verify it still matches the
        # launcher's claim. starttime is the anti-PID-reuse anchor.
        live_exe, live_start = _read_proc_identity(pid_i)
        if live_start == 0:
            raise dbus.DBusException(
                f"target pid {pid_i} is not a live process",
                name=BUS_NAME + ".CallerGone",
            )
        if expected_start and live_start != expected_start:
            raise dbus.DBusException(
                f"target pid {pid_i} starttime mismatch "
                f"(live={live_start} claimed={expected_start})",
                name=BUS_NAME + ".CallerIdentityMismatch",
            )
        exe_s = str(exe or "")
        # The broker runs as root, so a live process's exe is normally
        # readable. Refuse to mint a record we can't anchor on a real exe
        # (the record's exe is a verification axis the resolver enforces) —
        # fail closed rather than storing the launcher's unverified claim.
        if not live_exe or live_exe == "?":
            raise dbus.DBusException(
                f"target pid {pid_i} exe unreadable; refusing to register "
                f"an unverifiable launch record",
                name=BUS_NAME + ".CallerIdentityMismatch",
            )
        if exe_s and exe_s != "?" and live_exe != exe_s:
            raise dbus.DBusException(
                f"target pid {pid_i} exe mismatch "
                f"(live={live_exe!r} claimed={exe_s!r})",
                name=BUS_NAME + ".CallerIdentityMismatch",
            )
        live_uid = _read_proc_uid(pid_i)
        if live_uid is None:
            raise dbus.DBusException(
                f"target pid {pid_i} uid could not be verified",
                name=BUS_NAME + ".CallerGone",
            )
        live_label = _read_proc_selinux_label(pid_i)
        live_cgroup = _pi.read_cgroup(pid_i)
        rec = self.launch_records.register(
            silo=str(silo or "")[:80],
            uid=int(live_uid),
            pid=pid_i,
            starttime=int(live_start),
            exe=(live_exe if live_exe and live_exe != "?" else exe_s)[:4096],
            selinux_label=live_label[:512],
            cgroup=live_cgroup[:4096],
            sandbox_engine=str(sandbox_engine or "")[:128],
            app_id=str(app_id or "")[:128],
            instance_id=str(instance_id or "")[:128],
            namespace=str(namespace or "")[:128],
        )
        try:
            self.audit.log(
                caller_uid=launcher_uid, caller_pid=launcher_pid,
                caller_exe=launcher_exe,
                action=f"qdistro.lineage.register:{rec.silo}",
                decision=True, scope=None,
                source=(f"register_launch record={rec.record_id} "
                        f"pid={pid_i} starttime={live_start} "
                        f"uid={live_uid} engine={rec.sandbox_engine!r} "
                        f"app={rec.app_id!r} label={live_label!r}"),
                approver_uid=None,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[broker] qdistro.audit.failure: register_launch, "
                  f"reason={e!r}", flush=True)
        return rec.record_id

    @dbus.service.method(
        BUS_NAME,
        in_signature="",
        out_signature="s",
        sender_keyword="sender",
        connection_keyword="conn",
    )
    def GetLineageReceiptContext(self, sender=None, conn=None) -> str:
        """Return the broker-owned receipt surface context as JSON."""
        self._require_root_lineage_peer(sender, conn, "GetLineageReceiptContext")
        try:
            store = self._get_lineage_store()
        except Exception as e:  # noqa: BLE001
            raise dbus.DBusException(
                f"lineage store unavailable: {e}",
                name=BUS_NAME + ".LineageUnavailable",
            ) from e
        return json.dumps(
            {
                "version": 1,
                "chain_head": store.chain_head(),
                "issuer": LINEAGE_ISSUER,
            },
            sort_keys=True,
        )

    @dbus.service.method(
        BUS_NAME,
        in_signature="s",
        out_signature="s",
        sender_keyword="sender",
        connection_keyword="conn",
    )
    def RecordExportLineage(self, descriptor_json: str, sender=None, conn=None) -> str:
        """Validate landed export artifacts and record broker-owned lineage."""
        _uid, _pid, caller_exe, _st = self._require_root_lineage_peer(
            sender, conn, "RecordExportLineage")
        try:
            desc = _export_lineage.load_descriptor_json(descriptor_json)
            if not _disp.is_disposable_token(desc.launch_token):
                raise _export_lineage.BadDescriptor("malformed launch_token")
            self._check_export_lineage_policy(desc, caller_exe=caller_exe)
            _export_lineage.validate_landed_files(desc)
            store = self._get_lineage_store()
            result = _export_lineage.record_export_activity(store, desc)
        except _export_lineage.BadDescriptor as e:
            raise dbus.DBusException(str(e), name=BUS_NAME + ".BadArgument") from e
        except _export_lineage.ValidationFailed as e:
            raise dbus.DBusException(
                str(e), name=BUS_NAME + ".LineageValidationFailed"
            ) from e
        except dbus.DBusException:
            raise
        except Exception as e:  # noqa: BLE001
            raise dbus.DBusException(
                f"lineage store unavailable: {e}",
                name=BUS_NAME + ".LineageUnavailable",
            ) from e
        return json.dumps(
            {
                "version": 1,
                "lineage_sealed": True,
                "activity": result.activity,
                "source": result.source,
                "outputs": list(result.outputs),
                "chain_head": result.chain_head,
            },
            sort_keys=True,
        )

    @dbus.service.method(
        BUS_NAME,
        in_signature="s",
        out_signature="s",
        sender_keyword="sender",
        connection_keyword="conn",
    )
    def RecordCommitLineage(self, descriptor_json: str, sender=None, conn=None) -> str:
        """Record a git-commit chokepoint and return the commit message with the
        ``Qdistro-Lineage`` trailer appended (BEFORE the caller signs).

        Mirrors :meth:`RecordExportLineage`: root-only, the broker strictly
        re-validates the descriptor shape and reads the authoritative source
        security snapshot from the store (never from the caller), then invokes
        :func:`qdistro_commit_lineage.record_commit`. A guard-denied commit
        returns NO trailer (``.LineagePolicyDenied`` — the caller MUST abort the
        commit rather than sign an unguarded message); an unrecorded source is a
        ``.BadArgument`` (fail-closed laundering guard). The returned ``message``
        is the bytes the caller signs — the trailer is part of those bytes."""
        # Root-gate BEFORE any parsing or work, so a non-root caller can never
        # obtain a trailer-appended (and thus signable) commit message here.
        self._require_root_lineage_peer(sender, conn, "RecordCommitLineage")
        kwargs = self._normalize_commit_lineage_descriptor(descriptor_json)
        try:
            store = self._get_lineage_store()
        except Exception as e:  # noqa: BLE001
            raise dbus.DBusException(
                f"lineage store unavailable: {e}",
                name=BUS_NAME + ".LineageUnavailable",
            ) from e
        try:
            result = _commit_lineage.record_commit(store, **kwargs)
        except _commit_lineage.CommitDenied as e:
            # A successful fail-closed policy decision, NOT an infra failure:
            # distinct error so the caller aborts the commit (no retry, no
            # trailer leaked) rather than treating it as a transient outage.
            raise dbus.DBusException(
                str(e), name=BUS_NAME + ".LineagePolicyDenied"
            ) from e
        except _commit_lineage.BadCommitInput as e:
            raise dbus.DBusException(
                str(e), name=BUS_NAME + ".BadArgument"
            ) from e
        except dbus.DBusException:
            raise
        except Exception as e:  # noqa: BLE001
            raise dbus.DBusException(
                f"lineage store unavailable: {e}",
                name=BUS_NAME + ".LineageUnavailable",
            ) from e
        return json.dumps(
            {
                "version": 1,
                "lineage_sealed": True,
                "message": result.message,
                "commit_eid": result.commit_eid,
                "activity": result.activity_aid,
                "chain_head": result.chain_head,
            },
            sort_keys=True,
        )

    @dbus.service.method(
        BUS_NAME,
        in_signature="s",
        out_signature="s",
        sender_keyword="sender",
        connection_keyword="conn",
    )
    def RecordUploadLineage(self, descriptor_json: str, sender=None, conn=None) -> str:
        """Browser-upload chokepoint entry point (doc/lineage.md §Chokepoints
        "browser upload/download").

        ROOT-GATED, like ``RecordExportLineage`` and for the same reason: the
        upload chokepoint authorizes a source SOLELY by a global ``source_eid``
        lookup, and the lineage store has no per-silo source-ownership model yet
        (the production silo→source resolver that would bind a source to the
        authenticated silo is the documented blocker — see issues/qdistro/
        workflows-lineage-resources.md "Still open — entry-point invocation
        only"). Until that resolver exists, exposing this to a non-root silo
        would let any local caller mint a broker-sealed upload receipt + derived
        edge for ANY tracked, non-denied source it can name (cross-silo source
        forgery), attributed to its own silo. So the method is root-only at both
        the bus layer (deny in ``context="default"``) and here, and the bridge
        ``upload.record`` op is deliberately NOT wired. The legitimate live
        caller — a root upload helper that resolves the source to the
        authenticated silo and calls this — lands WITH that resolver.

        Authority model (already fail-closed via ``record_upload``):
          * The source security snapshot is read store-authoritatively from
            ``store.get_entity()``; the descriptor cannot supply it (the parser
            is a strict whitelist).
          * ``agent_gid`` is DERIVED from the authenticated D-Bus peer uid,
            never the body.
          * ``destination`` is legitimately caller-named; per file the caller may
            name only a ``source_eid`` reference, the ``digest`` of the bytes
            being sent, and an optional ``locator``.
          * A source the store has never recorded FAILS CLOSED (laundering
            guard); a guard-denied file refuses the whole batch and seals
            nothing.
        """
        uid, _pid, _exe, _st = self._require_root_lineage_peer(
            sender, conn, "RecordUploadLineage")
        # Agent identity is the AUTHENTICATED peer silo, never a body field.
        agent_gid = f"silo:{_username_for_uid(int(uid))}"
        try:
            desc = _upload_entry.load_descriptor_json(descriptor_json)
            store = self._get_lineage_store()
            files = [
                _upload_lineage.UploadFile(
                    source_eid=f.source_eid, digest=f.digest, locator=f.locator
                )
                for f in desc.files
            ]
            result = _upload_lineage.record_upload(
                store,
                files=files,
                destination=desc.destination,
                agent_gid=agent_gid,
            )
        except _upload_entry.UploadDescriptorError as e:
            raise dbus.DBusException(str(e), name=BUS_NAME + ".BadArgument") from e
        except _upload_lineage.UploadDenied as e:
            # The denial audit is already recorded in the store transaction
            # (record_upload commits phase-1 activities before raising); surface
            # a typed refusal so the caller aborts the upload.
            raise dbus.DBusException(
                str(e), name=BUS_NAME + ".LineagePolicyDenied"
            ) from e
        except _upload_lineage.BadUploadInput as e:
            # Includes the laundering guard: a source the store has never
            # recorded fails closed here.
            raise dbus.DBusException(
                str(e), name=BUS_NAME + ".LineageValidationFailed"
            ) from e
        except dbus.DBusException:
            raise
        except Exception as e:  # noqa: BLE001
            raise dbus.DBusException(
                f"lineage store unavailable: {e}",
                name=BUS_NAME + ".LineageUnavailable",
            ) from e
        return json.dumps(
            {
                "version": 1,
                "lineage_sealed": True,
                "manifest": result.manifest,
                "outputs": list(result.output_eids),
                "chain_head": result.chain_head,
            },
            sort_keys=True,
        )

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
        if not self.ratelimit.check(uid, action_s):
            raise dbus.DBusException(
                f"Rate limit exceeded for uid={uid} "
                f"action={action_s!r} (>{self.ratelimit.limit}/"
                f"{self.ratelimit.window_s}s). Check rejected.",
                name=BUS_NAME + ".RateLimited",
            )
        # Permission lineage (finding P0-1): the app_id / sandbox_engine
        # selectors are client-supplied and forgeable. Resolve the live
        # caller and use launcher-attested values (enforce mode) instead
        # of the raw claim; shadow mode logs divergence but preserves the
        # legacy claim. uid/action/exe/argv stay kernel-/argv-anchored.
        lin_engine, lin_app = self._lineage_selectors(
            pid, _selector_from_details(details, "sandbox_engine"),
            _selector_from_details(details, "app_id"),
            action_s, uid, exe)
        return self._decide_check(
            uid=uid, pid=pid, exe=exe, action_s=action_s,
            details=details, lin_app=lin_app, lin_engine=lin_engine)

    def _decide_check(self, *, uid: int, pid: int, exe: str, action_s: str,
                      details: dict, lin_app: str, lin_engine: str) -> str:
        """Shared rules→cache→hooks resolution for the synchronous
        permission gates. The (uid, pid, exe, lin_app, lin_engine) subject
        is the process being decided FOR — the D-Bus caller in
        CheckPermission, or the launcher-attested originating client in
        CheckPermissionForClient. Returns "allow"/"deny"/"unknown"."""
        argv = _argv_from_details(details)
        rule = self.rules.match(
            uid=uid, action=action_s, exe=exe,
            app_id=lin_app,
            sandbox_engine=lin_engine,
            mime_type=_selector_from_details(details, "mime_type"),
            argv=argv,
        )
        if rule is not None:
            return "allow" if rule.decision == "allow" else "deny"
        # Tier launch is rules-only: a stale approval cache row or hook
        # verdict must not mint a new sandboxed process. Disposable spawn
        # (qdistro.dispose.spawn:<workload>) joins the same fail-closed set
        # — a throwaway silo is still a sandboxed process and must not be
        # minted off a cached/hook verdict (07-disposables-plan P1).
        # Open-in-disposable (qdistro.dispose.open:<class>) is the same:
        # routing an untrusted input into a throwaway is a class-level
        # security decision that only an explicit admin rule may authorize —
        # a cache row or hook verdict must never mint an open
        # (07-disposables-plan P2). Export-back (qdistro.dispose.export:<class>)
        # joins the set too: promoting bytes OUT of a throwaway into a real silo
        # (the D7 copy-exception) is a class-level decision only an explicit
        # admin rule may authorize — never a cached/hook verdict.
        if action_s.startswith(("qdistro.tier1.spawn:",
                                "qdistro.tier2.spawn:",
                                "qdistro.dispose.spawn:",
                                "qdistro.dispose.open:",
                                "qdistro.dispose.export:")):
            return "unknown"
        # An authenticated sandboxed (tier-2) caller must not be
        # auto-decided by an argv-blind forever/forever_exe grant minted
        # for a different exe/argv/tier at this uid (issue
        # broker-forever-cache-scope). Derived from the verified launch
        # record, not the (shadow-mode forgeable) claimed selector.
        sandboxed = self._cache_sandboxed(pid)
        row = self.cache.lookup_detail(uid, action_s, exe, argv,
                                       sandboxed=sandboxed)
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
            hook_event: dict[str, Any] = dict(_sanitize_details(details))
            hook_event["caller_uid"] = uid
            hook_event["caller_pid"] = pid
            hook_event["caller_exe"] = exe
            hook_event["action_full"] = action_s
            hook_resp = self.hooks.query(action_s, hook_event)
            if hook_resp is not None:
                verdict = hook_resp.get("verdict")
                reason = hook_resp.get("reason", "")[:256]
                # Only audit an ACTIONABLE verdict. A non-None hook
                # response with a missing/unknown verdict falls through to
                # "unknown" (admin prompt) and must NOT write a
                # decision=False row that reads like a deny — mirrors the
                # _enqueue path, which audits only when a verdict decides.
                if verdict in ("allow", "transform", "deny"):
                    try:
                        self.audit.log(
                            caller_uid=uid, caller_pid=pid, caller_exe=exe,
                            action=action_s,
                            decision=(verdict in ("allow", "transform")),
                            scope=None, approver_uid=None,
                            source=f"hook verdict={verdict} reason={reason}")
                    except Exception as e:  # noqa: BLE001
                        print(f"[broker] qdistro.audit.failure: check_hook "
                              f"path, reason={e!r}", flush=True)
                if verdict in ("allow", "transform"):
                    return "allow"
                if verdict == "deny":
                    return "deny"
        except Exception:  # noqa: BLE001
            pass
        return "unknown"

    def _resolve_client_for_portal(self, client_pid: int,
                                   client_starttime: int):
        """Resolve a portal frontend's relayed client pid to its live
        kernel identity, anchored on the relayed starttime. Returns
        ``(uid, exe, ok)``: ok is False (fail closed) when the process is
        gone or the starttime drifted (PID reuse). The frontend is trusted
        only to *name* a pid it kernel-authenticated on the app's own
        connection; the broker re-reads /proc here so a recycled pid can
        never inherit an old app's decision."""
        live_exe, live_start = _read_proc_identity(client_pid)
        if live_start == 0:
            return (-1, "?", False)
        if client_starttime and int(live_start) != int(client_starttime):
            return (-1, live_exe, False)
        live_uid = _read_proc_uid(client_pid)
        if live_uid is None:
            return (-1, live_exe, False)
        return (int(live_uid), live_exe, True)

    @dbus.service.method(BUS_NAME, in_signature="sa{sv}ut", out_signature="s",
                         sender_keyword="sender", connection_keyword="conn")
    def CheckPermissionForClient(self, action: str, details: dict,
                                 client_pid: int, client_starttime: int,
                                 sender=None, conn=None) -> str:
        """Synchronous gate for a trusted portal **frontend** (Option A of
        the permission-lineage portal design). The frontend owns
        ``org.freedesktop.portal.Desktop`` on a silo session bus, so it can
        kernel-authenticate the originating app via
        ``GetConnectionUnixProcessID`` / SO_PEERCRED on the app's *own* D-Bus
        connection. It relays that ``(client_pid, client_starttime)`` here;
        the broker resolves the CLIENT (not the frontend) against the
        launch-record store and decides for it.

        Root-only (D-Bus policy + the in-method uid-0 check): the frontend
        runs privileged like ``qsu/root-exec``. A frontend-supplied pid is
        trusted only because (a) only the root frontend may call this and
        (b) the broker re-verifies the pid against /proc and anchors on the
        relayed starttime — so a recycled/gone pid fails closed. Same posture
        as the cross-silo source resolution; see findings P1-1 / portal §.
        """
        caller_uid, caller_pid, caller_exe, _ = self._peer_info(sender, conn)
        action_s = str(action)
        try:
            spid = int(client_pid)
            sstart = int(client_starttime)
        except (TypeError, ValueError):
            return "unknown"
        if caller_uid != 0:
            raise dbus.DBusException(
                f"CheckPermissionForClient restricted to root portal "
                f"frontends; got uid {caller_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        cuid, cexe, ok = self._resolve_client_for_portal(spid, sstart)
        if not ok:
            # The named client is gone or its pid was recycled — we cannot
            # authenticate it, so no rule/cache row may be applied for it.
            return "unknown"
        if not self.ratelimit.check(cuid, action_s):
            raise dbus.DBusException(
                f"Rate limit exceeded for client uid={cuid} "
                f"action={action_s!r}. Check rejected.",
                name=BUS_NAME + ".RateLimited",
            )
        lin_engine, lin_app = self._lineage_selectors(
            spid, _selector_from_details(details, "sandbox_engine"),
            _selector_from_details(details, "app_id"),
            action_s, cuid, cexe)
        return self._decide_check(
            uid=cuid, pid=spid, exe=cexe, action_s=action_s,
            details=details, lin_app=lin_app, lin_engine=lin_engine)

    @dbus.service.method(BUS_NAME, in_signature="sa{sv}ut", out_signature="i",
                         sender_keyword="sender", connection_keyword="conn")
    def RequestPermissionForClient(self, action: str, details: dict,
                                   client_pid: int, client_starttime: int,
                                   sender=None, conn=None) -> int:
        """Async (admin-prompt) twin of CheckPermissionForClient for a
        trusted portal frontend. Enqueues a pending request whose subject is
        the launcher-attested originating client, not the frontend. Root-only.
        Returns the request id, or 0 when the client can't be authenticated.
        """
        caller_uid, caller_pid, caller_exe, _ = self._peer_info(sender, conn)
        try:
            spid = int(client_pid)
            sstart = int(client_starttime)
        except (TypeError, ValueError):
            return 0
        if caller_uid != 0:
            raise dbus.DBusException(
                f"RequestPermissionForClient restricted to root portal "
                f"frontends; got uid {caller_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        cuid, cexe, ok = self._resolve_client_for_portal(spid, sstart)
        if not ok:
            return 0
        # _enqueue resolves the client pid via _lineage_selectors itself, so
        # the pending request carries the attested app_id/sandbox_engine.
        return self._enqueue(cuid, spid, cexe, sstart,
                             str(action), details, delegated=False)

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
                         in_signature="ssassssbut", out_signature="s",
                         sender_keyword="sender", connection_keyword="conn")
    def CheckClipboardTransfer(self, source_silo: str, dest_silo: str,
                               mime_types: list,
                               source_app_id: str = "",
                               dest_app_id: str = "",
                               source_sandbox_engine: str = "",
                               identity_verified: bool = False,
                               source_pid: int = 0,
                               source_starttime: int = 0,
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
        #
        # Permission lineage (P1-1): resolve the SOURCE app's relayed
        # (pid, starttime) to its launcher-attested silo/app/engine instead
        # of trusting the qdshell-claimed strings. Under enforce an
        # unattested source is denied cross-silo before the rule lookup.
        src, sapp, seng, _hard_deny = self._cross_silo_source(
            source_pid=source_pid, source_starttime=source_starttime,
            claimed_src=src, claimed_app=sapp, claimed_engine=seng,
            gate="clipboard.transfer", uid=uid, caller_pid=pid,
            caller_exe=exe)
        if _hard_deny:
            return "deny"
        action_s = f"qdistro.clipboard.transfer:{src}:{dst}"
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
        self._journal_cross_silo_decision(
            gate="transfer", src=src, dst=dst, decision=decision,
            src_app=sapp, src_engine=seng)
        return decision

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="s",
                         sender_keyword="sender", connection_keyword="conn")
    def PageExtract(self, body: str, sender=None, conn=None) -> str:
        """Browser "Send to…" share-to gate (Bridge Phase 9c).

        The browser bridge (running as the browser's uid) forwards a
        page-extract share-to action here. `body` is a JSON object with
        the fields the bridge sends in `_handle_page_extract`:

            url, title, selected_text, dest_uid, content_type,
            parent_exe, extension_id

        The reply is a JSON object the bridge decodes verbatim into a
        dict: ``{"ok": true}`` on allow, or
        ``{"ok": false, "error": "<reason>"}`` on refusal. The bridge
        has a synchronous 5 s D-Bus deadline, so this gate is
        synchronous (rules-engine lookup), mirroring
        ``CheckClipboardTransfer`` rather than the async admin-prompt
        path of ``RelayMessage``.

        Trust model: the *source* identity is the authenticated D-Bus
        caller resolved to its silo name (``_peer_info`` uid → username),
        NOT any value in the JSON body — a compromised bridge cannot
        claim to be a different source user. Only the *destination*
        (``dest_uid``) is taken from the body, since the caller is
        legitimately naming where to send. ``dest_uid`` is treated as an
        opaque destination silo identifier (a username / silo name such
        as ``dev-user`` or a numeric-uid string), mirroring how the
        clipboard gates key on silo names — the bridge / extension
        schema declares it a free-form string.

        Policy:
          * Same-user (source silo == dest silo): allowed without a rule
            (the data never leaves the user's own silo). Audited.
          * Cross-user: rules-engine lookup on the synthetic action
            ``qdistro.share_to:<source>:<dest>`` with the
            ``content_type`` exposed as the ``mime_type`` selector so
            admins can author per-content-type ``share_to`` rules
            (todo/browser/01-bridge-phase9.md §9c). Default-DENY when no
            rule matches — cross-user data movement is opt-in.

        Every decision (allow or deny, same- or cross-user) writes one
        audit row carrying the synthetic action, the destination, the
        content type, and the originating extension/exe.
        """
        uid, pid, exe, _st = self._peer_info(sender, conn)

        # --- parse + validate the request body --------------------------
        try:
            req = json.loads(body)
        except (TypeError, ValueError):
            return json.dumps({"ok": False, "error": "malformed_body"})
        if not isinstance(req, dict):
            return json.dumps({"ok": False, "error": "malformed_body"})

        url = req.get("url")
        if not isinstance(url, str) or not url:
            return json.dumps({"ok": False, "error": "missing_url"})

        # Destination is an opaque silo identifier supplied by the
        # caller. Sanitize (strip + cap) the same way the clipboard gates
        # cap silo names; an empty destination is unroutable.
        dst = str(req.get("dest_uid") or "").strip()
        if not dst:
            return json.dumps({"ok": False, "error": "missing_dest"})
        if len(dst) > 80:
            return json.dumps({"ok": False, "error": "bad_dest"})
        # Normalize a uid-shaped destination ("1001") to its username so
        # it shares the namespace with the resolved source below: the
        # same-user bypass and the rule action shape must compare like
        # with like, whether the caller sent a numeric uid or a
        # silo/username string. Non-numeric destinations (the documented
        # share-to case, e.g. "dev-user") pass through unchanged.
        if dst.isdigit():
            dst = _username_for_uid(int(dst))

        # Content type drives a per-type rule selector; cap + default it.
        content_type = str(req.get("content_type") or "url")[:64]
        ext_id = str(req.get("extension_id") or "")[:128]
        parent_exe = str(req.get("parent_exe") or "")[:256]

        # Source silo = the AUTHENTICATED caller, resolved to its
        # username. Falls back to ``uid:<n>`` if the uid has no passwd
        # entry (e.g. a freshly-provisioned silo) so the action is still
        # well-formed and rule-addressable.
        src = _username_for_uid(int(uid))
        action_s = f"qdistro.share_to:{src}:{dst}"

        if not self.ratelimit.check(uid, action_s):
            raise dbus.DBusException(
                f"Rate limit exceeded for uid={uid} "
                f"action={action_s!r} (>{self.ratelimit.limit}/"
                f"{self.ratelimit.window_s}s). Share rejected.",
                name=BUS_NAME + ".RateLimited",
            )

        # Audit context shared by every path; never carry the page body
        # (selected_text / full text) into the audit row — it can be
        # arbitrary page content. URL + title host are enough for review.
        audit_ctx = (f"content_type={content_type} dest={dst} "
                     f"ext={ext_id or '(unknown)'} "
                     f"parent_exe={parent_exe or '(unknown)'}")

        # --- same-user: data never leaves the source silo --------------
        if src == dst:
            try:
                self.audit.log(
                    caller_uid=uid, caller_pid=pid, caller_exe=exe,
                    action=action_s, decision=True, scope=None,
                    source=f"share_to_same_user {audit_ctx}",
                    approver_uid=None,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[broker] qdistro.audit.failure: share_to_same_user,"
                      f" reason={e!r}", flush=True)
            return json.dumps({"ok": True})

        # --- cross-user: rules lookup, default-deny --------------------
        rule = self.rules.match(uid=uid, action=action_s, exe=exe,
                                mime_type=content_type)
        decision = "deny"
        rule_path = None
        source_label = "share_to_default_deny"
        if rule is not None:
            decision = "allow" if rule.decision == "allow" else "deny"
            rule_path = rule.source_path
            source_label = "share_to_rule"
        try:
            self.audit.log(
                caller_uid=uid, caller_pid=pid, caller_exe=exe,
                action=action_s, decision=(decision == "allow"),
                scope=None, source=f"{source_label} {audit_ctx}",
                approver_uid=None, rule_path=rule_path,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[broker] qdistro.audit.failure: share_to, "
                  f"reason={e!r}", flush=True)
        if decision == "allow":
            return json.dumps({"ok": True})
        return json.dumps({"ok": False, "error": "policy_denied"})

    @dbus.service.method(BUS_NAME,
                         in_signature="ssssssbut", out_signature="s",
                         sender_keyword="sender", connection_keyword="conn")
    def CheckClipboardReceive(self, source_silo: str, dest_silo: str,
                              mime_type: str,
                              source_app_id: str = "",
                              dest_app_id: str = "",
                              source_sandbox_engine: str = "",
                              identity_verified: bool = False,
                              source_pid: int = 0,
                              source_starttime: int = 0,
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
        # Permission lineage (P1-1): resolve the source app to its
        # launcher-attested identity; deny cross-silo under enforce when
        # the source is unattested. See CheckClipboardTransfer.
        src, sapp, seng, _hard_deny = self._cross_silo_source(
            source_pid=source_pid, source_starttime=source_starttime,
            claimed_src=src, claimed_app=sapp, claimed_engine=seng,
            gate="clipboard.receive", uid=uid, caller_pid=pid,
            caller_exe=exe)
        if _hard_deny:
            return "deny"
        action_s = f"qdistro.clipboard.receive:{src}:{dst}"
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
        self._journal_cross_silo_decision(
            gate="receive", src=src, dst=dst, decision=decision,
            src_app=sapp, src_engine=seng)
        return decision

    @dbus.service.method(BUS_NAME,
                         in_signature="sssssbut", out_signature="s",
                         sender_keyword="sender", connection_keyword="conn")
    def CheckHandoffActivation(self, source_silo: str, dest_silo: str,
                               source_app_id: str, dest_app_id: str,
                               source_sandbox_engine: str = "",
                               identity_verified: bool = False,
                               source_pid: int = 0,
                               source_starttime: int = 0,
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
        # Permission lineage (P1-1): resolve the source app to its
        # launcher-attested identity; deny cross-silo under enforce when
        # the source is unattested. See CheckClipboardTransfer.
        src, sapp_raw, seng_raw, _hard_deny = self._cross_silo_source(
            source_pid=source_pid, source_starttime=source_starttime,
            claimed_src=src, claimed_app=sapp_raw, claimed_engine=seng_raw,
            gate="handoff.activate", uid=uid, caller_pid=pid, caller_exe=exe)
        if _hard_deny:
            return "deny"
        # _cross_silo_source() may have replaced the claimed identity with the
        # launcher-attested one (sapp_raw/seng_raw). Refresh the display values
        # so the audit row and journal line report the identity the decision is
        # actually made against, matching CheckClipboardTransfer/Receive which
        # bind sapp/seng directly to the resolved return.
        sapp = sapp_raw or "(unknown)"
        seng = seng_raw or "(unknown)"
        action_s = f"qdistro.handoff.activate:{src}:{dst}"
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
        self._journal_cross_silo_decision(
            gate="handoff", src=src, dst=dst, decision=decision,
            src_app=sapp, src_engine=seng)
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
        # Verify the delegated tuple is still true immediately before
        # accepting it. qsu also rechecks after socket connect; keeping
        # the broker check here prevents stale or hand-written
        # RequestPermissionAs calls from enqueueing misleading identity.
        try:
            expected_start_time = int(
                dict(details or {}).get("caller_start_time") or 0)
        except (TypeError, ValueError):
            expected_start_time = 0
        live_exe, start_time = _verify_delegated_claim(
            int(caller_uid), int(caller_pid), str(caller_exe),
            expected_start_time)
        return self._enqueue(int(caller_uid), int(caller_pid),
                             live_exe, start_time,
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

        Fail-open vs fail-closed: the default (REQUIRE_SILO_ACTIVE on)
        is fail-CLOSED — every manager error returns "Unreachable" so
        RelayMessage refuses the cross-uid relay. Both branches log a
        structured warning so operators auditing "why was this relay
        refused/allowed?" have a breadcrumb. A legacy host that ships
        the broker without the session manager can restore the old
        permissive fall-through (return None on error → pre-P02 trust
        path) with QDISTRO_BROKER_REQUIRE_SILO_ACTIVE=0 or
        require_silo_active=false in /etc/qdistro/broker.conf. Note the
        "manager reachable but no row" return below is always None
        regardless of this toggle — that is a real registry answer, not
        an error, and the relay still requires admin approval.
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
                    name=BUS_NAME + ".TargetNotReady") from e
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
        # --- Phase 1 (synchronous, fast): rules / cache / hooks ------
        #
        # Resolution order per spec/07: rules first, cache second,
        # hooks third, prompt last. A rule-matched decision is
        # authoritative — it doesn't flow through the admin prompt even
        # for allows. one_shot actions skip all tiers: every call
        # reaches admin.
        #
        # Tier-2 admission security (audit 2026-05-27): when all four
        # tiers (rules, cache, hooks, prompt) are exhausted without a
        # pre-decision, the request stays pending in _pending until
        # admin acts -- this is operationally default-deny.  There is
        # no hardcoded allow for sandbox_engine="qdistro.tier2" or any
        # other sandbox_engine value.  The cross-silo gates
        # (CheckClipboardTransfer, CheckClipboardReceive,
        # CheckHandoffActivation) are even stricter: they return
        # "deny" when rules.match() returns None, without reaching the
        # prompt queue at all.
        #
        # Tier-2 escalation guard (issue broker-forever-cache-scope):
        # the cache rows carry no sandbox_engine column, so an argv-blind
        # forever/forever_exe grant minted for a non-sandboxed process
        # could once admit a same-(uid, action) tier-2 request. We now
        # derive `sandboxed` from the VERIFIED launch record and skip the
        # argv-blind kinds for an authenticated sandboxed caller (see
        # _cache_sandboxed); it falls through to an argv-pinned row, a
        # rule/hook, or the admin prompt (default-deny). Cross-silo
        # actions additionally use tier-specific synthetic action strings.
        matched_rule = None
        cached_row = None
        hook_verdict = None
        if not one_shot:
            argv = _argv_from_details(details)
            # Permission lineage (finding P0-1): resolve the live caller
            # and use launcher-attested app_id/sandbox_engine in enforce
            # mode rather than the forgeable client-supplied claim.
            # Delegated requests (RequestPermissionAs) carry the *real*
            # caller's pid, so the resolution targets the right process.
            lin_engine, lin_app = self._lineage_selectors(
                pid, _selector_from_details(details, "sandbox_engine"),
                _selector_from_details(details, "app_id"),
                action_s, uid, exe)
            matched_rule = self.rules.match(
                uid=uid, action=action_s, exe=exe,
                app_id=lin_app,
                sandbox_engine=lin_engine,
                mime_type=_selector_from_details(details, "mime_type"),
                argv=argv,
            )
            if matched_rule is None:
                # Delegated requests must not be auto-satisfied by an
                # argv-blind (exe_only / always) row — the decision-time
                # mirror of the _DELEGATED_FORBIDDEN_SCOPES store guard.
                # Likewise an authenticated sandboxed (tier-2) caller
                # must not inherit a uid-wide forever/forever_exe grant
                # minted for a different exe/argv/tier (issue
                # broker-forever-cache-scope). The sandboxed flag is
                # anchored on the verified launch record, not the
                # (shadow-mode forgeable) claimed sandbox_engine.
                sandboxed = self._cache_sandboxed(pid)
                cached_row = self.cache.lookup_detail(
                    uid, action_s, exe, argv, delegated=delegated,
                    sandboxed=sandboxed)

        # Sanitise caller-supplied details before storing them. The
        # admin UI (GUI/TUI) renders these verbatim; without scrubbing,
        # a hostile caller can inject ANSI escapes or newlines that
        # draw fake approval banners inside the detail pane.
        clean_details = _sanitize_details(details)

        # Hook consultation: when rules and cache are both inconclusive,
        # ask the sandboxed hook executor before falling through to the
        # admin prompt. Done outside the lock and before creating a
        # _Request — the hook query is I/O-bound (AF_UNIX round-trip)
        # and we don't want to hold the lock during it.
        if not one_shot and matched_rule is None and cached_row is None:
            try:
                hook_event: dict[str, Any] = dict(clean_details)
                hook_event["caller_uid"] = uid
                hook_event["caller_pid"] = pid
                hook_event["caller_exe"] = exe
                hook_event["action_full"] = action_s
                hook_verdict = self.hooks.query(action_s, hook_event)
            except Exception as e:  # noqa: BLE001
                print(f"[broker] hook query failed: {e!r}", flush=True)
                hook_verdict = None

        # --- Phase 2 (synchronous, fast): allocate rid + _Request ----
        #
        # The layered-identity IO (_read_proc_layered) is deferred to
        # Phase 3 (thread pool) so the D-Bus method reply returns in
        # <1 ms even when N concurrent qsu clients hit the broker
        # simultaneously. The _Request is created with
        # layered_pending=True; the idle callback in Phase 3 fills in
        # exe_sha256 / selinux_label / cgroup once the IO completes.
        #
        # Requests that are decided immediately (rule / cache / hook)
        # skip the deferred IO — the layered fields are advisory only
        # and not needed for the decision or its cache key.
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            req = _Request(rid, uid, pid, exe, start_time, action_s,
                           clean_details, delegated=delegated,
                           one_shot=one_shot,
                           layered_pending=True)
            if matched_rule is not None:
                req.decision = (matched_rule.decision == "allow")
                req.layered_pending = False  # no layered IO needed
            elif cached_row is not None:
                req.decision = bool(cached_row["decision"])
                req.layered_pending = False
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
                if req.decision is not None:
                    req.layered_pending = False
            self._pending[rid] = req

        if req.decision is None:
            # --- Phase 3 (deferred, thread pool): layered IO ----------
            #
            # Submit _read_proc_layered to the broker's IO thread pool.
            # On completion, _apply_layered_identity runs on the GLib
            # mainloop thread via idle_add — the _pending dict is only
            # modified from the mainloop, preserving thread safety.
            #
            # The worker captures (start_time, exe) at submit time and
            # verifies the process identity on the pool thread before
            # hashing. If the process was recycled (start_time changed)
            # or exec'd (exe path changed), results are discarded
            # (empty strings) rather than displaying a misleading hash.
            io_pool = getattr(self, "_io_pool", None)
            if pid > 0 and io_pool is not None:
                try:
                    fut = io_pool.submit(
                        _read_proc_layered_checked, pid, start_time, exe)
                    fut.add_done_callback(
                        lambda f, r=rid: GLib.idle_add(
                            self._apply_layered_identity, r, f))
                except RuntimeError:
                    # Pool shut down (broker teardown) — leave fields empty.
                    with self._lock:
                        req.layered_pending = False
            elif pid > 0:
                # Fallback: no thread pool (e.g. test harness without
                # _io_pool). Run synchronously with the same identity
                # guard as the threaded path.
                layered = _read_proc_layered_checked(pid, start_time, exe)
                with self._lock:
                    req.exe_sha256 = layered.get("exe_sha256", "")
                    req.selinux_label = layered.get("selinux_label", "")
                    req.cgroup = layered.get("cgroup", "")
                    req.layered_pending = False
            else:
                with self._lock:
                    req.layered_pending = False
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

    def _apply_layered_identity(self, rid: int, future) -> bool:
        """GLib idle callback: apply layered-identity IO results.

        Called on the mainloop thread (via GLib.idle_add) when the
        _read_proc_layered future completes on a worker thread.

        Thread safety: this callback runs on the GLib mainloop thread,
        which is the same thread that handles all D-Bus method calls
        (GetPending, DecideRequest, etc.). The lock is taken for
        consistency with the rest of the broker — even though mainloop
        callbacks don't preempt each other, the lock makes the
        single-writer invariant explicit and future-proofs against any
        code path that might read _pending from outside the mainloop.
        """
        try:
            layered = future.result()
        except Exception as e:  # noqa: BLE001
            print(f"[broker] layered-identity IO failed for rid={rid}: {e!r}",
                  flush=True)
            layered = {"exe_sha256": "", "selinux_label": "", "cgroup": ""}
        with self._lock:
            req = self._pending.get(rid)
            if req is None:
                return False  # already decided + reaped
            req.exe_sha256 = layered.get("exe_sha256", "")
            req.selinux_label = layered.get("selinux_label", "")
            req.cgroup = layered.get("cgroup", "")
            req.layered_pending = False
        return False  # don't repeat

    @dbus.service.method(BUS_NAME, in_signature="i", out_signature="b",
                        async_callbacks=("_reply", "_error"),
                        sender_keyword="sender", connection_keyword="conn")
    def WaitForDecision(self, request_id: int, _reply, _error,
                        sender=None, conn=None):
        waiter_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        with self._lock:
            req = self._pending.get(int(request_id))
            if req is None:
                _reply(False)
                return
            if waiter_uid not in (0, ADMIN_UID, req.uid):
                _error(dbus.DBusException(
                    f"WaitForDecision request {int(request_id)} belongs "
                    f"to uid {req.uid}; got uid {waiter_uid}",
                    name=BUS_NAME + ".AccessDenied",
                ))
                return
            if req.decision is not None:
                _reply(bool(req.decision))
                return
            req.waiters.append((_reply, _error))

    @dbus.service.method(BUS_NAME, in_signature="", out_signature="aa{sv}",
                         sender_keyword="sender", connection_keyword="conn")
    def GetPending(self, sender=None, conn=None) -> list[dict[str, Any]]:
        """Return undecided requests for the admin approvals UI.

        Admin/root only. The D-Bus policy file is expected to enforce the
        same boundary, but keep the in-process peer check so a policy
        regression cannot expose pending request metadata to users.
        """
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"GetPending restricted to admin/root; got uid {admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
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
                    # layered_pending=True means the IO thread hasn't
                    # finished yet; admin app can show a spinner or
                    # display the request with empty layered fields.
                    "exe_sha256":      dbus.String(r.exe_sha256),
                    "selinux_label":   dbus.String(r.selinux_label),
                    "cgroup":          dbus.String(r.cgroup),
                    "layered_pending": dbus.Boolean(r.layered_pending),
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
            cache_argv = _argv_from_details(req.details)
            if (decision_s == "allow" and scope_s in _ARGV_REQUIRED_SCOPES
                    and not cache_argv):
                context = "delegated requests" if req.delegated else "this request"
                raise dbus.DBusException(
                    f"scope {scope_s!r} requires captured argv for {context}",
                    name=BUS_NAME + ".ScopeNotPermitted",
                )
            # Layered identity (exe_sha256, selinux_label, cgroup) is
            # advisory per spec/25 — it enriches the admin's view but
            # does NOT gate the decision or the cache key. The cache
            # key for forever_exe is (uid, action, exe_path), not
            # exe_sha256; other scopes are even broader. So a decision
            # made before layered IO completes is correct by design:
            # the admin saw uid/pid/exe/action in the approval pane,
            # which is the same data the cache row persists.
            #
            # We still log a warning for forever_exe (the scope most
            # visually tied to exe identity) so audit reviewers know
            # the exe_sha256 column was empty at approve time.
            if req.layered_pending and scope_s == "forever_exe":
                print(f"[broker] WARN: DecideRequest rid={request_id} "
                      f"scope=forever_exe decided before layered-identity "
                      f"IO completed (exe_sha256 not yet available)",
                      flush=True)
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
            # per-element argv[NN] keys). None when the request carried
            # no argv (clipboard / handoff / qdistro.test.* — those use
            # exe_only / always scopes anyway).

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
                ) from e

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
            ) from e
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
        import re
        import tempfile
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
                    "SaveRule: rule validation failed: " + "; ".join(errs),
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

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="bias",
                         sender_keyword="sender", connection_keyword="conn")
    def DeleteRule(self, source_path: str, name: str,
                   sender=None, conn=None):
        """Delete a YAML rule file and hot-reload the engine.

        Replaces the admin app's former direct ``os.remove`` of the
        rule file (security-hardening-carryforward §"Broker and rules":
        rule deletion should be a broker RPC with a broker-side audit
        row, not a direct UI file unlink). Admin/root only.

        ``source_path`` is the path ListRules returned for the rule;
        ``name`` is carried only for the audit row. All path safety is
        re-applied broker-side and fail-closed — the client's path is
        NOT trusted:

          - the file's realpath must resolve to a regular file whose
            *containing directory* is the canonical rules dir this
            broker's RulesEngine watches (rejects ``..`` escapes and
            files outside rules.d),
          - the supplied path must not be a symlink, and the realpath
            must not differ in basename from a real entry of rules.d
            (rejects symlink/hardlink redirection),
          - the file must exist.

        To avoid a TOCTOU between the safety check and the unlink, the
        validated absolute path is opened-by-directory and removed via
        ``os.unlink`` of the *basename* relative to an O_DIRECTORY fd of
        the canonical rules dir, so a swap of an intermediate component
        after validation cannot redirect the delete.

        Returns ``(deleted, reload_count, errors)``. Raises a
        DBusException with ``.AccessDenied`` for non-admin callers or
        ``.RulesEngineRefused`` when path safety rejects the target.
        Every outcome (success or refusal) is written to the broker
        audit log with the caller's identity.
        """
        admin_uid, caller_pid, caller_exe, _st = self._peer_info(sender, conn)

        def _audit(decision: bool, *, reason: str, path: str | None,
                   fail_closed: bool = False) -> None:
            try:
                self.audit.log(
                    caller_uid=admin_uid, caller_pid=caller_pid,
                    caller_exe=caller_exe,
                    action="qdistro.rules.delete",
                    decision=decision, scope=None,
                    source=f"dbus-deleterule name={name!r} {reason}",
                    approver_uid=None,
                    rule_path=path,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[broker] DeleteRule audit log failed: {e!r}",
                      flush=True)
                # On the success path the audit row is the whole point of
                # this RPC, so audit-fail means refuse the delete rather
                # than silently dropping the forensic record.
                if fail_closed:
                    raise dbus.DBusException(
                        f"DeleteRule: aborting, audit log write failed: {e}",
                        name=BUS_NAME + ".RulesEngineRefused",
                    ) from e

        if admin_uid not in (0, ADMIN_UID):
            _audit(False, reason=f"refused: non-admin uid {admin_uid}",
                   path=source_path or None)
            raise dbus.DBusException(
                f"DeleteRule restricted to admin/root; got uid {admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )

        if not source_path:
            _audit(False, reason="refused: empty source_path", path=None)
            raise dbus.DBusException(
                "DeleteRule: empty source_path",
                name=BUS_NAME + ".RulesEngineRefused",
            )

        # Canonical rules dir this broker actually watches (tests wire a
        # tmp_path-backed RulesEngine; production wires /etc/qdistro/rules.d).
        rules_real = os.path.realpath(self.rules._dir)
        # Reject the supplied path itself being a symlink (ln -s redirect).
        if os.path.islink(source_path):
            _audit(False, reason="refused: symlink", path=source_path)
            raise dbus.DBusException(
                f"DeleteRule: refusing to delete a symlink: {source_path}",
                name=BUS_NAME + ".RulesEngineRefused",
            )
        real = os.path.realpath(source_path)
        # The file's containing directory must be exactly the canonical
        # rules dir (rejects ../ escapes and files outside rules.d).
        if os.path.dirname(real) != rules_real:
            _audit(False, reason="refused: outside rules.d", path=real)
            raise dbus.DBusException(
                f"DeleteRule: refusing to delete a file outside "
                f"{rules_real}: {real}",
                name=BUS_NAME + ".RulesEngineRefused",
            )
        # Must be an existing regular file (not a dir / missing / special).
        if not os.path.isfile(real) or os.path.islink(real):
            _audit(False, reason="refused: missing or not a regular file",
                   path=real)
            raise dbus.DBusException(
                f"DeleteRule: rule file not found or not a regular file: "
                f"{real}",
                name=BUS_NAME + ".RulesEngineRefused",
            )

        basename = os.path.basename(real)
        # TOCTOU-resistant unlink: open the canonical rules dir as an
        # O_DIRECTORY fd and unlink the basename relative to it, so an
        # attacker swapping an intermediate path component after the
        # checks above cannot redirect the delete elsewhere.
        try:
            dir_fd = os.open(rules_real, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as e:
            _audit(False, reason=f"refused: cannot open rules dir: {e!r}",
                   path=real)
            raise dbus.DBusException(
                f"DeleteRule: cannot open rules dir {rules_real}: {e}",
                name=BUS_NAME + ".RulesEngineRefused",
            ) from e
        try:
            # Re-check by-fd that the basename entry is not a symlink and
            # is a regular file at unlink time, then unlink relative to
            # the dir fd. Any OSError on this race path is converted to a
            # fail-closed refusal (and audited), never a bare traceback.
            import stat as _stat
            try:
                st = os.lstat(basename, dir_fd=dir_fd)
            except OSError as e:
                _audit(False, reason=f"refused: lstat at unlink failed: {e!r}",
                       path=real)
                raise dbus.DBusException(
                    f"DeleteRule: rule entry {basename} vanished or is "
                    f"inaccessible at unlink time: {e}",
                    name=BUS_NAME + ".RulesEngineRefused",
                ) from e
            if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISREG(st.st_mode):
                _audit(False,
                       reason="refused: entry changed to symlink/non-regular",
                       path=real)
                raise dbus.DBusException(
                    f"DeleteRule: refusing to delete {basename}: not a "
                    f"regular file at unlink time",
                    name=BUS_NAME + ".RulesEngineRefused",
                )
            # Audit the (about-to-happen) deletion FIRST and fail-closed:
            # the broker-side audit row is the whole point of this RPC, so
            # if the audit write fails we abort before unlinking rather
            # than delete without a forensic record.
            _audit(True, reason="deleted", path=real, fail_closed=True)
            try:
                os.unlink(basename, dir_fd=dir_fd)
            except OSError as e:
                _audit(False, reason=f"unlink failed after audit: {e!r}",
                       path=real)
                raise dbus.DBusException(
                    f"DeleteRule: unlink of {basename} failed: {e}",
                    name=BUS_NAME + ".RulesEngineRefused",
                ) from e
        finally:
            os.close(dir_fd)

        print(f"[broker] DeleteRule removed {real} (name={name!r}, "
              f"admin_uid={admin_uid})", flush=True)
        # Hot reload via the shared helper (also re-emits RulesReloaded).
        n = self.reload_rules_from_disk(source="dbus-deleterule")
        errs = self.rules.load_errors()
        return dbus.Boolean(True), dbus.Int32(n), dbus.Array(errs,
                                                             signature="s")

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="s",
                         sender_keyword="sender", connection_keyword="conn")
    def GetRuleSource(self, source_path: str,
                      sender=None, conn=None) -> str:
        """Return the raw on-disk YAML text of a rule file for editing.

        Powers the admin app's "Edit rule" path: when editing an
        existing rule the editor must show the actual file text so that
        comments and any keys ListRules does not project (future/unknown
        keys) are preserved on round-trip, instead of regenerating YAML
        from the projected ListRules fields and dropping them
        (security-hardening-carryforward §"Broker and rules"). The admin
        app cannot read /etc/qdistro/rules.d directly (root-owned), so
        this is a broker RPC — the same reason DeleteRule exists.

        Read-only, so no audit row (matching the local convention for
        the other read RPCs ListRules/ReloadRules). Admin/root only.

        ``source_path`` is the path ListRules returned for the rule. All
        path safety is re-applied broker-side and fail-closed exactly as
        DeleteRule does — the client's path is NOT trusted:

          - the supplied path must not be a symlink (rejects ln -s
            redirect),
          - the file's realpath's *containing directory* must be exactly
            the canonical rules dir this broker's RulesEngine watches
            (rejects ``..`` escapes and files outside rules.d),
          - the realpath must be an existing regular file.

        To avoid a TOCTOU between the safety check and the read, the
        validated absolute path is opened-by-directory and read via an
        ``os.open`` of the *basename* relative to an ``O_DIRECTORY`` fd
        of the canonical rules dir with ``O_NOFOLLOW``, so a swap of an
        intermediate component after validation cannot redirect the read
        outside rules.d.

        Returns the file's UTF-8 text. Raises a DBusException with
        ``.AccessDenied`` for non-admin callers or ``.RulesEngineRefused``
        when path safety rejects the target.
        """
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"GetRuleSource restricted to admin/root; got uid "
                f"{admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        if not source_path:
            raise dbus.DBusException(
                "GetRuleSource: empty source_path",
                name=BUS_NAME + ".RulesEngineRefused",
            )

        # Canonical rules dir this broker actually watches (tests wire a
        # tmp_path-backed RulesEngine; production wires /etc/qdistro/rules.d).
        rules_real = os.path.realpath(self.rules._dir)
        # Reject the supplied path itself being a symlink (ln -s redirect).
        if os.path.islink(source_path):
            raise dbus.DBusException(
                f"GetRuleSource: refusing to read a symlink: {source_path}",
                name=BUS_NAME + ".RulesEngineRefused",
            )
        real = os.path.realpath(source_path)
        # The file's containing directory must be exactly the canonical
        # rules dir (rejects ../ escapes and files outside rules.d).
        if os.path.dirname(real) != rules_real:
            raise dbus.DBusException(
                f"GetRuleSource: refusing to read a file outside "
                f"{rules_real}: {real}",
                name=BUS_NAME + ".RulesEngineRefused",
            )
        if not os.path.isfile(real) or os.path.islink(real):
            raise dbus.DBusException(
                f"GetRuleSource: rule file not found or not a regular "
                f"file: {real}",
                name=BUS_NAME + ".RulesEngineRefused",
            )
        # Narrow the RPC's authority to rule files: only .yaml/.yml
        # entries are served, so it cannot be used to read arbitrary
        # non-rule regular files that happen to sit directly inside
        # rules.d. (We deliberately do NOT require the path to back a
        # currently *loaded* rule: an admin must be able to open a rule
        # file that fails to parse — e.g. to fix a malformed entry — or
        # one carrying keys a future engine will accept but this one
        # rejects, which is exactly the comments/future-keys preservation
        # this RPC exists for.)
        if os.path.splitext(real)[1].lower() not in (".yaml", ".yml"):
            raise dbus.DBusException(
                f"GetRuleSource: not a YAML rule file: {real}",
                name=BUS_NAME + ".RulesEngineRefused",
            )

        basename = os.path.basename(real)
        # TOCTOU-resistant read: open the canonical rules dir as an
        # O_DIRECTORY fd and open the basename relative to it with
        # O_NOFOLLOW, so an attacker swapping an intermediate path
        # component (or the entry into a symlink) after the checks above
        # cannot redirect the read outside rules.d.
        try:
            dir_fd = os.open(rules_real, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as e:
            raise dbus.DBusException(
                f"GetRuleSource: cannot open rules dir {rules_real}: {e}",
                name=BUS_NAME + ".RulesEngineRefused",
            ) from e
        try:
            try:
                file_fd = os.open(
                    basename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except OSError as e:
                raise dbus.DBusException(
                    f"GetRuleSource: cannot open rule {basename}: {e}",
                    name=BUS_NAME + ".RulesEngineRefused",
                ) from e
            import stat as _stat
            st = os.fstat(file_fd)
            if not _stat.S_ISREG(st.st_mode):
                os.close(file_fd)
                raise dbus.DBusException(
                    f"GetRuleSource: refusing to read {basename}: not "
                    f"a regular file at read time",
                    name=BUS_NAME + ".RulesEngineRefused",
                )
            # os.fdopen takes ownership of file_fd and closes it on exit.
            with os.fdopen(file_fd, "r", encoding="utf-8") as f:
                text = f.read()
        finally:
            os.close(dir_fd)

        return dbus.String(text)

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

    @dbus.service.method(BUS_NAME, in_signature="", out_signature="aa{sv}",
                         sender_keyword="sender", connection_keyword="conn")
    def ListWorkflows(self, sender=None, conn=None) -> list[dict]:
        """Return loaded workflow definitions for the admin Workflows tab.

        Admin/root only. Read-only metadata (no secret values): name,
        trigger_type, description, needs, step_count, source_path. Returns
        an empty list when the workflow engine isn't running.
        """
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"ListWorkflows restricted to admin/root; got uid {admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        engine = getattr(self, "workflow_engine", None)
        if engine is None:
            return []
        out = []
        for wf in engine.list_workflow_defs():
            out.append({
                "name":         dbus.String(wf.get("name", "")),
                "trigger_type": dbus.String(wf.get("trigger_type", "")),
                "description":  dbus.String(wf.get("description", "")),
                "needs":        dbus.Array(
                    [dbus.String(x) for x in wf.get("needs", ())],
                    signature="s"),
                "step_count":   dbus.Int32(int(wf.get("step_count", 0))),
                "source_path":  dbus.String(wf.get("source_path", "")),
            })
        return out

    @dbus.service.method(BUS_NAME, in_signature="i", out_signature="aa{sv}",
                         sender_keyword="sender", connection_keyword="conn")
    def ListWorkflowRuns(self, limit: int, sender=None,
                         conn=None) -> list[dict]:
        """Return the N most-recent workflow runs for the admin Workflows
        tab. Admin/root only; read-only. `limit` <= 0 defaults to 200.
        Empty list when the workflow engine isn't running.
        """
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"ListWorkflowRuns restricted to admin/root; got uid "
                f"{admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        engine = getattr(self, "workflow_engine", None)
        if engine is None:
            return []
        n = int(limit) if int(limit) > 0 else 200
        out = []
        for r in engine.recent_runs(n):
            out.append({
                "run_id":        dbus.String(str(r.get("run_id", ""))),
                "workflow_name": dbus.String(str(r.get("workflow_name", ""))),
                "state":         dbus.String(str(r.get("state", ""))),
                "started_at":    dbus.Double(float(r.get("started_at") or 0.0)),
                "completed_at":  dbus.Double(
                    float(r.get("completed_at") or 0.0)),
                "error":         dbus.String(str(r.get("error") or "")),
            })
        return out

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="b",
                         sender_keyword="sender", connection_keyword="conn")
    def ApproveWorkflowRun(self, run_id: str, sender=None,
                           conn=None) -> bool:
        """Approve a PENDING workflow run so it executes (F3).

        Human-in-the-loop gate: a workflow that did not opt into
        ``auto_run`` parks each fire as a PENDING run; an admin approves it
        here. Admin/root only at the bus level AND server-side. Returns
        True if a pending run was found and scheduled (conditions are still
        re-evaluated at execution, so approval never bypasses the identity
        gate). False if no such pending run exists.
        """
        admin_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if admin_uid not in (0, ADMIN_UID):
            raise dbus.DBusException(
                f"ApproveWorkflowRun restricted to admin/root; got uid "
                f"{admin_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        engine = getattr(self, "workflow_engine", None)
        if engine is None:
            return False
        try:
            approved = bool(engine.approve_run(str(run_id)))
        except Exception as e:  # noqa: BLE001
            print(f"[broker] ApproveWorkflowRun failed: {e!r}", flush=True)
            return False
        if approved:
            try:
                self.audit.log(
                    caller_uid=admin_uid, caller_pid=_pid,
                    caller_exe=_exe or "qdistro-admin",
                    action=f"qdistro.workflow.approve:{run_id}",
                    decision=True, scope=None,
                    source=f"run_id={run_id}", approver_uid=admin_uid,
                )
            except Exception:  # noqa: BLE001
                pass
        return approved

    @dbus.service.method(BUS_NAME, in_signature="sasi", out_signature="a{ss}",
                         sender_keyword="sender", connection_keyword="conn")
    def GetRunChannelEnv(self, run_id: str, names, caller_uid: int,
                         sender=None, conn=None) -> dict:
        """Return a workflow run's NON-SECRET published channel_env refs.

        The read side of the git-sign external-consume bridge: the
        root-exec daemon (uid 0) carries the run_id from a
        ``qsu --workflow-run`` handshake and folds the returned references
        (e.g. ``SSH_AUTH_SOCK``) into the child's environment before exec.

        ROOT ONLY. Unlike the admin-facing list surfaces, this is callable
        exclusively by uid 0 — the only legitimate caller is the root-exec
        daemon, which runs as root. The D-Bus policy denies it for the
        default context; this server-side check is defense-in-depth (and
        the bus name is owned by root, so admin/non-root must not reach it
        either — hence uid != 0 is rejected, not uid not in (0, ADMIN_UID)).

        ``caller_uid`` is the SO_PEERCRED uid of the ORIGINAL qsu caller,
        forwarded by root-exec (trusted because only uid 0 can reach this
        method). The engine binds the lookup to it: a run is revealed only
        when its triggering process belongs to that uid, so a run_id (which
        leaks via the WorkflowRunPending broadcast) is not a bearer token.

        Returns ONLY allowlisted, non-secret references (the engine
        intersects the requested ``names`` with its own allowlist and only
        returns a live RUNNING run's published channel). Fail-closed:
        unknown run / uid-mismatch / not-yet-published / non-allowlisted
        name → empty dict. Never returns secret material.
        """
        sender_uid, _pid, _exe, _st = self._peer_info(sender, conn)
        if sender_uid != 0:
            raise dbus.DBusException(
                f"GetRunChannelEnv restricted to root; got uid {sender_uid}",
                name=BUS_NAME + ".AccessDenied",
            )
        engine = getattr(self, "workflow_engine", None)
        if engine is None:
            return {}
        try:
            req_names = [str(n) for n in names] if names else None
        except TypeError:
            req_names = None
        try:
            bind_uid = int(caller_uid)
        except (TypeError, ValueError):
            return {}
        try:
            env = engine.get_run_channel_env(
                str(run_id), req_names, caller_uid=bind_uid)
        except Exception as e:  # noqa: BLE001 — fail closed on any engine error
            print(f"[broker] GetRunChannelEnv failed: {e!r}", flush=True)
            return {}
        return {dbus.String(str(k)): dbus.String(str(v))
                for k, v in (env or {}).items()}

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
            ) from e
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
        # Any (non-admin) uid may record its own notification rows (the
        # D-Bus policy allows this member for the default context), so
        # rate-limit per uid to bound audit-DB write amplification — a
        # buggy or hostile silo must not be able to churn the SQLite store
        # with unbounded fire-and-forget writes. Same shared limiter +
        # dedicated action key used by SnapshotBefore; the key is
        # (uid, action) so this never competes with permission accounting.
        # Fire-and-forget contract: on reject we silently drop the row
        # (log once) rather than raise — qdshell never awaits a reply.
        if not self.ratelimit.check(int(uid), "notification.posted"):
            print(f"[broker] notification rate-limited: uid={uid} "
                  f"(>{self.ratelimit.limit}/{self.ratelimit.window_s}s); "
                  f"dropping row", flush=True)
            return
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

    @dbus.service.signal(BUS_NAME, signature="")
    def ReceiversChanged(self):
        """Emitted when the set of org.qdistro.App1 receivers visible
        via :meth:`ListReceivers` may have changed inside some running
        session — a receiver registered or unregistered on a user's
        session bus while the broker (and silos) stayed up.

        Carries no payload: it's a "re-fetch" nudge, never a leak of
        which app changed in which uid. qdshell's Send-to menu / App1
        launcher subscribes and re-runs ListReceivers on it instead of
        relying solely on its slow safety-net poll.

        The trigger is each per-uid UserRelay's own
        ``LocalReceiversChanged`` signal (observed on the system bus via
        a single sender-agnostic match in :meth:`__init__`); the broker
        coalesces a burst across relays into one ReceiversChanged.
        """
        pass

    def _on_relay_receivers_changed(self, uid=None):
        """Signal handler for any per-uid UserRelay's
        LocalReceiversChanged. Arms a debounced ReceiversChanged emit so
        a burst (multiple relays, or one relay's rapid changes) collapses
        to a single re-fetch nudge. ``uid`` is logged for diagnosis only;
        the re-emitted ReceiversChanged carries no payload."""
        print(f"[broker] relay receivers changed (uid={uid}); "
              f"scheduling ReceiversChanged", flush=True)
        if self._receivers_changed_timer:
            # A burst is already pending — coalesce onto the armed timer.
            return
        self._receivers_changed_timer = GLib.timeout_add(
            self.RECEIVERS_CHANGED_DEBOUNCE_MS,
            self._emit_receivers_changed)

    def _emit_receivers_changed(self) -> bool:
        """Debounce-timer callback: emit one ReceiversChanged and clear
        the pending-timer id. Returns False so GLib drops the one-shot
        timeout."""
        self._receivers_changed_timer = 0
        self.ReceiversChanged()
        return False

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
                name=BUS_NAME + ".SnapperUnavailable") from e
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
                name=BUS_NAME + ".SnapperUnavailable") from e
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
                name=BUS_NAME + ".SnapshotFailed") from e
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

    @dbus.service.signal(BUS_NAME, signature="ss")
    def WorkflowRunPending(self, run_id: str, workflow_name: str):
        """Emitted when a non-auto-run workflow fires and parks a run for
        admin approval (F3). The admin Workflows tab refreshes its run
        list so the new PENDING entry appears; an admin then calls
        ApproveWorkflowRun(run_id). Carries only identifiers, never any
        secret or trigger payload."""
        pass


def main():
    # Cheap, side-effect-free health smoke. The broker has no argparse
    # (its only real invocation is the systemd ExecStart with no args),
    # so handle --version with an explicit guard BEFORE touching the
    # system bus — the smoke gate must not require root or a live bus.
    if "--version" in sys.argv[1:]:
        print("qdistro-admin-broker (qdistro)")
        return
    # Fail closed before serving if the host is misconfigured (no admin/uid-1000
    # account). Deferred here (not import) so unit tests can import this module.
    _require_admin_account()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    _name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)
    broker = Broker(bus)
    print(f"[qdistro-admin-broker] listening on {BUS_NAME} {OBJ_PATH}", flush=True)
    try:
        GLib.MainLoop().run()
    finally:
        # Best-effort scrub of any outstanding workflow secrets on a clean
        # main-loop exit (startup reap covers hard crashes).
        engine = getattr(broker, "workflow_engine", None)
        if engine is not None:
            try:
                engine.shutdown()
            except Exception as e:  # noqa: BLE001
                print(f"[broker] workflow engine shutdown failed: {e!r}",
                      flush=True)
        # Tear down the zero-coordination sign-agent relay (if started) so a
        # clean exit does not leave its listening socket bound or its socket
        # file on disk. build_relay_registrar() hands us the relay handle
        # precisely so we can stop() it here.
        relay = getattr(broker, "_sign_agent_relay", None)
        if relay is not None:
            try:
                relay.stop()
            except Exception as e:  # noqa: BLE001
                print(f"[broker] sign-agent relay stop failed: {e!r}",
                      flush=True)


if __name__ == "__main__":
    main()
