#!/usr/bin/env python3
"""qdistro-session-manager — admin-controlled user-silo lifecycle.

System-bus D-Bus service that owns user-silo state: create, delete,
start, stop, freeze, resume. A "silo" is a Linux uid + per-silo home
subvolume + a cgroup-v2 scope under /sys/fs/cgroup/qdistro-silos/.

P02 scope (see plan2/tasks/P02-session-manager.md):
  - Methods: CreateSilo, DeleteSilo, StartSilo, StopSilo, FreezeSilo,
    ResumeSilo, ListSilos.
  - Signal: SiloChanged(name, state).
  - State machine: Created → Active → Frozen → Active → Stopped →
    Deleted; illegal transitions raise BadState.
  - Persistence: /etc/qdistro/silos.yaml regenerated on every state
    change; autostart=true rows boot to Active on daemon startup.
  - Authz: caller uid must equal ADMIN_UID at the D-Bus method and
    is re-checked in-process before any privileged operation.

The module is import-safe on hosts without dbus-python so unit tests
can exercise the pure state machine. The class _SiloStore + Silo
dataclass + _STATE_TRANSITIONS table do the load-bearing work; the
dbus.service.Object subclass is a thin shell over them.
"""
from __future__ import annotations

import json
import logging
import os
import pwd
import re as _re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Per-silo netns egress backend (interim per-silo VPN; todo/fable-networking
# task 3). Pure module: the side-effecting ip/wg/nft/veth ops live on _SystemOps
# below; this module owns only the policy + the kill-switch-by-construction
# sequence. Import-cycle-free (it imports nothing from this module).
import qdistro_silo_egress as _egress
from qdistro_silo_egress import (
    EgressBackend, EgressPolicy, KeyUnavailable, TunnelConfig, validate_egress,
)

BUS_NAME = "org.qdistro.SessionManager1"
OBJ_PATH = "/org/qdistro/SessionManager1"
try:
    ADMIN_UID = pwd.getpwnam(
        os.environ.get("QDISTRO_ADMIN_USER", "admin")).pw_uid
except KeyError:
    ADMIN_UID = 1000

# Where per-silo broker state lives. The dir for each silo is owned
# by the silo's uid and is mode 0700 so other uids cannot peek.
SILOS_STATE_DIR = Path("/var/lib/qdistro/silos")
# Persistence: silos.yaml is admin-editable; the session manager
# regenerates it on every state change. Schema documented in the
# header comment of the file the daemon writes.
SILOS_CONFIG_PATH = Path("/etc/qdistro/silos.yaml")
# Durable forensic record of every privileged lifecycle mutation
# (create/delete/start/stop/freeze/resume), including refusals. Lives
# alongside the other qdistro audit DBs (pwd_audit.sqlite, the broker
# audit db) so an admin investigating "who started/stopped/deleted
# which silo and when" has one obvious place to look. SQLite, append-
# only, 0600.
SILOS_AUDIT_PATH = Path("/var/lib/qdistro/audit/session_manager_audit.sqlite")
# cgroup-v2 hierarchy root for silo scopes. One subdir per silo;
# cgroup.freeze controls Freeze/Resume.
CGROUP_ROOT = Path("/sys/fs/cgroup/qdistro-silos")
# Per-silo launcher unit. The placeholder mirrors qdwin-session-launcher;
# whichever launcher the bake ships is fine — the session manager only
# needs the unit name shape so it can `systemctl start` it.
SILO_LAUNCHER_FMT = "qdshell-session-{name}@{uid}.service"
# Tier-2 templated silos launch through their own unit, which runs
# spawn-tier2 as admin (fableplan2 task 04). The session manager runs as
# root; the unit boundary is where privileges drop to admin (rootless
# podman must run as admin). One %i = the silo name.
TIER2_SILO_LAUNCHER_FMT = "qdistro-tier2-silo@{name}.service"
# The rootless container spawn-tier2 names for a tier-2 silo (mirrors the
# daemon-exported QD_CONTAINER). Used to fail-closed-verify a stop actually
# tore the container down (the unit going inactive is not sufficient: a
# rootless container can survive its supervisor).
TIER2_CONTAINER_FMT = "qdistro-silo-{name}"

# Silo kinds (fableplan2 task 04). tier3-user is today's implicit default
# (a real Linux user, per-uid home/cgroup, tier-3 session launcher);
# tier2-template is a templated podman silo whose launch-owner is admin and
# whose state is the binding's state_path (created by promote, not useradd).
KIND_TIER3_USER = "tier3-user"
KIND_TIER2_TEMPLATE = "tier2-template"
SILO_KINDS = (KIND_TIER3_USER, KIND_TIER2_TEMPLATE)
# The launch-owner uid a tier2-template row carries: admin, where rootless
# podman runs. Not a fresh silo uid (no useradd/home/cgroup semantics).
ADMIN_UID = 1000
# Network modes a tier2-template launch may request (maps to TIER2_NETWORK).
SILO_NETWORK_MODES = ("none", "slirp4netns")
# Per-silo launch env the daemon writes for the tier-2 launcher unit to read
# (the unit drops privileges to admin and runs spawn-tier2). Under /run so it
# is tmpfs-backed and gone on reboot — the daemon rewrites it on each start.
TIER2_LAUNCH_ENV_DIR = Path("/run/qdistro/silo-launch")

# Per-silo network-namespace egress (todo/fable-networking task 3), applied only
# to tier3-user silos that carry an explicit `egress` policy. A silo with no
# egress field keeps today's legacy host networking (no netns) for backward
# compatibility; `none`/`direct`/`wg:<name>` opt it into the netns contract.
# NETNS_RUN_DIR is the iproute2 convention (`ip netns` bind-targets here);
# ETC_NETNS holds the per-netns resolv.conf that `ip netns exec` bind-mounts
# over /etc/resolv.conf. WG_CONFIG_DIR holds non-secret per-tunnel config
# (public key/endpoint/address/dns); the private key lives in qdistro-pwd.
NETNS_RUN_DIR = Path("/run/netns")
ETC_NETNS = Path("/etc/netns")
WG_CONFIG_DIR = Path("/etc/qdistro/wg")
# Per-silo `direct`-egress resolver: one dnsmasq pinned to each direct silo's
# veth host address, pid-tracked here so teardown can stop exactly that
# instance. tmpfs-backed (/run) so it is gone on reboot — a fresh start
# re-spawns it (Opt 3-A follow-up).
SILO_DNS_RUN_DIR = Path("/run/qdistro/silo-dns")

# Reserved uid range. Admin = 1000; silos start at 2000 so the admin
# account and the few system-fixed uids in qdistro (audisp = 990 etc.)
# never collide.
SILO_UID_MIN = 2000
SILO_UID_MAX = 60000

# Default grace seconds for StopSilo (SIGTERM → wait → SIGKILL).
DEFAULT_STOP_GRACE_S = 5

log = logging.getLogger("qdistro_session_manager")


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

# The legal target states per current state. Anything not in the
# table is rejected by SessionManager with BadState. Persistence
# lives in silos.yaml; cgroup + processes follow the machine, not
# the other way around.
class State:
    CREATED = "Created"
    ACTIVE = "Active"
    FROZEN = "Frozen"
    STOPPING = "Stopping"
    STOPPED = "Stopped"
    DELETING = "Deleting"

    ALL = frozenset((CREATED, ACTIVE, FROZEN, STOPPING, STOPPED, DELETING))


_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    State.CREATED:  frozenset((State.ACTIVE, State.DELETING)),
    State.ACTIVE:   frozenset((State.FROZEN, State.STOPPING, State.STOPPED)),
    State.FROZEN:   frozenset((State.ACTIVE, State.STOPPING, State.STOPPED)),
    State.STOPPING: frozenset((State.STOPPED,)),
    State.STOPPED:  frozenset((State.ACTIVE, State.DELETING)),
    State.DELETING: frozenset(),
}


class SessionError(Exception):
    """Base for session-manager-raised errors. The dbus shim maps
    each subclass to a typed DBusException with a stable error name
    (org.qdistro.SessionManager1.<Class>); unit tests assert on the
    exception class directly."""

    dbus_name = "Generic"


class UnknownSilo(SessionError):
    dbus_name = "UnknownSilo"


class SiloExists(SessionError):
    dbus_name = "SiloExists"


class SiloBusy(SessionError):
    dbus_name = "SiloBusy"


class BadState(SessionError):
    dbus_name = "BadState"


class BadArgument(SessionError):
    dbus_name = "BadArgument"


class NotAuthorized(SessionError):
    dbus_name = "NotAuthorized"


@dataclass
class Silo:
    name: str
    uid: int
    state: str = State.CREATED
    autostart: bool = False
    # Wall-clock seconds; informational only.
    created_at: int = 0
    last_change: int = 0
    # fableplan2 task 04: a tier3-user silo is a real Linux user (today's
    # default); a tier2-template silo is a templated podman silo launched via
    # spawn-tier2. The launch stanza is empty for tier3-user and carries
    # {workload, argv, network, template_silo} for tier2-template.
    kind: str = KIND_TIER3_USER
    launch: dict = field(default_factory=dict)
    # Per-silo netns egress policy (todo/fable-networking task 3). None means
    # the legacy un-managed state (no netns, host networking); a string
    # ("none"|"direct"|"wg:<name>") opts the silo into the netns contract with
    # "none" = default-deny. Only meaningful for tier3-user silos.
    egress: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "uid": int(self.uid),
            "state": self.state,
            "autostart": bool(self.autostart),
            "created_at": int(self.created_at),
            "last_change": int(self.last_change),
            "kind": self.kind,
            "launch": dict(self.launch),
            "egress": self.egress,
        }


# ---------------------------------------------------------------------------
# Name / uid validation
# ---------------------------------------------------------------------------

# Linux-y POSIX user names: lowercase alpha + digit + underscore +
# dash, must start with alpha or _, len <= 32. Rejecting anything
# the system useradd wouldn't accept keeps the error surface narrow.
_VALID_NAME_RE = _re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def validate_name(name: str) -> str:
    if not isinstance(name, str):
        raise BadArgument(f"name must be a string, got {type(name).__name__}")
    if not _VALID_NAME_RE.match(name):
        raise BadArgument(
            f"name {name!r} is not a valid silo name (lowercase + "
            f"alnum/underscore/dash, ≤32 chars, must start with a "
            f"letter or underscore)")
    # Reserved namespace.
    if name in ("admin", "root", "qdistro"):
        raise BadArgument(f"name {name!r} is reserved")
    return name


def validate_uid(uid: int) -> int:
    try:
        uid_i = int(uid)
    except (TypeError, ValueError):
        raise BadArgument(f"uid must be an integer, got {uid!r}")
    if uid_i < SILO_UID_MIN or uid_i > SILO_UID_MAX:
        raise BadArgument(
            f"uid must be in [{SILO_UID_MIN}, {SILO_UID_MAX}], got {uid_i}")
    return uid_i


def validate_kind(kind: str) -> str:
    if kind not in SILO_KINDS:
        raise BadArgument(f"kind must be one of {SILO_KINDS}, got {kind!r}")
    return kind


def _validate_egress_field(value: object) -> str | None:
    """Validate/normalise the silo's egress policy at the session-manager
    boundary. None = legacy un-managed (no netns). Translates the egress
    module's EgressError into our BadArgument so callers/loaders catch one
    type."""
    try:
        return validate_egress(value)
    except _egress.EgressError as e:
        raise BadArgument(str(e)) from e


def validate_silo_uid(uid: int, kind: str) -> int:
    """A tier3-user silo's uid is a fresh silo uid (2000..60000); a
    tier2-template silo's uid is the launch-owner admin uid (rootless podman
    runs as admin). A fake/silo uid on a tier2-template row, or admin's uid on
    a tier3-user row, is rejected — the loader must not smuggle the wrong
    privilege semantics in."""
    if kind == KIND_TIER2_TEMPLATE:
        if int(uid) != ADMIN_UID:
            raise BadArgument(
                f"tier2-template silo uid must be the admin launch-owner "
                f"({ADMIN_UID}), got {uid}")
        return ADMIN_UID
    return validate_uid(uid)


# Tier-2 launch-stanza field names cross into TIER2_* env at spawn time and
# the launcher unit reads them, so they are untrusted input: the workload
# selects the seccomp profile and (legacy) image tag, argv is the app command
# line, network maps to TIER2_NETWORK, template_silo to TIER2_SILO.
_SAFE_TOKEN_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/=+-]*$")


def validate_launch(kind: str, launch: object) -> dict[str, Any]:
    """Validate (and normalise) a silo's launch stanza for its kind.

    tier3-user carries no launch stanza (the tier-3 session launcher owns the
    payload). tier2-template requires workload + template_silo + network, with
    an optional argv; every value is constrained because it crosses into the
    spawn-tier2 env / launcher unit."""
    if kind == KIND_TIER3_USER:
        if launch:
            raise BadArgument(
                "tier3-user silos carry no launch stanza (the tier-3 session "
                "launcher owns the payload)")
        return {}
    if not isinstance(launch, dict):
        raise BadArgument("tier2-template launch stanza must be a table")

    def _tok(key: str, *, required: bool = True, default: str = "") -> str:
        v = launch.get(key, default)
        if not v:
            if required:
                raise BadArgument(f"tier2-template launch.{key} is required")
            return default
        if not isinstance(v, str) or not _SAFE_TOKEN_RE.match(v) or ".." in v:
            raise BadArgument(f"tier2-template launch.{key} is unsafe: {v!r}")
        return v

    workload = _tok("workload")
    template_silo = _tok("template_silo")
    network = launch.get("network", "none")
    if network not in SILO_NETWORK_MODES:
        raise BadArgument(
            f"tier2-template launch.network must be one of "
            f"{SILO_NETWORK_MODES}, got {network!r}")
    argv = launch.get("argv", [])
    # PyYAML parses the rendered `argv: [...]` as a real list; the tolerant
    # fallback parser yields the JSON-array text verbatim — normalise both.
    if isinstance(argv, str):
        try:
            argv = json.loads(argv)
        except json.JSONDecodeError as e:
            raise BadArgument(f"tier2-template launch.argv not valid JSON: {e}")
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise BadArgument("tier2-template launch.argv must be a list of strings")
    if any(("\n" in a or "\r" in a) for a in argv):
        raise BadArgument("tier2-template launch.argv entries must be single-line")
    return {
        "workload": workload,
        "template_silo": template_silo,
        "network": network,
        "argv": list(argv),
    }


# ---------------------------------------------------------------------------
# System-side adapter
# ---------------------------------------------------------------------------

def _nft_benign(stderr: str) -> bool:
    """True if an nft element add/delete error is a benign no-op for us:
    adding an element that already exists, or deleting one that's absent."""
    s = (stderr or "").lower()
    return ("file exists" in s            # add-existing
            or "no such file" in s        # delete-missing
            or "does not exist" in s)


class _SystemOps:
    """Real implementation of the side-effecting ops (useradd,
    userdel, cgroup writes, systemctl). Tests substitute a fake
    that records calls and pretends success.
    """

    def user_exists(self, name: str) -> bool:
        try:
            pwd.getpwnam(name)
            return True
        except KeyError:
            return False

    def uid_exists(self, uid: int) -> bool:
        try:
            pwd.getpwuid(int(uid))
            return True
        except KeyError:
            return False

    def useradd(self, name: str, uid: int) -> None:
        # -m creates the home dir under /home/<name>. -U creates a
        # matching group with the same name. -s bash gives the silo
        # a real shell; the launcher service replaces $SHELL with
        # qdshell when it spawns.
        subprocess.run(
            ["useradd", "-m", "-u", str(int(uid)), "-U",
             "-s", "/bin/bash", str(name)],
            check=True)
        # Reload dbus so policy files referencing `<policy user="<name>">`
        # pick up the freshly-created user. Without this, dbus rejects
        # the silo's qdistro-user-relay@<uid>.service from claiming
        # org.qdistro.UserRelay.uid<N> with "Request to own name refused
        # by policy" — the policy file (org.qdistro.UserRelay.conf) was
        # parsed at bootstrap before the silo user existed.
        subprocess.run(
            ["systemctl", "reload", "dbus.service"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Replace the plain /home/<name> dir useradd created with a
        # fresh btrfs subvolume so each silo has its own snapshot
        # boundary + per-quota target. Falls through (warning only)
        # on non-btrfs hosts so dev VMs without btrfs still work.
        self._convert_home_to_subvolume(name, uid)

    def _convert_home_to_subvolume(self, name: str, uid: int) -> None:
        home = Path("/home") / name
        try:
            # Are we on btrfs at all? `btrfs filesystem df` exits 0 on
            # btrfs, non-zero elsewhere. Cheap probe.
            probe = subprocess.run(
                ["btrfs", "filesystem", "df", str(home)],
                check=False, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            if probe.returncode != 0:
                log.warning(
                    "btrfs not available at %s; leaving plain dir "
                    "(per-silo subvolume isolation skipped)", home)
                return
            # Save what useradd populated from /etc/skel.
            skel_backup = Path("/var/lib/qdistro/silos") / name / ".skel-backup"
            skel_backup.parent.mkdir(parents=True, exist_ok=True)
            if skel_backup.exists():
                shutil.rmtree(skel_backup)
            shutil.copytree(home, skel_backup, symlinks=True)
            # Replace dir with subvolume.
            shutil.rmtree(home)
            subprocess.run(
                ["btrfs", "subvolume", "create", str(home)],
                check=True)
            # Restore the skeleton contents.
            for child in skel_backup.iterdir():
                dst = home / child.name
                if child.is_dir():
                    shutil.copytree(child, dst, symlinks=True)
                else:
                    shutil.copy2(child, dst, follow_symlinks=False)
            shutil.rmtree(skel_backup)
            os.chown(home, int(uid), int(uid))
            home.chmod(0o700)
            # Recursive chown for the restored files.
            for root, dirs, files in os.walk(home):
                for d in dirs:
                    os.chown(Path(root) / d, int(uid), int(uid))
                for f in files:
                    try:
                        os.chown(Path(root) / f, int(uid), int(uid),
                                 follow_symlinks=False)
                    except OSError:
                        pass
        except (subprocess.CalledProcessError, FileNotFoundError,
                OSError) as e:
            log.warning("btrfs subvolume conversion for %s failed: %s "
                        "(home left as plain dir)", home, e)

    def userdel(self, name: str) -> None:
        # -r removes home dir + mail spool. -f forces removal even
        # if the user is logged in — we already killed everything
        # in the silo's cgroup before getting here.
        subprocess.run(
            ["userdel", "-r", "-f", str(name)],
            check=True)

    def make_state_dir(self, name: str, uid: int) -> Path:
        d = SILOS_STATE_DIR / name
        d.mkdir(parents=True, exist_ok=True)
        os.chown(d, int(uid), int(uid))
        d.chmod(0o700)
        return d

    def remove_state_dir(self, name: str) -> None:
        d = SILOS_STATE_DIR / name
        if d.exists():
            shutil.rmtree(d)

    def cgroup_create(self, name: str) -> Path:
        p = CGROUP_ROOT / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def cgroup_remove(self, name: str) -> None:
        p = CGROUP_ROOT / name
        if p.exists():
            # cgroup dirs are removed with rmdir (not rmtree) — the
            # kernel rejects rmtree on the synthetic cgroup files.
            # If rmdir fails (EBUSY: live tasks still in cgroup.procs)
            # we surface the error so the caller can decide — silently
            # swallowing leaks the cgroup directory.
            p.rmdir()

    def cgroup_freeze(self, name: str, frozen: bool) -> None:
        p = CGROUP_ROOT / name / "cgroup.freeze"
        p.write_text("1\n" if frozen else "0\n")

    def cgroup_pids(self, name: str) -> list[int]:
        p = CGROUP_ROOT / name / "cgroup.procs"
        if not p.exists():
            return []
        out = []
        for ln in p.read_text().splitlines():
            ln = ln.strip()
            if ln:
                out.append(int(ln))
        return out

    def cgroup_is_populated(self, name: str) -> bool:
        # cgroup.events is "populated 0|1\nfrozen 0|1\n" on cgroup v2.
        ev = CGROUP_ROOT / name / "cgroup.events"
        if not ev.exists():
            return False
        for ln in ev.read_text().splitlines():
            if ln.startswith("populated "):
                return ln.endswith(" 1")
        return False

    def write_launch_env(self, name: str, content: str) -> Path:
        TIER2_LAUNCH_ENV_DIR.mkdir(parents=True, exist_ok=True)
        p = TIER2_LAUNCH_ENV_DIR / f"{name}.env"
        tmp = p.with_suffix(".env.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content.encode())
            os.fdatasync(fd)
            # The launcher unit drops to admin (the rootless-podman owner) and
            # reads this file; the daemon runs as root, so a root-owned 0600 file
            # is unreadable by the launcher (the launch fails with "no launch
            # env"). Hand it to admin so the unit it is written FOR can read it.
            # Leave the group unchanged (-1): the second arg is a GID, not a
            # second UID, and admin's primary gid is not guaranteed to equal
            # ADMIN_UID. Best-effort: only root can chown, and the daemon is root
            # in prod.
            try:
                os.fchown(fd, ADMIN_UID, -1)
            except OSError:
                pass
        finally:
            os.close(fd)
        os.replace(tmp, p)
        return p

    def remove_launch_env(self, name: str) -> None:
        p = TIER2_LAUNCH_ENV_DIR / f"{name}.env"
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    # ---- per-silo netns egress (todo/fable-networking task 3) -------------
    #
    # Thin side-effect wrappers over `ip`/`wg`/`nft`/sysctl, mirroring the
    # cgroup_* methods: the pure EgressBackend (qdistro_silo_egress) decides the
    # sequence; these just run it. A falsy `ns` means the init (host) netns.
    # Not unit-tested directly (the fake records calls); exercised in the VM
    # bats probe (task 3 Phase E).

    _NFT_TABLE = "qdistro_egress"

    def _ip(self, ns, *args, check=True) -> None:
        cmd = ["ip"]
        if ns:
            cmd += ["-n", str(ns)]
        cmd += [str(a) for a in args]
        subprocess.run(cmd, check=check)

    def netns_exists(self, ns: str) -> bool:
        return (NETNS_RUN_DIR / str(ns)).exists()

    def netns_create(self, ns: str) -> None:
        # Idempotent: `ip netns add` errors if the name exists.
        if not self.netns_exists(ns):
            subprocess.run(["ip", "netns", "add", str(ns)], check=True)

    def netns_remove(self, ns: str) -> None:
        # Deleting the netns destroys every interface still inside it. Best
        # effort: a missing netns is not an error.
        subprocess.run(["ip", "netns", "del", str(ns)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def link_up(self, ns, ifname) -> None:
        self._ip(ns, "link", "set", ifname, "up")

    def link_del(self, ns, ifname) -> None:
        # Idempotent teardown: a missing device must not raise.
        self._ip(ns, "link", "del", ifname, check=False)

    def link_set_netns(self, ifname, ns) -> None:
        # The device is in the init netns; move it into `ns`.
        subprocess.run(["ip", "link", "set", str(ifname), "netns", str(ns)],
                       check=True)

    def addr_add(self, ns, ifname, address) -> None:
        # `replace` (not `add`) so reattach() on a link-up is idempotent — a
        # re-add of an existing address fails EEXIST (codex #6).
        self._ip(ns, "addr", "replace", address, "dev", ifname)

    def route_add_default_dev(self, ns, ifname) -> None:
        # `replace` so reattach is idempotent (a wg link bounce flushes the
        # route; re-adding an existing one would fail).
        self._ip(ns, "route", "replace", "default", "dev", ifname)

    def route_add_default_via(self, ns, gateway) -> None:
        self._ip(ns, "route", "replace", "default", "via", gateway)

    def ipv6_disable(self, ns, ifname) -> None:
        # Stop SLAAC handing a direct-egress silo a v6 path around the NAT.
        argv = ["sysctl", "-q", f"net.ipv6.conf.{ifname}.disable_ipv6=1"]
        if ns:
            argv = ["ip", "netns", "exec", str(ns)] + argv
        subprocess.run(argv, check=False)

    def wg_add_dev(self, ifname) -> None:
        # Born in the init netns so WireGuard binds its encrypted UDP socket
        # here; moved into the silo netns afterwards (kill-switch by
        # construction — the silo netns then holds only this device + lo).
        subprocess.run(["ip", "link", "add", str(ifname), "type", "wireguard"],
                       check=True)

    def wg_configure(self, ifname, *, private_key, peer_public_key, endpoint,
                     allowed_ips, keepalive) -> None:
        # Pass the private key via a pipe + /dev/fd/N so it never lands on disk
        # (least of all a silo home). The read fd is inherited by `wg`.
        r, w = os.pipe()
        try:
            os.write(w, (str(private_key).strip() + "\n").encode())
            os.close(w)
            w = -1
            cmd = ["wg", "set", str(ifname),
                   "private-key", f"/dev/fd/{r}",
                   "peer", str(peer_public_key),
                   "allowed-ips", str(allowed_ips),
                   "endpoint", str(endpoint)]
            if keepalive:
                cmd += ["persistent-keepalive", str(int(keepalive))]
            subprocess.run(cmd, check=True, pass_fds=(r,))
        finally:
            if w != -1:
                os.close(w)
            os.close(r)

    def veth_create(self, host_if, peer_if) -> None:
        subprocess.run(["ip", "link", "add", str(host_if), "type", "veth",
                        "peer", "name", str(peer_if)], check=True)

    def _nft_ensure_table(self) -> None:
        # Self-healing + fail-closed (codex #1). A dedicated table so per-silo
        # changes never touch other firewall state; two sets drive the rules and
        # per-silo apply/teardown is just an element add/del. We VERIFY the
        # backstop drop rule + masquerade rule are actually present (not merely
        # that the table exists), and rebuild the scaffold if the table is
        # missing or partial — a stale/partial table would otherwise silently
        # disable the backstop. Raises on failure so the caller fails closed.
        listing = subprocess.run(
            ["nft", "list", "table", "inet", self._NFT_TABLE],
            capture_output=True, text=True)
        out = listing.stdout
        if (listing.returncode == 0
                and "skuid @blocked_uids drop" in out            # out chain rule
                and "hook input" in out                          # host-protect
                and "@nat_subnets ip daddr" in out               # forward drop rule
                and "masquerade" in out):                        # post rule
            return                               # healthy (rules present, not
            #                                      merely the chains)
        # (Re)build. `add table`/`add set` are idempotent and PRESERVE existing
        # set elements, so other silos' backstop/nat entries survive a rebuild;
        # we then delete+re-add each chain so its rule is present exactly once.
        base = (
            f"add table inet {self._NFT_TABLE}\n"
            f"add set inet {self._NFT_TABLE} blocked_uids {{ type uid; }}\n"
            f"add set inet {self._NFT_TABLE} nat_subnets "
            f"{{ type ipv4_addr; flags interval; }}\n")
        r = subprocess.run(["nft", "-f", "-"], input=base,
                           capture_output=True, text=True)
        # `nft add` of an existing table/set is idempotent, but tolerate a
        # benign "exists" just in case a partial table is present; fail closed
        # on anything else (a broken scaffold must not silently disable the
        # backstop).
        if r.returncode != 0 and not _nft_benign(r.stderr):
            raise RuntimeError(
                f"nft egress scaffold (table/sets) failed: {r.stderr.strip()}")
        # NB: the forward-hook chain is named `forward`, not `fwd` — `fwd` is a
        # reserved nft keyword (the netdev fwd verdict) and fails to parse as a
        # chain identifier (nft v1.1.6).
        for chain in ("out", "in", "forward", "post"):
            subprocess.run(["nft", "delete", "chain", "inet", self._NFT_TABLE,
                            chain], check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        # The set of destinations a `direct` silo must NOT reach: every other
        # silo's /30 (all from 10.128.0.0/9 ⊂ 10/8), the host's own LAN, plus
        # bogons that are local/management surfaces — link-local 169.254/16
        # (incl. the 169.254.169.254 cloud metadata endpoint) and CGNAT
        # 100.64/10. IPv6 needs no rule here: _apply_direct disables IPv6 on the
        # silo veth and we never enable IPv6 forwarding, so a direct silo has no
        # IPv6 path at all. Multicast/loopback are not routable off-host.
        _BOGON = ("{ 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, "
                  "169.254.0.0/16, 100.64.0.0/10 }")
        # `in`:  protect the HOST itself. Forwarding only covers transit
        #        traffic; a silo's packet to a host-local address (the veth
        #        gateway beyond :53, the host LAN IP, sshd, any 0.0.0.0-bound
        #        daemon) is local delivery via the INPUT hook, NOT forward. So
        #        drop silo->host except the one resolver port and return
        #        traffic. Keyed on @nat_subnets, so wg/legacy silos and normal
        #        host clients are untouched (policy accept).
        # `forward`: silo-isolation. Allow established/return + silo->public WAN
        #        (falls through to accept), DROP silo->bogon (silo<->silo +
        #        silo<->LAN) AND anyone->silo new connections (a LAN host that
        #        adds a route to 10.128/9 cannot initiate into a silo). The
        #        silo's own dnsmasq (veth host .1) is local delivery, not
        #        forward, so it is not caught here. A base chain with `policy
        #        accept` cannot weaken other tables (nft requires ALL base
        #        chains to accept).
        chains = (
            f"add chain inet {self._NFT_TABLE} out "
            f"{{ type filter hook output priority 0; }}\n"
            f"add rule inet {self._NFT_TABLE} out meta skuid @blocked_uids drop\n"
            f"add chain inet {self._NFT_TABLE} in "
            f"{{ type filter hook input priority filter; policy accept; }}\n"
            f"add rule inet {self._NFT_TABLE} in ct state established,related "
            f"accept\n"
            f"add rule inet {self._NFT_TABLE} in ip saddr @nat_subnets "
            f"udp dport 53 accept\n"
            f"add rule inet {self._NFT_TABLE} in ip saddr @nat_subnets "
            f"tcp dport 53 accept\n"
            f"add rule inet {self._NFT_TABLE} in ip saddr @nat_subnets drop\n"
            f"add chain inet {self._NFT_TABLE} forward "
            f"{{ type filter hook forward priority filter; policy accept; }}\n"
            f"add rule inet {self._NFT_TABLE} forward ct state established,related "
            f"accept\n"
            f"add rule inet {self._NFT_TABLE} forward ip saddr @nat_subnets "
            f"ip daddr {_BOGON} drop\n"
            f"add rule inet {self._NFT_TABLE} forward ip daddr @nat_subnets drop\n"
            f"add chain inet {self._NFT_TABLE} post "
            f"{{ type nat hook postrouting priority srcnat; }}\n"
            f"add rule inet {self._NFT_TABLE} post "
            f"ip saddr @nat_subnets masquerade\n")
        r = subprocess.run(["nft", "-f", "-"], input=chains,
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(
                f"nft egress backstop rule install failed: {r.stderr.strip()}")

    def _nft_table_present(self) -> bool:
        return subprocess.run(
            ["nft", "list", "table", "inet", self._NFT_TABLE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    def nft_skuid_drop(self, uid, enable) -> None:
        # Defense-in-depth backstop: drop traffic from a silo uid that runs in
        # the INIT netns (a process that bypassed `ip netns exec`). The in-netns
        # route topology is the primary kill-switch; this catches stragglers.
        if enable:
            self._nft_ensure_table()
        elif not self._nft_table_present():
            # Removal never creates the table: a pristine legacy silo (no table)
            # has nothing to clear, so this stays zero-cost (codex #3).
            return
        verb = "add" if enable else "delete"
        r = subprocess.run(
            ["nft", verb, "element", "inet", self._NFT_TABLE, "blocked_uids",
             "{", str(int(uid)), "}"],
            capture_output=True, text=True)
        if r.returncode != 0 and not _nft_benign(r.stderr):
            msg = f"nft backstop {verb} uid={uid} failed: {r.stderr.strip()}"
            if enable:
                # Fail closed: never run a silo whose containment backstop could
                # not be installed (codex #1/NEW-2).
                raise RuntimeError(msg)
            log.warning("%s", msg)             # failed removal is fail-safe

    def nat_masquerade(self, subnet, enable) -> None:
        if enable:
            self._nft_ensure_table()
        elif not self._nft_table_present():
            return
        verb = "add" if enable else "delete"
        r = subprocess.run(
            ["nft", verb, "element", "inet", self._NFT_TABLE, "nat_subnets",
             "{", str(subnet), "}"],
            capture_output=True, text=True)
        if r.returncode != 0 and not _nft_benign(r.stderr):
            msg = f"nft nat {verb} subnet={subnet} failed: {r.stderr.strip()}"
            if enable:
                raise RuntimeError(msg)
            log.warning("%s", msg)

    def enable_ip_forward(self) -> None:
        # Host-global IPv4 forwarding so a `direct` silo's routed veth can reach
        # the WAN. Idempotent; left enabled once set (a re-disable would break
        # other direct silos, and `=1` is inert on a workstation that does no
        # other routing — the silo-isolation it would expose is closed by the
        # nft `forward` chain). Fail-closed: if the sysctl write fails the direct
        # apply raises and the silo comes up dark.
        subprocess.run(["sysctl", "-q", "net.ipv4.ip_forward=1"], check=True)

    def _dns_pidfile(self, ns) -> Path:
        return SILO_DNS_RUN_DIR / f"{ns}.pid"

    def dns_start(self, ns, host_if, host_ip) -> None:
        # Per-silo `direct` resolver: a dnsmasq bound to ONLY the veth host
        # address, forwarding to the host's configured upstreams
        # (/etc/resolv.conf, read at start). Runs in the init netns (where the
        # veth host end lives).
        #
        # `--listen-address` + `--bind-interfaces` binds EXACTLY host_ip and
        # nothing else. We deliberately do NOT pass `--interface=`: with
        # `--interface` dnsmasq auto-adds the loopback interface and binds
        # 127.0.0.1:53 too, so a SECOND direct silo's dnsmasq would collide on
        # loopback and fail to start (and it would also contend with any host
        # 127.0.0.1:53 service). listen-address alone never auto-adds loopback.
        # conf-file=/dev/null ignores host dnsmasq.d drop-ins so a silo resolver
        # can't inherit DHCP or extra listen addresses. Raises (-> dark) if
        # dnsmasq is absent or cannot bind — a direct silo with no working
        # resolver fails closed rather than coming up half-built.
        SILO_DNS_RUN_DIR.mkdir(parents=True, exist_ok=True)
        self.dns_stop(ns)                          # waits for the old pid to exit
        subprocess.run(
            ["dnsmasq",
             "--conf-file=/dev/null",
             f"--pid-file={self._dns_pidfile(ns)}",
             f"--listen-address={host_ip}",
             "--bind-interfaces",
             "--except-interface=lo",
             "--no-hosts",
             "--no-dhcp-interface=*",
             "--cache-size=150"],
            check=True)

    def dns_stop(self, ns) -> None:
        # Idempotent: a missing/garbage pidfile, an already-dead pid, or an
        # EPERM are all non-fatal — teardown must never raise on a best-effort
        # resolver stop. SYNCHRONOUS: we wait for the old dnsmasq to actually
        # exit before returning so a quick dns_start re-bind of the same address
        # cannot race the dying instance (EADDRINUSE under --bind-interfaces).
        pidfile = self._dns_pidfile(ns)
        try:
            pid = int(pidfile.read_text().strip())
        except (FileNotFoundError, ValueError):
            return
        # Only signal if the pid is genuinely our per-silo dnsmasq (pidfile path
        # on its argv) — guards against a reused pid after a daemon crash.
        cmdline = b""
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read()
        except OSError:
            pid = None  # process gone; just drop the stale pidfile
        if pid is not None and (
                b"dnsmasq" in cmdline and str(pidfile).encode() in cmdline):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pid = None
            else:
                # Bounded wait for exit (the dnsmasq daemonized, so it is not our
                # child and cannot be waitpid'd — poll instead).
                for _ in range(20):                 # ~1s total
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    except PermissionError:
                        break
                    time.sleep(0.05)
                else:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
        try:
            pidfile.unlink()
        except FileNotFoundError:
            pass

    def start_link_watcher(self, ns, ifname, on_up):
        """Spawn a daemon thread running `ip netns exec <ns> ip monitor link`
        and invoke `on_up()` each time `ifname` reports an admin-UP event — the
        link-up event source for EgressBackend.reattach (Probe 2 finding 1: a wg
        link bounce flushes the default route but keeps the address). Returns an
        opaque handle for stop_link_watcher.

        `on_up` runs in the watcher thread and MUST NOT take the store lock (it
        re-attaches directly via the pure backend), so a teardown that joins
        this thread cannot deadlock. Best-effort: a watcher that fails to start
        only means a manual flap won't auto-heal (still fails closed)."""
        try:
            proc = subprocess.Popen(
                ["ip", "netns", "exec", str(ns), "ip", "monitor", "link"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            log.warning("link watcher for %s/%s could not start: %s",
                        ns, ifname, e)
            return None

        def _run():
            try:
                for line in proc.stdout:           # blocks until a line or EOF
                    if _egress.link_up_event(line, str(ifname)):
                        try:
                            on_up()
                        except Exception as e:     # noqa: BLE001
                            log.warning("link-up reattach for %s failed: %s",
                                        ifname, e)
            except Exception as e:  # noqa: BLE001 — watcher must never crash
                log.warning("link watcher for %s/%s stopped: %s",
                            ns, ifname, e)

        t = threading.Thread(target=_run, name=f"linkwatch-{ns}", daemon=True)
        t.start()
        return (proc, t)

    def stop_link_watcher(self, handle) -> None:
        """Terminate the `ip monitor` subprocess (which EOFs the reader loop)
        and join the thread. Idempotent; safe on a None/dead handle."""
        if not handle:
            return
        proc, t = handle
        try:
            proc.terminate()
        except Exception as e:  # noqa: BLE001
            log.warning("link watcher terminate failed: %s", e)
        try:
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        t.join(timeout=2)

    def write_netns_resolv(self, ns, nameservers) -> None:
        # `ip netns exec <ns>` bind-mounts /etc/netns/<ns>/resolv.conf over
        # /etc/resolv.conf for the spawned process tree — this is how a silo
        # gets its one tunnel-bound resolver.
        d = ETC_NETNS / str(ns)
        d.mkdir(parents=True, exist_ok=True)
        body = "".join(f"nameserver {n}\n" for n in nameservers)
        (d / "resolv.conf").write_text(body)

    def remove_netns_resolv(self, ns) -> None:
        try:
            (ETC_NETNS / str(ns) / "resolv.conf").unlink()
        except FileNotFoundError:
            pass

    def systemctl_start(self, unit: str) -> None:
        subprocess.run(
            ["systemctl", "start", unit],
            check=True)

    def systemctl_stop(self, unit: str) -> None:
        subprocess.run(
            ["systemctl", "stop", unit],
            check=False)

    def tier2_silo_running(self, name: str) -> bool:
        """True if a tier-2 stop did NOT fully take effect: the launcher unit
        is still active, OR the rootless container still exists. The daemon is
        root but the container is admin-owned rootless, so existence is checked
        in admin's podman (the daemon can drop to admin via runuser). Used to
        fail closed — never report STOPPED while a container may survive."""
        unit = TIER2_SILO_LAUNCHER_FMT.format(name=name)
        active = subprocess.run(["systemctl", "is-active", unit],
                                capture_output=True, text=True).stdout.strip()
        # Only a DEFINITIVELY not-running unit state (inactive/failed: the main
        # process has exited) lets us proceed to the container check. Any active
        # or transitional state — and any unrecognized/empty output (systemctl
        # could not give a definitive answer) — is fail-closed "still running".
        if active not in ("inactive", "failed"):
            return True
        container = TIER2_CONTAINER_FMT.format(name=name)
        # `podman container exists` returns 0 when present, 1 when absent; run
        # it as admin (the rootless owner). rc 0 = still running; rc 1 = gone;
        # any OTHER rc means the check itself failed to run — fail closed and
        # treat that as still running, never silently report a clean stop.
        proc = subprocess.run(
            ["runuser", "-u", os.environ.get("QDISTRO_ADMIN_USER", "admin"),
             "--", "podman", "container", "exists", container],
            capture_output=True)
        return proc.returncode != 1

    def kill_pids(self, pids: Iterable[int], sig: int) -> None:
        for pid in pids:
            try:
                os.kill(int(pid), sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Logged but not fatal — the next pass with SIGKILL
                # may succeed if we drop into a higher-priv context.
                log.warning("kill(%d, %d) PermissionError", pid, sig)


# ---------------------------------------------------------------------------
# Durable audit log
# ---------------------------------------------------------------------------

# Schema mirrors pwd/qdistro_pwd_audit.py and broker/qdistro_admin_audit.py
# for cross-daemon consistency: one row per lifecycle mutation attempt,
# value payloads never logged (there are none here — only silo metadata).
_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_audit (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,            -- unix epoch seconds
    action      TEXT    NOT NULL,            -- create|delete|start|stop|freeze|resume
    silo        TEXT    NOT NULL,            -- silo name
    decision    TEXT    NOT NULL,            -- allow | deny | error
    reason      TEXT    NOT NULL,            -- short human-readable
    caller_uid  INTEGER,
    caller_pid  INTEGER,
    caller_exe  TEXT
);
CREATE INDEX IF NOT EXISTS session_audit_ts_idx ON session_audit(ts DESC);
CREATE INDEX IF NOT EXISTS session_audit_silo_idx ON session_audit(silo);
"""


class _AuditLog:
    """Append-only SQLite audit log for session-manager lifecycle
    mutations.

    Durability: opened with ``isolation_level=None`` (autocommit, every
    INSERT is its own committed transaction) and ``journal_mode=WAL`` +
    ``synchronous=FULL`` so a committed row is fsync'd to durable
    storage before ``record()`` returns — matching the
    write-then-fdatasync durability the silos.yaml store already
    guarantees in ``_SiloStore.save()``. The DB file is created 0600.
    """

    def __init__(self, path: Path = SILOS_AUDIT_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        old_umask = os.umask(0o077)
        try:
            self._conn = sqlite3.connect(
                str(self.path), isolation_level=None,
                check_same_thread=False)
        finally:
            os.umask(old_umask)
        self._lock = threading.Lock()
        self._conn.executescript(_AUDIT_SCHEMA)
        # WAL + FULL: each committed INSERT is fsync'd before the
        # autocommit returns, so a power loss after record() returns
        # cannot lose the row.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        try:
            os.chmod(str(self.path), 0o600)
        except OSError:
            pass
        # fsync the directory once so the newly-created DB/WAL files'
        # directory entries are durable across a crash — mirrors the
        # dir-fdatasync in _SiloStore.save(). Best-effort: the DB itself
        # is already on disk; this only orders the dir entry.
        try:
            dfd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as e:
            log.warning("audit dir fsync failed: %s", e)

    def record(self, action: str, silo: str, *,
               decision: str = "allow", reason: str = "",
               caller: dict[str, Any] | None = None) -> int:
        c = caller or {}
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO session_audit "
                "(ts, action, silo, decision, reason, "
                " caller_uid, caller_pid, caller_exe) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (int(time.time()), str(action), str(silo),
                 str(decision), str(reason),
                 c.get("uid"), c.get("pid"), c.get("exe", "")))
            return cur.lastrowid

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 10000))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, action, silo, decision, reason, "
                "       caller_uid, caller_pid, caller_exe "
                "FROM session_audit ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        cols = ("id", "ts", "action", "silo", "decision", "reason",
                "caller_uid", "caller_pid", "caller_exe")
        return [dict(zip(cols, r)) for r in rows]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Default egress tunnel-config / key providers (task 3)
# ---------------------------------------------------------------------------

def _default_tunnel_resolver(name: str) -> TunnelConfig:
    """Load a tunnel's *non-secret* config from /etc/qdistro/wg/<name>.conf.
    Minimal `key = value` lines: public_key, endpoint, address, dns (optional),
    allowed_ips (optional), keepalive (optional). Raises on absent/invalid so
    the silo comes up dark (retriable once the config lands)."""
    path = WG_CONFIG_DIR / f"{name}.conf"
    data: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip()
    ka = data.get("keepalive")
    return TunnelConfig(
        name=name,
        peer_public_key=data["public_key"],
        endpoint=data["endpoint"],
        address=data["address"],
        dns=data.get("dns") or None,
        allowed_ips=data.get("allowed_ips", "0.0.0.0/0, ::/0"),
        keepalive=int(ka) if ka else None,
    )


# WireGuard key custody (task 3 Phase C). Per-tunnel private keys live in this
# qdistro-pwd vault, pinned so only root (the session-manager — TCB) can read
# them; they never land in a silo home. The public key / endpoint / address /
# dns are non-secret and live in WG_CONFIG_DIR instead.
WG_PWD_VAULT = "wireguard"
PWD_BUS_NAME = "org.qdistro.Pwd1"
PWD_OBJ_PATH = "/org/qdistro/Pwd1"


def _wg_key_tag(name: str) -> str:
    return f"wg/{name}/private-key"


def _pwd_err_reason(e: Exception) -> str:
    """Map a pwd D-Bus error to a short, retriable reason for the dark state."""
    name = ""
    getter = getattr(e, "get_dbus_name", None)
    if callable(getter):
        name = getter() or ""
    name = name or type(e).__name__
    if name.endswith("NotUnlocked"):
        return "vault-locked"        # autostart before admin auth, or relock
    if name.endswith("NotFound"):
        return "no-key"
    if name.endswith("PolicyError"):
        return "pin-refused"
    return "pwd-unavailable"


def _pwd_getitem(vault: str, tag: str) -> str:
    """Real D-Bus GetItem against qdistro-pwd on the system bus. Imported
    lazily so headless unit tests (which inject a fake getter) need no bus."""
    import dbus  # local import: optional dependency
    bus = dbus.SystemBus()
    proxy = bus.get_object(PWD_BUS_NAME, PWD_OBJ_PATH)
    # Short timeout: this runs under the store lock during start(), so a hung
    # qdistro-pwd must fail fast (-> KeyUnavailable -> dark) rather than stall
    # every session-manager method for the default ~25s D-Bus timeout.
    return str(proxy.GetItem(vault, tag, dbus_interface=PWD_BUS_NAME,
                             timeout=5.0))


class _PwdKeyProvider:
    """Fetches a tunnel's WireGuard private key from qdistro-pwd. ANY failure
    (locked/pre-auth vault, missing key, pin refusal, bus down) raises
    KeyUnavailable so the silo comes up dark (retriable once the vault is
    unlocked / the key is provisioned) rather than failing its start (B3). The
    getter is injectable for tests."""

    def __init__(self, getter=None):
        self._getter = getter or _pwd_getitem

    def __call__(self, name: str) -> str:
        tag = _wg_key_tag(name)
        try:
            value = self._getter(WG_PWD_VAULT, tag)
        except KeyUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 — any pwd/bus failure -> dark
            raise KeyUnavailable(_pwd_err_reason(e)) from e
        if not value:
            raise KeyUnavailable("empty key")
        return value


def _default_key_provider(name: str) -> str:
    """Default for a bare _SiloStore (headless tests): wg silos come up dark.
    The daemon wires a live _PwdKeyProvider when it constructs the store."""
    raise KeyUnavailable("wireguard key custody not wired (no pwd provider)")


# ---------------------------------------------------------------------------
# Core store (no D-Bus)
# ---------------------------------------------------------------------------

class _SiloStore:
    """In-memory + on-disk silo registry with a thread-safe lock.

    The store is the *pure* part of the session manager: it owns the
    state machine, the silos.yaml file, and a _SystemOps that does
    the side effects. Unit tests substitute a fake _SystemOps and a
    tmp config path; the dbus shim adds nothing of substance beyond
    typed exceptions and signal emission.
    """

    def __init__(self, ops: _SystemOps,
                 config_path: Path = SILOS_CONFIG_PATH,
                 on_change: "callable | None" = None,
                 audit: "_AuditLog | None" = None,
                 egress_backend: "EgressBackend | None" = None,
                 tunnel_resolver: "callable | None" = None,
                 key_provider: "callable | None" = None):
        self._ops = ops
        self._config_path = Path(config_path)
        self._on_change = on_change
        # Per-silo netns egress (task 3). The backend is pure; tunnel_resolver
        # maps a tunnel name -> TunnelConfig (non-secret, from /etc/qdistro/wg),
        # and key_provider maps a tunnel name -> private key (from qdistro-pwd),
        # raising KeyUnavailable on a locked/pre-auth vault so the silo comes up
        # dark instead of failing its start. All injectable for tests.
        self._egress = egress_backend or EgressBackend()
        self._tunnel_resolver = tunnel_resolver or _default_tunnel_resolver
        self._key_provider = key_provider or _default_key_provider
        # Per-silo link-up watchers (Opt 3-C): name -> opaque handle from
        # _SystemOps.start_link_watcher. One per running, non-dark wg silo; its
        # callback re-attaches addr+route on a wg-<uid> link bounce without
        # taking _lock. Per-name access is serialized by the silo state machine
        # (a given silo's start/stop/delete never overlap), and the start vs.
        # phase-2 teardown of *different* silos touch different keys; dict ops
        # are atomic under the GIL, so no extra lock is needed here.
        self._watchers: dict[str, Any] = {}
        # Monotonic per-silo generation. Bumped on every watcher start AND stop;
        # the watcher callback captures its generation and no-ops if it no longer
        # matches — so a callback still in flight when stop_link_watcher's join
        # times out (or after the same silo restarts under a different tunnel)
        # cannot re-attach stale addr/route onto a torn-down/repurposed netns.
        self._watcher_gen: dict[str, int] = {}
        # Durable forensic sink. None disables auditing (e.g. tests that
        # don't care). Audit writes never raise into the lifecycle path.
        self._audit = audit
        self._lock = threading.RLock()
        self._silos: dict[str, Silo] = {}
        # Names with a stop() currently in its phase-2 teardown (running
        # without the store lock). A concurrent stop() for the same silo
        # waits on _stop_cv until the in-flight one finishes, then re-checks
        # state — so it observes the final result instead of returning
        # success prematurely. Guarded by _lock.
        self._stopping_inflight: set[str] = set()
        self._stop_cv = threading.Condition(self._lock)
        self.load()

    # ---- persistence ----------------------------------------------------

    def load(self) -> None:
        with self._lock:
            self._silos = {}
            if not self._config_path.exists():
                return
            data = _yaml_load(self._config_path.read_text())
            if not isinstance(data, dict):
                return
            for row in (data.get("silos") or []):
                if not isinstance(row, dict):
                    continue
                name = row.get("name")
                uid = row.get("uid")
                if not isinstance(name, str) or not isinstance(uid, int):
                    log.error(
                        "silos.yaml: dropping row with bad name/uid "
                        "types (name=%r uid=%r)", name, uid)
                    continue
                # Re-run the same validation we use on CreateSilo so a
                # hand-edited silos.yaml row can't smuggle a bogus name
                # (path traversal in shutil.rmtree, argv injection in
                # useradd/userdel, etc.) or an out-of-range uid into
                # the privileged code paths.
                kind = row.get("kind", KIND_TIER3_USER)
                try:
                    validate_name(name)
                    validate_kind(kind)
                    # uid validation depends on kind (tier2-template carries
                    # the admin launch-owner uid, tier3-user a fresh silo uid).
                    validate_silo_uid(uid, kind)
                    launch = validate_launch(kind, row.get("launch", {}) or {})
                    # Re-validate egress on load too: a hand-edited silos.yaml
                    # must not smuggle a bogus egress spec (e.g. an injected
                    # tunnel name) into the privileged netns/wg path.
                    egress = _validate_egress_field(row.get("egress"))
                except BadArgument as e:
                    log.error(
                        "silos.yaml: dropping row with invalid "
                        "name/uid/kind/launch/egress: %s", e)
                    continue
                state = row.get("state", State.STOPPED)
                if state not in State.ALL:
                    state = State.STOPPED
                silo = Silo(
                    name=name,
                    uid=int(uid),
                    state=str(state),
                    autostart=bool(row.get("autostart", False)),
                    created_at=int(row.get("created_at", 0) or 0),
                    last_change=int(row.get("last_change", 0) or 0),
                    kind=kind,
                    launch=launch,
                    egress=egress,
                )
                self._silos[name] = silo

    def save(self) -> None:
        with self._lock:
            rows = [s.to_dict() for s in self._silos.values()]
            text = _silos_yaml_render(rows)
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._config_path.with_suffix(self._config_path.suffix + ".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                data = text.encode()
                mv = memoryview(data)
                while mv:
                    written = os.write(fd, mv)
                    mv = mv[written:]
                os.fdatasync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self._config_path)
            # Directory fdatasync orders the rename before a crash — it's a
            # durability optimization, not a correctness requirement. If it
            # fails, the new data is already on disk (os.replace succeeded).
            # Swallow the error so callers (e.g. _transition) don't roll back
            # in-memory state when the file has already been committed.
            try:
                dfd = os.open(str(self._config_path.parent), os.O_RDONLY)
                try:
                    os.fdatasync(dfd)
                finally:
                    os.close(dfd)
            except OSError as e:
                log.warning("dir fdatasync after save failed: %s "
                            "(data is on disk; ordering not guaranteed "
                            "across crash)", e)

    # ---- lookup ---------------------------------------------------------

    def get(self, name: str) -> Silo:
        with self._lock:
            try:
                return self._silos[name]
            except KeyError:
                raise UnknownSilo(f"no such silo {name!r}")

    def list_silos(self) -> list[Silo]:
        with self._lock:
            return sorted(self._silos.values(), key=lambda s: s.name)

    # ---- transitions ----------------------------------------------------

    def _transition(self, silo: Silo, new_state: str) -> None:
        if new_state not in State.ALL:
            raise BadState(f"unknown target state {new_state!r}")
        allowed = _STATE_TRANSITIONS.get(silo.state, frozenset())
        if new_state not in allowed:
            raise BadState(
                f"silo {silo.name!r} cannot move {silo.state} → "
                f"{new_state} (allowed: {sorted(allowed) or 'none'})")
        prev_state = silo.state
        prev_last_change = silo.last_change
        silo.state = new_state
        silo.last_change = int(time.time())
        try:
            self.save()
        except Exception:
            # Roll back in-memory state so it matches what is on disk.
            silo.state = prev_state
            silo.last_change = prev_last_change
            raise
        self._emit_change(silo.name, silo.state)

    def _force_state(self, silo: Silo, new_state: str) -> None:
        # Bypass the transition table for rollback paths. Always
        # persists + emits SiloChanged so subscribers don't get
        # stuck on the previous state. Used when a side-effect
        # failed mid-transition and the daemon has to declare a
        # recoverable resting state regardless of the table.
        silo.state = new_state
        silo.last_change = int(time.time())
        self.save()
        self._emit_change(silo.name, silo.state)

    def _emit_change(self, name: str, state: str) -> None:
        if self._on_change is not None:
            try:
                self._on_change(name, state)
            except Exception:  # noqa: BLE001
                log.exception("on_change callback raised; continuing")

    def _audit_record(self, action: str, silo: str, *,
                      decision: str, reason: str,
                      caller: dict[str, Any] | None) -> None:
        # Write a durable audit row for a lifecycle mutation attempt.
        # Audit failures must NEVER break the lifecycle operation: the
        # mutation has already succeeded (or been refused) at the call
        # site, so we only log if the durable write fails.
        if self._audit is None:
            return
        try:
            self._audit.record(action, silo, decision=decision,
                                reason=reason, caller=caller)
        except Exception:  # noqa: BLE001
            log.exception(
                "audit record failed (action=%s silo=%s decision=%s)",
                action, silo, decision)

    def _clear_stop_inflight(self, name: str) -> None:
        # Release the in-flight stop marker for *name* and wake any
        # concurrent stop() callers waiting on it. Idempotent. Must be
        # called with self._lock held, BEFORE any on_change emission, so a
        # re-entrant stop() from the callback doesn't block on a marker
        # only the current thread can clear.
        if name in self._stopping_inflight:
            self._stopping_inflight.discard(name)
            self._stop_cv.notify_all()

    # ---- per-silo netns egress (task 3) ---------------------------------

    def _is_netns_backed(self, silo: Silo) -> bool:
        # Only tier3-user silos with an explicit egress policy get a netns; a
        # silo with egress=None keeps legacy host networking (backward compat).
        return silo.kind == KIND_TIER3_USER and silo.egress is not None

    def _apply_egress(self, silo: Silo) -> None:
        """Bring up the silo's netns egress per its policy, BEFORE the launcher
        unit, so the silo's processes (which enter the netns — Phase D) see the
        right route + resolver from the first instant. A locked/pre-auth pwd
        vault or missing tunnel config brings the silo up *dark* (netns present,
        no egress device) — never fails the start (B3)."""
        ns = _egress.netns_name(silo.name)
        policy = EgressPolicy.parse(silo.egress)
        self._stop_egress_watcher(silo.name)       # clear any stale watcher
        self._ops.netns_create(ns)
        tunnel = None
        keyfn = None
        if policy.mode == "wg":
            try:
                tunnel = self._tunnel_resolver(policy.tunnel)
            except Exception as e:  # noqa: BLE001
                # No/invalid tunnel config: come up dark (netns only), retriable
                # once the config lands. Don't fail the launch.
                log.warning("silo %r tunnel %r config unavailable: %s — dark",
                            silo.name, policy.tunnel, e)
                self._egress.apply(ns, silo.uid, EgressPolicy.parse("none"),
                                   self._ops)
                self._audit_record("egress-apply", silo.name, decision="allow",
                                   reason="dark:no-tunnel-config", caller=None)
                return
            keyfn = self._key_provider
        try:
            result = self._egress.apply(ns, silo.uid, policy, self._ops,
                                        tunnel=tunnel, keyfn=keyfn)
        except Exception as e:  # noqa: BLE001
            # A transient bring-up failure (e.g. a boot-time endpoint-DNS race
            # in `wg set`) must not fail the whole start — come up dark
            # (retriable next start) with the backstop in place, never leaking.
            log.warning("silo %r egress %s apply failed: %s — coming up dark",
                        silo.name, silo.egress, e)
            self._teardown_egress_devices(ns, silo.uid, policy)
            self._egress.apply(ns, silo.uid, EgressPolicy.parse("none"),
                               self._ops)
            self._audit_record("egress-apply", silo.name, decision="allow",
                               reason="dark:apply-failed", caller=None)
            return
        reason = (f"dark:{result.pending}"
                  if result.dark and result.pending else silo.egress)
        if result.dark and result.pending:
            log.warning("silo %r egress %s came up dark: %s",
                        silo.name, silo.egress, result.pending)
        elif policy.mode == "wg" and not result.dark:
            # Live tunnel: watch wg-<uid> for a link bounce and re-attach its
            # default route (Opt 3-C). A dark silo has no device to watch.
            self._start_egress_watcher(silo.name, ns, silo.uid, policy, tunnel)
        self._audit_record("egress-apply", silo.name, decision="allow",
                           reason=reason, caller=None)

    def _start_egress_watcher(self, name: str, ns: str, uid: int,
                              policy: "EgressPolicy",
                              tunnel: "TunnelConfig | None") -> None:
        """Start a link-up watcher for a live wg silo; its callback re-attaches
        addr+route via the pure backend (no _lock — see reattach's docstring).
        Captures the immutable (ns, uid, policy, tunnel) + a generation token so
        the callback needs no store state and self-cancels if it is stale."""
        self._stop_egress_watcher(name)            # bumps the generation
        backend, ops = self._egress, self._ops
        gen = self._watcher_gen.get(name, 0)

        def _on_up() -> None:
            # Self-cancel a stale callback (teardown/restart bumped the gen):
            # never re-attach onto a netns that may be torn down or repurposed.
            if self._watcher_gen.get(name) != gen:
                return
            backend.reattach(ns, uid, policy, ops, tunnel=tunnel)

        try:
            handle = ops.start_link_watcher(ns, _egress.wg_ifname(uid), _on_up)
        except Exception as e:  # noqa: BLE001 — a missing watcher fails closed
            log.warning("could not start link watcher for %r: %s", name, e)
            return
        if handle is not None:
            self._watchers[name] = handle

    def _stop_egress_watcher(self, name: str) -> None:
        # Bump the generation FIRST so any callback that fires while we tear the
        # watcher down (or that is already mid-flight past the join timeout)
        # sees the mismatch and no-ops before touching netns state.
        self._watcher_gen[name] = self._watcher_gen.get(name, 0) + 1
        handle = self._watchers.pop(name, None)
        if handle is None:
            return
        try:
            self._ops.stop_link_watcher(handle)
        except Exception as e:  # noqa: BLE001
            log.warning("stopping link watcher for %r failed: %s", name, e)

    def _teardown_egress_devices(self, ns: str, uid: int,
                                 policy: "EgressPolicy") -> None:
        # Best-effort device teardown (no netns removal) for the dark-fallback
        # path: clear any half-configured device before re-applying `none`.
        try:
            self._egress.teardown(ns, uid, policy, self._ops)
        except Exception as e:  # noqa: BLE001
            log.warning("egress device teardown (dark fallback) failed: %s", e)

    def _teardown_egress(self, name: str, uid: int,
                         egress: str | None) -> None:
        """Best-effort egress + netns teardown. Never raises into the lifecycle
        path — a teardown failure is logged, not fatal (the next start re-applies
        teardown-stale-first, and delete removes the netns regardless)."""
        self._stop_egress_watcher(name)
        if egress is None:
            return
        ns = _egress.netns_name(name)
        try:
            policy = EgressPolicy.parse(egress)
        except _egress.EgressError:
            policy = None
        try:
            self._egress.teardown(ns, uid, policy, self._ops)
        except Exception as e:  # noqa: BLE001
            log.warning("egress teardown for %r failed: %s — continuing",
                        name, e)
        try:
            self._ops.netns_remove(ns)
        except Exception as e:  # noqa: BLE001
            log.warning("netns_remove for %r failed: %s — continuing", name, e)

    def _force_clear_egress(self, name: str, uid: int) -> None:
        """Unconditionally clear ANY egress state for a silo (devices, the nft
        skuid backstop, the per-netns resolver, the netns) regardless of its
        current policy. Used when starting a legacy (egress=None) silo that has
        stale state from a prior policy or a crash — _teardown_egress short-
        circuits on egress=None, which is why this exists. Only invoked when a
        stale netns is detected, so normal legacy silos pay nothing. Best-effort."""
        self._stop_egress_watcher(name)
        ns = _egress.netns_name(name)
        try:
            self._egress.teardown(ns, uid, None, self._ops)
        except Exception as e:  # noqa: BLE001
            log.warning("forced egress clear for %r failed: %s — continuing",
                        name, e)
        try:
            self._ops.netns_remove(ns)
        except Exception as e:  # noqa: BLE001
            log.warning("netns_remove for %r failed: %s — continuing", name, e)

    # ---- create / delete -----------------------------------------------

    def create(self, name: str, uid: int, *, autostart: bool = False,
               kind: str = KIND_TIER3_USER, launch: dict | None = None,
               egress: str | None = None,
               caller: dict[str, Any] | None = None) -> Silo:
        try:
            validate_name(name)
            validate_kind(kind)
            validate_silo_uid(uid, kind)
            launch_norm = validate_launch(kind, launch or {})
            egress_norm = _validate_egress_field(egress)
            if egress_norm is not None and kind != KIND_TIER3_USER:
                raise BadArgument(
                    "egress policy is only valid for tier3-user silos "
                    f"(got kind {kind!r})")
            with self._lock:
                if name in self._silos:
                    raise SiloExists(f"silo {name!r} already exists")
                if kind == KIND_TIER3_USER:
                    # A tier-3 silo is a real Linux user: its uid must be
                    # unique and it gets a home + state dir via useradd.
                    for existing in self._silos.values():
                        if existing.uid == uid and existing.kind == KIND_TIER3_USER:
                            raise SiloExists(
                                f"uid {uid} already in use by silo "
                                f"{existing.name!r}")
                    if self._ops.user_exists(name):
                        raise SiloExists(f"system user {name!r} already exists")
                    if self._ops.uid_exists(uid):
                        raise SiloExists(
                            f"uid {uid} already in use on this system")
                    self._ops.useradd(name, uid)
                    self._ops.make_state_dir(name, uid)
                # else tier2-template: no useradd / no per-uid state dir — the
                # launch-owner is admin (already exists, shared across template
                # silos) and the silo's state is the binding's state_path,
                # created by qdistro-template-promote, not here.
                silo = Silo(
                    name=name, uid=int(uid), state=State.CREATED,
                    autostart=bool(autostart),
                    created_at=int(time.time()),
                    last_change=int(time.time()),
                    kind=kind, launch=launch_norm,
                    egress=egress_norm,
                )
                self._silos[name] = silo
                self.save()
                self._emit_change(silo.name, silo.state)
        except Exception as e:  # noqa: BLE001
            # Refusals (SiloExists/BadArgument) are "deny"; an unexpected
            # side-effect failure (useradd/btrfs/etc.) is "error". Either
            # way the privileged attempt leaves a durable trail.
            decision = "deny" if isinstance(e, SessionError) else "error"
            self._audit_record("create", str(name), decision=decision,
                               reason=str(e), caller=caller)
            raise
        self._audit_record("create", silo.name, decision="allow",
                           reason=f"uid={silo.uid}", caller=caller)
        return silo

    def delete(self, name: str, caller: dict[str, Any] | None = None) -> None:
        try:
            with self._lock:
                silo = self.get(name)
                # Refuse while the silo is anything other than Created or
                # Stopped — the admin must explicitly Stop it first.
                if silo.state not in (State.CREATED, State.STOPPED):
                    if silo.state in (State.STOPPING,):
                        msg = (f"silo {silo.name!r} is {silo.state}; "
                               f"wait for it to reach Stopped")
                    elif silo.state == State.DELETING:
                        msg = (f"silo {silo.name!r} is {silo.state}; "
                               f"the daemon may have crashed mid-delete")
                    else:
                        msg = (f"silo {silo.name!r} is {silo.state}; "
                               f"stop it first")
                    raise SiloBusy(msg)
                self._transition(silo, State.DELETING)
                try:
                    self._ops.cgroup_remove(silo.name)
                    # Idempotent egress/netns cleanup: stop() already tore it
                    # down for the normal path, but a crash mid-stop could
                    # leave a netns behind — never leak it past delete.
                    self._teardown_egress(silo.name, silo.uid, silo.egress)
                    self._ops.remove_state_dir(silo.name)
                    self._ops.userdel(silo.name)
                except Exception as e:  # noqa: BLE001
                    # Side-effect failure mid-delete: roll back to
                    # Stopped so the admin can retry. Use _force_state
                    # so subscribers see the Deleting→Stopped emit and
                    # the silo isn't wedged in a terminal-on-paper state
                    # (DELETING has no legal outgoing edges).
                    log.error("delete of silo %r failed during teardown: %s",
                              silo.name, e)
                    self._force_state(silo, State.STOPPED)
                    raise SessionError(f"delete failed: {e}") from e
                self._silos.pop(name, None)
                self.save()
                self._emit_change(silo.name, "Deleted")
        except SessionError as e:
            decision = "deny" if isinstance(
                e, (UnknownSilo, SiloBusy)) else "error"
            self._audit_record("delete", str(name), decision=decision,
                               reason=str(e), caller=caller)
            raise
        self._audit_record("delete", str(name), decision="allow",
                           reason="deleted", caller=caller)

    # ---- egress policy mutation (task 3 Phase B) ------------------------

    def set_egress(self, name: str, egress: str | None,
                   caller: dict[str, Any] | None = None) -> None:
        """Set (or clear) a tier3-user silo's egress policy. Admin-approved at
        the D-Bus boundary (_require_admin) and audited here, exactly like the
        other silo lifecycle mutations — egress is admin-initiated infrastructure
        config, not an untrusted-silo request, so it follows the create/delete
        idiom rather than the broker's request/approve queue (which exists for
        silo-initiated cross-boundary access). Takes effect at the next start:
        reconfiguring a running silo's tunnel mid-flight is out of scope (a
        stop/start re-applies the new policy), so the silo must be stopped."""
        prev = None
        try:
            egress_norm = _validate_egress_field(egress)
            with self._lock:
                silo = self.get(name)
                if silo.kind != KIND_TIER3_USER:
                    raise BadArgument(
                        "egress policy is only valid for tier3-user silos "
                        f"(silo {name!r} is {silo.kind})")
                if silo.state not in (State.CREATED, State.STOPPED):
                    raise SiloBusy(
                        f"silo {silo.name!r} is {silo.state}; stop it before "
                        f"changing egress (the new policy applies at next start)")
                prev = silo.egress
                no_change = (egress_norm == prev)
                silo.egress = egress_norm
                try:
                    self.save()
                except Exception:
                    silo.egress = prev           # match what's on disk
                    raise
        except SessionError as e:
            decision = "deny" if isinstance(
                e, (UnknownSilo, SiloBusy, BadArgument)) else "error"
            self._audit_record("egress-configure", str(name), decision=decision,
                               reason=str(e), caller=caller)
            raise
        reason = ("no-change" if no_change
                  else f"{prev!r} -> {egress_norm!r}")
        self._audit_record("egress-configure", str(name), decision="allow",
                           reason=reason, caller=caller)

    # ---- start / stop / freeze / resume -------------------------------

    def _export_tier2_launch_env(self, silo: Silo) -> None:
        """Write the per-silo env the tier-2 launcher unit reads. The unit
        drops to admin and runs spawn-tier2 with these — TIER2_SILO makes the
        launch binding-resolved (the only launch that mounts real state) and
        TIER2_NETWORK sets egress. argv is JSON so the launcher script can
        re-split it without quoting hazards."""
        lc = silo.launch
        # shlex.quote EVERY value so the launcher can `set -a; . envfile`
        # safely: the argv JSON contains spaces/brackets/double-quotes, and an
        # argv entry may even contain a single quote (validate_launch allows
        # it), which a naive single-quote wrapper could not represent.
        # shlex.quote produces a correctly-escaped bash token for any string.
        argv_json = json.dumps(lc.get("argv", []))
        kv = [
            ("TIER2_SILO", lc["template_silo"]),
            ("TIER2_NETWORK", lc["network"]),
            ("QD_WORKLOAD", lc["workload"]),
            ("QD_CONTAINER", f"qdistro-silo-{silo.name}"),
            ("QD_APP_ARGV_JSON", argv_json),
        ]
        lines = [f"{k}={shlex.quote(v)}" for k, v in kv] + [""]
        self._ops.write_launch_env(silo.name, "\n".join(lines))

    def start(self, name: str, caller: dict[str, Any] | None = None) -> None:
        reason = "started"
        try:
            with self._lock:
                silo = self.get(name)
                if silo.state == State.ACTIVE:
                    # idempotent — fall through to the post-lock audit so
                    # the (potentially fsync-blocking) audit write never
                    # happens while the store lock is held.
                    reason = "already active (idempotent)"
                else:
                    self._transition(silo, State.ACTIVE)
                    try:
                        if silo.kind == KIND_TIER2_TEMPLATE:
                            # Tier-2 templated silo: launch through its unit,
                            # which runs spawn-tier2 as admin (rootless podman
                            # manages its own cgroup, so no per-silo cgroup
                            # here). The unit reads the launch stanza the
                            # daemon exported (see _export_tier2_launch_env).
                            self._export_tier2_launch_env(silo)
                            unit = TIER2_SILO_LAUNCHER_FMT.format(name=silo.name)
                        else:
                            self._ops.cgroup_create(silo.name)
                            # Bring up the per-silo netns egress BEFORE the
                            # launcher, so the silo's processes (which enter the
                            # netns) get the right route + resolver immediately.
                            # No-op for legacy (egress=None) silos.
                            if self._is_netns_backed(silo):
                                self._apply_egress(silo)
                            else:
                                # Legacy silo (egress=None): if a netns lingers
                                # from a prior policy / crash-mid-stop, FULLY
                                # clear it — devices, the per-netns resolver,
                                # AND the nft skuid backstop. A leftover backstop
                                # element would drop this silo's traffic in the
                                # init netns, leaving a legacy silo permanently
                                # dark on host networking (codex #2); a leftover
                                # netns would make spawn-tier3 run it in a stale
                                # dark netns (Fable S5).
                                #
                                # Always clear a possibly-orphaned backstop
                                # element for this uid — cheap and never creates
                                # the nft table, so a pristine legacy silo still
                                # touches nothing, but an orphaned blocked_uids
                                # entry (crash after netns_remove, before the
                                # element delete) can't keep this silo dark
                                # (codex #3).
                                self._ops.nft_skuid_drop(silo.uid, False)
                                # Full clear (devices + resolver + netns) only
                                # when a stale netns actually lingers.
                                if self._ops.netns_exists(
                                        _egress.netns_name(silo.name)):
                                    self._force_clear_egress(silo.name, silo.uid)
                            unit = SILO_LAUNCHER_FMT.format(name=silo.name,
                                                            uid=silo.uid)
                        self._ops.systemctl_start(unit)
                    except Exception as e:  # noqa: BLE001
                        # Roll back state on failure. _force_state emits
                        # SiloChanged so the admin UI / PodApps don't stick
                        # on "Active" after a failed launch. Tear down any
                        # egress we applied so a failed start leaves no
                        # half-configured netns behind.
                        log.error("start of silo %r failed: %s", silo.name, e)
                        if self._is_netns_backed(silo):
                            self._teardown_egress(silo.name, silo.uid,
                                                  silo.egress)
                        self._force_state(silo, State.STOPPED)
                        if isinstance(e, SessionError):
                            raise
                        raise SessionError(
                            f"start of silo {silo.name!r} failed: {e}") from e
        except SessionError as e:
            decision = "deny" if isinstance(
                e, (UnknownSilo, BadState)) else "error"
            self._audit_record("start", str(name), decision=decision,
                               reason=str(e), caller=caller)
            raise
        self._audit_record("start", str(name), decision="allow",
                           reason=reason, caller=caller)

    def stop(self, name: str, grace_s: int = DEFAULT_STOP_GRACE_S,
             caller: dict[str, Any] | None = None) -> None:
        # Thin audit wrapper around _stop_impl: the teardown logic
        # (lock ordering, in-flight markers, grace polling) is left
        # byte-for-byte unchanged so auditing cannot introduce a
        # lifecycle regression. We only observe the outcome.
        try:
            self._stop_impl(name, grace_s)
        except SessionError as e:
            decision = "deny" if isinstance(
                e, (UnknownSilo, BadState)) else "error"
            self._audit_record("stop", str(name), decision=decision,
                               reason=str(e), caller=caller)
            raise
        self._audit_record("stop", str(name), decision="allow",
                           reason="stopped", caller=caller)

    def _stop_impl(self, name: str,
                   grace_s: int = DEFAULT_STOP_GRACE_S) -> None:
        # Phase 1: validate state and transition to STOPPING under the lock.
        with self._lock:
            silo = self.get(name)
            # A concurrent/retried StopSilo while a stop is already in its
            # phase-2 teardown (running without the lock) must NOT return
            # success prematurely — the first stop may still fail. Wait for
            # the in-flight stop to finish, then re-check the final state.
            while silo.name in self._stopping_inflight:
                self._stop_cv.wait()
                # Re-fetch in case the silo was deleted while we waited.
                silo = self._silos.get(name)
                if silo is None:
                    raise UnknownSilo(f"no such silo {name!r}")
            if silo.state == State.STOPPED:
                return
            if silo.state not in (State.ACTIVE, State.FROZEN):
                raise BadState(
                    f"cannot stop silo {silo.name!r} in state {silo.state}")
            # If frozen we must thaw before SIGTERM has any effect.
            if silo.state == State.FROZEN:
                try:
                    self._ops.cgroup_freeze(silo.name, False)
                except Exception as e:  # noqa: BLE001
                    log.warning("cgroup_freeze(False) for %r failed during "
                                "stop pre-thaw: %s — continuing",
                                silo.name, e)
            self._transition(silo, State.STOPPING)
            # Snapshot the immutable identifiers we need outside the lock,
            # and claim the in-flight slot so concurrent stops wait.
            silo_name = silo.name
            silo_uid = silo.uid
            silo_kind = silo.kind
            silo_egress = silo.egress
            self._stopping_inflight.add(silo_name)

        # Phase 2: grace-period polling WITHOUT holding the store lock.
        # Other callers (ListSilos, signal handlers) can proceed while we
        # wait for processes to exit. The try/finally guarantees the
        # in-flight slot is cleared and waiters are woken on every exit.
        try:
            if silo_kind == KIND_TIER2_TEMPLATE:
                # Tier-2 templated silo: stopping its unit (whose ExecStop runs
                # `podman stop` on the rootless container) is the whole teardown
                # — no per-silo cgroup to freeze/kill/rmdir. This branch owns its
                # OWN error handling (the cgroup-path handlers below force
                # STOPPED, which would be a lie here) and must never leave the
                # silo wedged in STOPPING.
                try:
                    self._ops.systemctl_stop(
                        TIER2_SILO_LAUNCHER_FMT.format(name=silo_name))
                    survived = self._ops.tier2_silo_running(silo_name)
                except Exception as e:  # noqa: BLE001
                    # The stop machinery itself errored (subprocess OSError, a
                    # missing systemctl/podman, ...). Fail closed: the container
                    # may still be running, so force ACTIVE (honest, retryable)
                    # and surface the error — do NOT report STOPPED.
                    with self._lock:
                        self._clear_stop_inflight(silo_name)
                        self._force_state(silo, State.ACTIVE)
                    raise SessionError(
                        f"stop of tier-2 silo {silo_name!r} failed: {e}") from e
                # FAIL CLOSED: systemctl_stop is check=False and the unit's
                # ExecStop is best-effort, so a unit timeout / podman failure /
                # surviving rootless container would otherwise be reported as a
                # clean stop — a lie that hides an orphan. Report STOPPED only
                # when the unit is inactive AND the container is gone.
                if survived:
                    with self._lock:
                        self._clear_stop_inflight(silo_name)
                        self._force_state(silo, State.ACTIVE)
                    raise SessionError(
                        f"stop of tier-2 silo {silo_name!r} did not take "
                        f"effect: the launcher unit is still active or the "
                        f"container "
                        f"{TIER2_CONTAINER_FMT.format(name=silo_name)} survives")
                # The container is verified gone — the stop SUCCEEDED. Clearing
                # the (now-stale) launch env is best-effort: a failure here must
                # not flip the verified-stopped silo back to ACTIVE (the daemon
                # rewrites the env on the next start anyway).
                try:
                    self._ops.remove_launch_env(silo_name)
                except Exception as e:  # noqa: BLE001
                    log.warning("remove_launch_env for %r failed after a "
                                "verified stop: %s — leaving the stale env",
                                silo_name, e)
                with self._lock:
                    self._clear_stop_inflight(silo_name)
                    self._transition(silo, State.STOPPED)
                return
            try:
                unit = SILO_LAUNCHER_FMT.format(name=silo_name, uid=silo_uid)
                self._ops.systemctl_stop(unit)
                # Capture pids defensively — cgroup_pids may raise on a
                # corrupted procfs entry. Treat any read failure as
                # "assume populated; SIGKILL anything we can find".
                try:
                    pids = self._ops.cgroup_pids(silo_name)
                except Exception as e:  # noqa: BLE001
                    log.warning("cgroup_pids for %r raised %s; assuming "
                                "empty", silo_name, e)
                    pids = []
                if pids:
                    self._ops.kill_pids(pids, signal.SIGTERM)
                    deadline = time.monotonic() + max(0, int(grace_s))
                    while time.monotonic() < deadline:
                        try:
                            if not self._ops.cgroup_pids(silo_name):
                                break
                        except Exception:  # noqa: BLE001
                            break
                        time.sleep(0.1)
                    try:
                        remaining = self._ops.cgroup_pids(silo_name)
                    except Exception:  # noqa: BLE001
                        remaining = []
                    if remaining:
                        self._ops.kill_pids(remaining, signal.SIGKILL)
                # After SIGKILL the kernel needs a moment to reap the
                # task and drop it from cgroup.procs. Without this poll
                # the rmdir below races EBUSY and the cgroup dir leaks.
                reap_deadline = time.monotonic() + 1.0
                while time.monotonic() < reap_deadline:
                    try:
                        if not self._ops.cgroup_pids(silo_name):
                            break
                    except Exception:  # noqa: BLE001
                        break
                    time.sleep(0.05)
                try:
                    self._ops.cgroup_remove(silo_name)
                except OSError as e:
                    # The cgroup is still populated — kernel didn't reap
                    # in time, or one of the processes is stuck in D.
                    # Log loudly so the leak is visible; transition to
                    # STOPPED anyway so the admin can retry stop or
                    # delete (the autostart sweep will paper over the
                    # leftover dir on next daemon start).
                    log.error("cgroup rmdir for silo %r failed: %s "
                              "(cgroup may have leaked)", silo_name, e)
                # Tear down the per-silo netns egress (devices, backstop,
                # resolver) and remove the netns. Best-effort: never raises, so
                # a teardown hiccup can't wedge the stop. No-op for legacy
                # (egress=None) silos.
                self._teardown_egress(silo_name, silo_uid, silo_egress)
            except SessionError:
                # Already-typed errors propagate; force back to STOPPED
                # so the silo isn't wedged in STOPPING.
                with self._lock:
                    self._clear_stop_inflight(silo_name)
                    self._force_state(silo, State.STOPPED)
                raise
            except Exception as e:  # noqa: BLE001
                log.error("stop of silo %r failed mid-teardown: %s — "
                          "forcing STOPPED so the silo isn't wedged",
                          silo_name, e)
                with self._lock:
                    self._clear_stop_inflight(silo_name)
                    self._force_state(silo, State.STOPPED)
                raise SessionError(
                    f"stop of silo {silo_name!r} failed: {e}") from e

            # Phase 3: re-acquire the lock for the final state transition.
            # Clear the in-flight slot + notify waiters BEFORE _transition
            # emits on_change: the callback may re-enter stop() on this same
            # thread (Stopped signal), and it must not block waiting on a
            # marker only this thread can clear (re-entrant deadlock).
            with self._lock:
                self._clear_stop_inflight(silo_name)
                self._transition(silo, State.STOPPED)
        finally:
            # Safety net: guarantee the in-flight slot is released and
            # waiters woken on every exit path (idempotent — the success
            # and error paths above already cleared it).
            with self._lock:
                self._clear_stop_inflight(silo_name)

    def freeze(self, name: str, caller: dict[str, Any] | None = None) -> None:
        reason = "frozen"
        try:
            with self._lock:
                silo = self.get(name)
                if silo.state == State.FROZEN:
                    # idempotent — audit after releasing the lock.
                    reason = "already frozen (idempotent)"
                else:
                    self._transition(silo, State.FROZEN)
                    self._ops.cgroup_freeze(silo.name, True)
        except Exception as e:  # noqa: BLE001
            decision = "deny" if isinstance(
                e, (UnknownSilo, BadState)) else "error"
            self._audit_record("freeze", str(name), decision=decision,
                               reason=str(e), caller=caller)
            raise
        self._audit_record("freeze", str(name), decision="allow",
                           reason=reason, caller=caller)

    def resume(self, name: str, caller: dict[str, Any] | None = None) -> None:
        reason = "resumed"
        try:
            with self._lock:
                silo = self.get(name)
                if silo.state == State.ACTIVE:
                    # idempotent — audit after releasing the lock.
                    reason = "already active (idempotent)"
                elif silo.state != State.FROZEN:
                    raise BadState(
                        f"cannot resume silo {silo.name!r} in state "
                        f"{silo.state}")
                else:
                    self._ops.cgroup_freeze(silo.name, False)
                    self._transition(silo, State.ACTIVE)
        except Exception as e:  # noqa: BLE001
            decision = "deny" if isinstance(
                e, (UnknownSilo, BadState)) else "error"
            self._audit_record("resume", str(name), decision=decision,
                               reason=str(e), caller=caller)
            raise
        self._audit_record("resume", str(name), decision="allow",
                           reason=reason, caller=caller)

    # ---- startup recovery ----------------------------------------------

    def autostart_pass(self) -> list[str]:
        """Called once at daemon startup. For each silo whose
        persisted state is Active OR whose autostart flag is true,
        bring it back to Active. Returns the list of started silo
        names. Silos whose state is Frozen also get re-thawed to
        Active — admin can refreeze after the reboot if desired,
        but a frozen silo across a reboot is an invariant we don't
        try to preserve (the cgroup is gone)."""
        started: list[str] = []
        with self._lock:
            for silo in list(self._silos.values()):
                want_start = silo.autostart or silo.state in (
                    State.ACTIVE, State.FROZEN)
                # Normalize stale "Active"/"Frozen"/"Stopping" to Stopped
                # so the legal-transition check passes.
                if silo.state not in (State.CREATED, State.STOPPED):
                    silo.state = State.STOPPED
                if want_start:
                    try:
                        self.start(silo.name)
                        started.append(silo.name)
                    except SessionError as e:
                        log.warning("autostart of %r failed: %s",
                                    silo.name, e)
            self.save()
        return started


# ---------------------------------------------------------------------------
# YAML helpers (silos.yaml is a tiny schema — avoid a hard PyYAML dep)
# ---------------------------------------------------------------------------

def _yaml_load(text: str) -> Any:
    """Tolerant tiny-YAML loader: handles the exact schema we write.
    Falls through to PyYAML if installed and the input is anything
    more complex than our simple list-of-mappings."""
    # Try real PyYAML first if importable; on test hosts it usually is.
    try:
        import yaml  # type: ignore[import-not-found]
        return yaml.safe_load(text)
    except ImportError:
        pass
    # Hand-rolled parser for the file we generate. Handles flat scalar keys
    # plus ONE nested mapping (`launch:`) whose children are 6-space-indented
    # scalars — enough for the fableplan2 task-04 schema.
    data: dict[str, Any] = {"silos": []}
    cur: dict[str, Any] | None = None
    cur_sub: dict[str, Any] | None = None
    in_silos = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("silos:"):
            in_silos = True
            continue
        if not in_silos:
            continue
        if line.startswith("  - "):
            cur = {}
            cur_sub = None
            data["silos"].append(cur)
            kv = line[4:].strip()
            if ":" in kv:
                k, v = kv.split(":", 1)
                cur[k.strip()] = _yaml_scalar(v.strip())
            continue
        if cur is None:
            continue
        indent = len(line) - len(line.lstrip())
        kv = line.strip()
        if ":" not in kv and cur_sub is None:
            continue
        if indent >= 6 and cur_sub is not None:
            k, v = kv.split(":", 1)
            cur_sub[k.strip()] = _yaml_scalar(v.strip())
            continue
        if indent == 4:
            k, v = kv.split(":", 1)
            k = k.strip()
            v = v.strip()
            if k == "launch" and v == "":
                cur_sub = {}
                cur["launch"] = cur_sub
            else:
                cur_sub = None
                cur[k] = _yaml_scalar(v)
    return data


def _yaml_scalar(v: str) -> Any:
    if v == "" or v == "~" or v.lower() == "null":
        return None
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    try:
        return int(v)
    except ValueError:
        pass
    return v


def _silos_yaml_render(rows: list[dict[str, Any]]) -> str:
    """Pin the exact schema so a test can assert on the bytes."""
    out = [
        "# qdistro-session-manager persistence file. Schema:",
        "#   silos:",
        "#     - name: <str>          # POSIX-ish silo name (= Linux user name)",
        "#       uid: <int>           # Linux uid (2000..60000)",
        "#       state: <Created|Active|Frozen|Stopping|Stopped|Deleting>",
        "#       autostart: <bool>    # restart on daemon startup if true",
        "#       created_at: <int>    # epoch seconds",
        "#       last_change: <int>   # epoch seconds of last state change",
        "#       kind: <tier3-user|tier2-template>",
        "#       egress: <none|direct|wg:NAME>  # tier3-user netns egress;",
        "#                              # omit/null = legacy host net (no netns)",
        "#       launch:              # tier2-template only:",
        "#         workload: <str>    #   spawn-tier2 workload (seccomp/image)",
        "#         template_silo: <str>  # TIER2_SILO (binding to resolve)",
        "#         network: <none|slirp4netns>  # TIER2_NETWORK",
        "#         argv: [<str>, ...] #   app argv after `--`",
        "#",
        "# This file is regenerated atomically on every state change.",
        "# Hand-editing while qdistro-session-manager is running will be",
        "# overwritten; stop the service first.",
        "silos:",
    ]
    if not rows:
        out.append("  []")
    for r in rows:
        out.append(f"  - name: {r['name']}")
        out.append(f"    uid: {int(r['uid'])}")
        out.append(f"    state: {r['state']}")
        out.append(f"    autostart: {'true' if r['autostart'] else 'false'}")
        out.append(f"    created_at: {int(r['created_at'])}")
        out.append(f"    last_change: {int(r['last_change'])}")
        out.append(f"    kind: {r.get('kind', KIND_TIER3_USER)}")
        # egress is optional: omit the line entirely for the legacy
        # un-managed state (None) so existing rows round-trip unchanged.
        egress = r.get("egress")
        if egress is not None:
            out.append(f"    egress: {egress}")
        launch = r.get("launch") or {}
        if launch:
            out.append("    launch:")
            out.append(f"      workload: {launch['workload']}")
            out.append(f"      template_silo: {launch['template_silo']}")
            out.append(f"      network: {launch['network']}")
            # argv as a JSON array: PyYAML parses it as a list; the fallback
            # parser yields the text and validate_launch json.loads it.
            out.append(f"      argv: {json.dumps(launch.get('argv', []))}")
    if rows:
        # Make sure trailing newline so editors don't complain.
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# D-Bus service shim (optional import)
# ---------------------------------------------------------------------------

try:
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib
except ImportError:  # pragma: no cover - exercised on hosts without dbus
    dbus = None  # type: ignore[assignment]
    GLib = None  # type: ignore[assignment]


def _to_dbus_exception(e: SessionError):
    return dbus.DBusException(  # type: ignore[union-attr]
        str(e), name=f"{BUS_NAME}.{e.dbus_name}")


if dbus is not None:

    class SessionManager(dbus.service.Object):  # type: ignore[misc]
        """D-Bus shim. Methods enforce the two-layer caller-uid
        check pattern from the broker: the system-bus policy file
        restricts the bus-level surface, and the in-process
        _require_admin re-checks before any privileged op.
        """

        def __init__(self, bus, ops: _SystemOps | None = None,
                     config_path: Path = SILOS_CONFIG_PATH,
                     autostart: bool = True,
                     audit_path: Path = SILOS_AUDIT_PATH):
            super().__init__(bus, OBJ_PATH)
            self._bus = bus
            try:
                self.audit: _AuditLog | None = _AuditLog(audit_path)
            except Exception:  # noqa: BLE001
                # An unwritable audit DB must not take the daemon down —
                # log loudly and run without durable auditing rather than
                # refuse all lifecycle ops.
                log.exception("could not open session-manager audit log at "
                              "%s; running without durable audit", audit_path)
                self.audit = None
            self.store = _SiloStore(
                ops or _SystemOps(),
                config_path=config_path,
                on_change=self._emit_changed,
                audit=self.audit,
                # Live key custody: fetch wg private keys from qdistro-pwd
                # over D-Bus, pinned to root. A locked/pre-auth vault brings a
                # wg silo up dark rather than failing its start (task 3 C/B3).
                key_provider=_PwdKeyProvider(),
            )
            if autostart:
                self.store.autostart_pass()

        # ---- caller-uid guard -----------------------------------------

        def _peer_uid(self, sender, conn) -> int:
            try:
                bus_obj = conn.get_object("org.freedesktop.DBus",
                                          "/org/freedesktop/DBus")
                dbus_iface = dbus.Interface(bus_obj, "org.freedesktop.DBus")
                return int(dbus_iface.GetConnectionUnixUser(sender))
            except Exception as e:  # noqa: BLE001
                raise NotAuthorized(f"could not resolve caller uid: {e}")

        def _peer_caller(self, sender, conn) -> dict[str, Any]:
            """Resolve the calling peer's (uid, pid, exe) for the audit
            row. Best-effort beyond uid: a failure to read pid/exe must
            not block the privileged op (uid is already authoritative
            from _require_admin), so pid/exe degrade to None/"" rather
            than raising."""
            caller: dict[str, Any] = {"uid": None, "pid": None, "exe": ""}
            try:
                bus_obj = conn.get_object("org.freedesktop.DBus",
                                          "/org/freedesktop/DBus")
                dbus_iface = dbus.Interface(bus_obj, "org.freedesktop.DBus")
                caller["uid"] = int(dbus_iface.GetConnectionUnixUser(sender))
                pid = int(dbus_iface.GetConnectionUnixProcessID(sender))
                caller["pid"] = pid
                try:
                    caller["exe"] = os.readlink(f"/proc/{pid}/exe")
                except OSError:
                    caller["exe"] = ""
            except Exception as e:  # noqa: BLE001
                log.warning("could not fully resolve caller identity for "
                            "audit: %s", e)
            return caller

        def _require_admin(self, sender, conn) -> None:
            uid = self._peer_uid(sender, conn)
            if uid != ADMIN_UID:
                raise NotAuthorized(
                    f"caller uid {uid} is not ADMIN_UID={ADMIN_UID}")

        # ---- signals --------------------------------------------------

        @dbus.service.signal(BUS_NAME, signature="ss")
        def SiloChanged(self, name, state):
            pass

        def _emit_changed(self, name: str, state: str) -> None:
            # Called from the store under its lock; emit on the dbus
            # connection from the same thread (GLib main loop).
            self.SiloChanged(name, state)

        # ---- methods --------------------------------------------------

        def _audit_refusal(self, action, name, caller, exc):
            # Record an authorization refusal that happens BEFORE the
            # store call (e.g. NotAuthorized), so even rejected privileged
            # attempts leave a durable forensic trail. Store-level
            # refusals (UnknownSilo/BadState/SiloBusy) are already audited
            # inside the store.
            if self.audit is None:
                return
            try:
                self.audit.record(action, str(name), decision="deny",
                                  reason=str(exc), caller=caller)
            except Exception:  # noqa: BLE001
                log.exception("audit refusal record failed (action=%s "
                              "silo=%s)", action, name)

        @dbus.service.method(BUS_NAME, in_signature="si", out_signature="",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def CreateSilo(self, name, uid, sender=None, conn=None):
            caller = self._peer_caller(sender, conn)
            try:
                self._require_admin(sender, conn)
            except SessionError as e:
                self._audit_refusal("create", name, caller, e)
                raise _to_dbus_exception(e)
            try:
                self.store.create(str(name), int(uid), caller=caller)
                log.info("CreateSilo name=%s uid=%d", name, int(uid))
            except SessionError as e:
                raise _to_dbus_exception(e)

        @dbus.service.method(BUS_NAME, in_signature="ssss", out_signature="",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def CreateTemplateSilo(self, name, workload, template_silo, network,
                               sender=None, conn=None):
            """Create a tier2-template silo (fableplan2 task 04): launch-owner
            is admin, state is the binding's state_path (not a fresh user).
            argv defaults to [workload]; a richer argv is set via silos.yaml."""
            caller = self._peer_caller(sender, conn)
            try:
                self._require_admin(sender, conn)
            except SessionError as e:
                self._audit_refusal("create", name, caller, e)
                raise _to_dbus_exception(e)
            try:
                self.store.create(
                    str(name), ADMIN_UID, kind=KIND_TIER2_TEMPLATE,
                    launch={"workload": str(workload),
                            "template_silo": str(template_silo),
                            "network": str(network), "argv": []},
                    caller=caller)
                log.info("CreateTemplateSilo name=%s workload=%s silo=%s",
                         name, workload, template_silo)
            except SessionError as e:
                raise _to_dbus_exception(e)

        @dbus.service.method(BUS_NAME, in_signature="s", out_signature="",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def DeleteSilo(self, name, sender=None, conn=None):
            caller = self._peer_caller(sender, conn)
            try:
                self._require_admin(sender, conn)
            except SessionError as e:
                self._audit_refusal("delete", name, caller, e)
                raise _to_dbus_exception(e)
            try:
                self.store.delete(str(name), caller=caller)
                log.info("DeleteSilo name=%s", name)
            except SessionError as e:
                raise _to_dbus_exception(e)

        @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def SetSiloEgress(self, name, egress, sender=None, conn=None):
            """Set a tier3-user silo's per-silo netns egress policy
            (task 3). `egress` is "none" | "direct" | "wg:<name>", or the
            empty string to clear it back to legacy host networking (no
            netns). Admin-only; takes effect at the silo's next start."""
            caller = self._peer_caller(sender, conn)
            try:
                self._require_admin(sender, conn)
            except SessionError as e:
                self._audit_refusal("egress-configure", name, caller, e)
                raise _to_dbus_exception(e)
            try:
                # D-Bus has no null in a string arg; "" clears to legacy.
                policy = None if str(egress) == "" else str(egress)
                self.store.set_egress(str(name), policy, caller=caller)
                log.info("SetSiloEgress name=%s egress=%s", name, egress)
            except SessionError as e:
                raise _to_dbus_exception(e)

        @dbus.service.method(BUS_NAME, in_signature="s", out_signature="",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def StartSilo(self, name, sender=None, conn=None):
            caller = self._peer_caller(sender, conn)
            try:
                self._require_admin(sender, conn)
            except SessionError as e:
                self._audit_refusal("start", name, caller, e)
                raise _to_dbus_exception(e)
            try:
                self.store.start(str(name), caller=caller)
                log.info("StartSilo name=%s", name)
            except SessionError as e:
                raise _to_dbus_exception(e)

        @dbus.service.method(BUS_NAME, in_signature="si", out_signature="",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def StopSilo(self, name, grace_s, sender=None, conn=None):
            caller = self._peer_caller(sender, conn)
            try:
                self._require_admin(sender, conn)
            except SessionError as e:
                self._audit_refusal("stop", name, caller, e)
                raise _to_dbus_exception(e)
            try:
                self.store.stop(str(name), int(grace_s), caller=caller)
                log.info("StopSilo name=%s grace_s=%d", name, int(grace_s))
            except SessionError as e:
                raise _to_dbus_exception(e)

        @dbus.service.method(BUS_NAME, in_signature="s", out_signature="",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def FreezeSilo(self, name, sender=None, conn=None):
            caller = self._peer_caller(sender, conn)
            try:
                self._require_admin(sender, conn)
            except SessionError as e:
                self._audit_refusal("freeze", name, caller, e)
                raise _to_dbus_exception(e)
            try:
                self.store.freeze(str(name), caller=caller)
                log.info("FreezeSilo name=%s", name)
            except SessionError as e:
                raise _to_dbus_exception(e)

        @dbus.service.method(BUS_NAME, in_signature="s", out_signature="",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def ResumeSilo(self, name, sender=None, conn=None):
            caller = self._peer_caller(sender, conn)
            try:
                self._require_admin(sender, conn)
            except SessionError as e:
                self._audit_refusal("resume", name, caller, e)
                raise _to_dbus_exception(e)
            try:
                self.store.resume(str(name), caller=caller)
                log.info("ResumeSilo name=%s", name)
            except SessionError as e:
                raise _to_dbus_exception(e)

        @dbus.service.method(BUS_NAME, in_signature="", out_signature="s")
        def ListSilos(self):
            rows = [s.to_dict() for s in self.store.list_silos()]
            return json.dumps(rows)

        @dbus.service.method(BUS_NAME, in_signature="i", out_signature="s",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def ListAuditLog(self, limit, sender=None, conn=None):
            # Admin-only read of the durable lifecycle audit log, most
            # recent first. Mirrors the broker's ListHistory / pwd's
            # audit-tail surface so the admin tooling has one query
            # shape across daemons.
            try:
                self._require_admin(sender, conn)
            except SessionError as e:
                raise _to_dbus_exception(e)
            if self.audit is None:
                return json.dumps([])
            lim = int(limit) if int(limit) > 0 else 100
            return json.dumps(self.audit.tail(lim))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():  # pragma: no cover - exercised in the VM
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    if dbus is None:
        raise SystemExit("dbus-python not available")
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)
    SessionManager(name)
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
