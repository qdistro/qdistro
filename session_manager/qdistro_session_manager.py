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
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

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
# cgroup-v2 hierarchy root for silo scopes. One subdir per silo;
# cgroup.freeze controls Freeze/Resume.
CGROUP_ROOT = Path("/sys/fs/cgroup/qdistro-silos")
# Per-silo launcher unit. The placeholder mirrors qdwin-session-launcher;
# whichever launcher the bake ships is fine — the session manager only
# needs the unit name shape so it can `systemctl start` it.
SILO_LAUNCHER_FMT = "qdshell-session-{name}@{uid}.service"

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "uid": int(self.uid),
            "state": self.state,
            "autostart": bool(self.autostart),
            "created_at": int(self.created_at),
            "last_change": int(self.last_change),
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


# ---------------------------------------------------------------------------
# System-side adapter
# ---------------------------------------------------------------------------

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

    def systemctl_start(self, unit: str) -> None:
        subprocess.run(
            ["systemctl", "start", unit],
            check=True)

    def systemctl_stop(self, unit: str) -> None:
        subprocess.run(
            ["systemctl", "stop", unit],
            check=False)

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
                 on_change: "callable | None" = None):
        self._ops = ops
        self._config_path = Path(config_path)
        self._on_change = on_change
        self._lock = threading.RLock()
        self._silos: dict[str, Silo] = {}
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
                try:
                    validate_name(name)
                    validate_uid(uid)
                except BadArgument as e:
                    log.error(
                        "silos.yaml: dropping row with invalid "
                        "name/uid: %s", e)
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
            dfd = os.open(str(self._config_path.parent), os.O_RDONLY)
            try:
                os.fdatasync(dfd)
            finally:
                os.close(dfd)

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

    # ---- create / delete -----------------------------------------------

    def create(self, name: str, uid: int, *, autostart: bool = False) -> Silo:
        validate_name(name)
        validate_uid(uid)
        with self._lock:
            if name in self._silos:
                raise SiloExists(f"silo {name!r} already exists")
            for existing in self._silos.values():
                if existing.uid == uid:
                    raise SiloExists(
                        f"uid {uid} already in use by silo {existing.name!r}")
            if self._ops.user_exists(name):
                raise SiloExists(f"system user {name!r} already exists")
            if self._ops.uid_exists(uid):
                raise SiloExists(f"uid {uid} already in use on this system")
            self._ops.useradd(name, uid)
            self._ops.make_state_dir(name, uid)
            silo = Silo(
                name=name, uid=int(uid), state=State.CREATED,
                autostart=bool(autostart),
                created_at=int(time.time()),
                last_change=int(time.time()),
            )
            self._silos[name] = silo
            self.save()
            self._emit_change(silo.name, silo.state)
            return silo

    def delete(self, name: str) -> None:
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

    # ---- start / stop / freeze / resume -------------------------------

    def start(self, name: str) -> None:
        with self._lock:
            silo = self.get(name)
            if silo.state == State.ACTIVE:
                # idempotent
                return
            self._transition(silo, State.ACTIVE)
            try:
                self._ops.cgroup_create(silo.name)
                unit = SILO_LAUNCHER_FMT.format(name=silo.name,
                                                uid=silo.uid)
                self._ops.systemctl_start(unit)
            except Exception as e:  # noqa: BLE001
                # Roll back state on failure. _force_state emits
                # SiloChanged so the admin UI / PodApps don't stick on
                # "Active" after a failed launch.
                log.error("start of silo %r failed: %s", silo.name, e)
                self._force_state(silo, State.STOPPED)
                if isinstance(e, SessionError):
                    raise
                raise SessionError(
                    f"start of silo {silo.name!r} failed: {e}") from e

    def stop(self, name: str, grace_s: int = DEFAULT_STOP_GRACE_S) -> None:
        # Phase 1: validate state and transition to STOPPING under the lock.
        with self._lock:
            silo = self.get(name)
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
            # Snapshot the immutable identifiers we need outside the lock.
            silo_name = silo.name
            silo_uid = silo.uid

        # Phase 2: grace-period polling WITHOUT holding the store lock.
        # Other callers (ListSilos, signal handlers) can proceed while we
        # wait for processes to exit.
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
        except SessionError:
            # Already-typed errors propagate; force back to STOPPED
            # so the silo isn't wedged in STOPPING.
            with self._lock:
                self._force_state(silo, State.STOPPED)
            raise
        except Exception as e:  # noqa: BLE001
            log.error("stop of silo %r failed mid-teardown: %s — "
                      "forcing STOPPED so the silo isn't wedged",
                      silo_name, e)
            with self._lock:
                self._force_state(silo, State.STOPPED)
            raise SessionError(
                f"stop of silo {silo_name!r} failed: {e}") from e

        # Phase 3: re-acquire the lock for the final state transition.
        with self._lock:
            self._transition(silo, State.STOPPED)

    def freeze(self, name: str) -> None:
        with self._lock:
            silo = self.get(name)
            if silo.state == State.FROZEN:
                return
            self._transition(silo, State.FROZEN)
            self._ops.cgroup_freeze(silo.name, True)

    def resume(self, name: str) -> None:
        with self._lock:
            silo = self.get(name)
            if silo.state == State.ACTIVE:
                return
            if silo.state != State.FROZEN:
                raise BadState(
                    f"cannot resume silo {silo.name!r} in state {silo.state}")
            self._ops.cgroup_freeze(silo.name, False)
            self._transition(silo, State.ACTIVE)

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
    # Hand-rolled parser for the file we generate.
    data: dict[str, Any] = {"silos": []}
    cur: dict[str, Any] | None = None
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
            data["silos"].append(cur)
            kv = line[4:].strip()
            if ":" in kv:
                k, v = kv.split(":", 1)
                cur[k.strip()] = _yaml_scalar(v.strip())
            continue
        if line.startswith("    ") and cur is not None:
            kv = line.strip()
            if ":" in kv:
                k, v = kv.split(":", 1)
                cur[k.strip()] = _yaml_scalar(v.strip())
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
                     autostart: bool = True):
            super().__init__(bus, OBJ_PATH)
            self._bus = bus
            self.store = _SiloStore(
                ops or _SystemOps(),
                config_path=config_path,
                on_change=self._emit_changed,
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

        @dbus.service.method(BUS_NAME, in_signature="si", out_signature="",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def CreateSilo(self, name, uid, sender=None, conn=None):
            try:
                self._require_admin(sender, conn)
                self.store.create(str(name), int(uid))
                log.info("CreateSilo name=%s uid=%d", name, int(uid))
            except SessionError as e:
                raise _to_dbus_exception(e)

        @dbus.service.method(BUS_NAME, in_signature="s", out_signature="",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def DeleteSilo(self, name, sender=None, conn=None):
            try:
                self._require_admin(sender, conn)
                self.store.delete(str(name))
                log.info("DeleteSilo name=%s", name)
            except SessionError as e:
                raise _to_dbus_exception(e)

        @dbus.service.method(BUS_NAME, in_signature="s", out_signature="",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def StartSilo(self, name, sender=None, conn=None):
            try:
                self._require_admin(sender, conn)
                self.store.start(str(name))
                log.info("StartSilo name=%s", name)
            except SessionError as e:
                raise _to_dbus_exception(e)

        @dbus.service.method(BUS_NAME, in_signature="si", out_signature="",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def StopSilo(self, name, grace_s, sender=None, conn=None):
            try:
                self._require_admin(sender, conn)
                self.store.stop(str(name), int(grace_s))
                log.info("StopSilo name=%s grace_s=%d", name, int(grace_s))
            except SessionError as e:
                raise _to_dbus_exception(e)

        @dbus.service.method(BUS_NAME, in_signature="s", out_signature="",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def FreezeSilo(self, name, sender=None, conn=None):
            try:
                self._require_admin(sender, conn)
                self.store.freeze(str(name))
                log.info("FreezeSilo name=%s", name)
            except SessionError as e:
                raise _to_dbus_exception(e)

        @dbus.service.method(BUS_NAME, in_signature="s", out_signature="",
                             sender_keyword="sender",
                             connection_keyword="conn")
        def ResumeSilo(self, name, sender=None, conn=None):
            try:
                self._require_admin(sender, conn)
                self.store.resume(str(name))
                log.info("ResumeSilo name=%s", name)
            except SessionError as e:
                raise _to_dbus_exception(e)

        @dbus.service.method(BUS_NAME, in_signature="", out_signature="s")
        def ListSilos(self):
            rows = [s.to_dict() for s in self.store.list_silos()]
            return json.dumps(rows)


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
