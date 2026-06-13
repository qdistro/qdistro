"""Unit tests for the session-manager state machine and silo store.

The store is exercised against a `_FakeOps` adapter that records
useradd/userdel/cgroup calls and pretends success. The pure state
machine + persistence file are real; no dbus or root needed.

Mirrors the `_StubBroker` pattern in test_broker_sendto.py — the
production class is run as-is, only the side-effecting boundary is
swapped.
"""
from __future__ import annotations

import json
import os
import signal
import time
import types
from pathlib import Path

import pytest

import qdistro_session_manager as sm
from qdistro_session_manager import (
    _STATE_TRANSITIONS,
    BadArgument,
    BadState,
    SiloBusy,
    SiloExists,
    State,
    UnknownSilo,
    _SiloStore,
    validate_name,
    validate_uid,
)

# ---------------------------------------------------------------------------
# Fake side-effect adapter
# ---------------------------------------------------------------------------

class _FakeOps:
    def __init__(self):
        self.users: dict[str, int] = {}        # name → uid
        self.state_dirs: dict[str, tuple[int, int, int]] = {}  # name → (uid, gid, mode)
        self.cgroups: set[str] = set()
        self.cgroup_pids_map: dict[str, list[int]] = {}
        self.cgroup_frozen: dict[str, bool] = {}
        # Names whose cgroup_remove should raise EBUSY (simulates a task
        # stuck in D-state at stop time → the rmdir leaks the dir).
        self.cgroup_remove_ebusy: set[str] = set()
        # When True, cgroup_freeze raises (simulates a wedged kernel write).
        self.cgroup_freeze_should_fail = False
        self.systemctl_calls: list[tuple[str, str]] = []
        self.launch_envs: dict[str, str] = {}   # name → env file content
        self.killed: list[tuple[int, int]] = []
        self.useradd_should_fail = False
        self.userdel_should_fail = False
        # Snapshot of self.cgroup_frozen taken on each kill_pids call.
        self.frozen_at_kill: dict[str, bool] = {}
        # When True, a tier-2 stop's fail-closed verification reports the
        # container/unit still running (simulates an ExecStop regression or a
        # surviving rootless container) so the store must NOT report STOPPED.
        self.tier2_stop_fails = False
        # When True, kill_pids drains cgroup_pids_map for that cgroup
        # (simulates the launcher exiting after SIGTERM). Tests that
        # exercise the SIGKILL fallback set this False.
        self.kill_drains = True
        # Disposable (disp-*) containers in admin's rootless podman, and a
        # record of remove calls (for the Dispose teardown tests).
        self.disp_containers: list[str] = []
        self.disp_removed: list[str] = []
        # When True, disp_container_remove reports podman-rm failure (rc!=0);
        # when set to an exception, it raises that (simulates podman missing).
        self.disp_remove_fails = False
        self.disp_remove_raises: BaseException | None = None
        # token -> list of container names carrying that qdistro_tier2_token
        # label (for the DisposeByToken resolution tests). A list models the
        # podman filter result, including the should-never-happen >1 match.
        self.disp_token_map: dict[str, list[str]] = {}
        # When set to an exception, disp_containers_by_token raises it
        # (simulates a podman/runuser lookup failure — must fail closed, never
        # be read as "already gone").
        self.disp_token_lookup_raises: BaseException | None = None
        # name -> {"token","ttl","created"} lease metadata as podman labels
        # would carry it (strings), for the TTL-lease sweep tests. Only names
        # also present in disp_containers are enumerated (mirrors the
        # qdistro_disposable=1 label filter). When set to an exception,
        # disp_lease_candidates raises it.
        self.disp_lease_meta: dict[str, dict[str, str]] = {}
        self.disp_lease_raises: BaseException | None = None
        # name -> {"token","proctree","created","grace"} for the process-tree
        # sweep candidate read (mirrors the qdistro_lease_proctree=1 filter: only
        # names also flagged here as opted-in are enumerated). When set to an
        # exception, disp_proctree_candidates raises it.
        self.disp_proctree_meta: dict[str, dict[str, str]] = {}
        self.disp_proctree_raises: BaseException | None = None
        # name -> raw `podman top` output string the proctree predicate parses.
        # A missing name returns None (the real ops' fail-closed behaviour). When
        # set to an exception, disp_container_top_pids raises it.
        self.disp_top_output: dict[str, str | None] = {}
        self.disp_top_raises: BaseException | None = None
        self.disp_top_calls: list[str] = []
        # id -> list of container names carrying qdistro_lease_workflow=<id>
        # (for DisposeByWorkflow resolution). When set to an exception,
        # disp_containers_by_workflow raises it (lookup failure -> fail closed).
        self.disp_workflow_map: dict[str, list[str]] = {}
        self.disp_workflow_lookup_raises: BaseException | None = None
        self._egress_init()

    # ---- queries ----------------------------------------------------------

    def user_exists(self, name: str) -> bool:
        return name in self.users

    def uid_exists(self, uid: int) -> bool:
        return int(uid) in self.users.values()

    # ---- mutations --------------------------------------------------------

    def useradd(self, name: str, uid: int) -> None:
        if self.useradd_should_fail:
            import subprocess
            raise subprocess.CalledProcessError(1, ["useradd", name])
        self.users[name] = int(uid)

    def userdel(self, name: str) -> None:
        if self.userdel_should_fail:
            import subprocess
            raise subprocess.CalledProcessError(1, ["userdel", name])
        self.users.pop(name, None)

    # ---- disposable containers --------------------------------------------

    def disp_container_list(self) -> list[str]:
        return list(self.disp_containers)

    def disp_containers_by_token(self, token: str) -> list[str]:
        if self.disp_token_lookup_raises is not None:
            raise self.disp_token_lookup_raises
        # Mirror the real ops: only containers that are ALSO in disp_containers
        # (i.e. carry qdistro_disposable=1) resolve, so a token mapped to a
        # non-disposable name returns [] just as the dual label filter would.
        return [n for n in self.disp_token_map.get(token, [])
                if n in self.disp_containers]

    def disp_container_remove(self, name: str) -> bool:
        if self.disp_remove_raises is not None:
            raise self.disp_remove_raises
        self.disp_removed.append(name)
        if self.disp_remove_fails:
            return False
        if name in self.disp_containers:
            self.disp_containers.remove(name)
        return True

    def disp_lease_candidates(self) -> list[dict]:
        if self.disp_lease_raises is not None:
            raise self.disp_lease_raises
        out = []
        for name in self.disp_containers:
            meta = self.disp_lease_meta.get(name, {})
            # Absent labels come back as an empty field from real podman.
            out.append({"name": name,
                        "token": meta.get("token", ""),
                        "ttl": meta.get("ttl", ""),
                        "created": meta.get("created", "")})
        return out

    def disp_proctree_candidates(self) -> list[dict]:
        if self.disp_proctree_raises is not None:
            raise self.disp_proctree_raises
        out = []
        for name in self.disp_containers:
            meta = self.disp_proctree_meta.get(name)
            if meta is None:
                # Mirror the real --filter label=qdistro_lease_proctree=1:
                # only opted-in disposables are enumerated.
                continue
            out.append({"name": name,
                        "token": meta.get("token", ""),
                        "proctree": meta.get("proctree", "1"),
                        "created": meta.get("created", ""),
                        "grace": meta.get("grace", "")})
        return out

    def disp_container_top_pids(self, name: str):
        self.disp_top_calls.append(name)
        if self.disp_top_raises is not None:
            raise self.disp_top_raises
        return self.disp_top_output.get(name)

    def disp_containers_by_workflow(self, workflow_id: str) -> list[str]:
        if self.disp_workflow_lookup_raises is not None:
            raise self.disp_workflow_lookup_raises
        # Mirror the dual label filter: only containers also in disp_containers
        # (carrying qdistro_disposable=1) resolve.
        return [n for n in self.disp_workflow_map.get(workflow_id, [])
                if n in self.disp_containers]

    def make_state_dir(self, name: str, uid: int):
        self.state_dirs[name] = (int(uid), int(uid), 0o700)
        return Path("/var/lib/qdistro/silos") / name

    def remove_state_dir(self, name: str) -> None:
        self.state_dirs.pop(name, None)

    def cgroup_create(self, name: str):
        self.cgroups.add(name)
        self.cgroup_pids_map.setdefault(name, [])
        return Path("/sys/fs/cgroup/qdistro-silos") / name

    def cgroup_remove(self, name: str) -> None:
        # When this name is staged to fail, raise EBUSY (as the kernel does
        # when cgroup.procs is non-empty) so tests can exercise the leak path.
        if name in getattr(self, "cgroup_remove_ebusy", set()):
            import errno
            raise OSError(errno.EBUSY, "Device or resource busy")
        self.cgroups.discard(name)
        self.cgroup_pids_map.pop(name, None)
        self.cgroup_frozen.pop(name, None)

    def cgroup_list(self) -> list[str]:
        return list(self.cgroups)

    def cgroup_freeze(self, name: str, frozen: bool) -> None:
        if self.cgroup_freeze_should_fail:
            raise OSError("cgroup.freeze write failed (simulated)")
        self.cgroup_frozen[name] = bool(frozen)

    def cgroup_pids(self, name: str) -> list[int]:
        return list(self.cgroup_pids_map.get(name, []))

    def cgroup_is_populated(self, name: str) -> bool:
        return bool(self.cgroup_pids_map.get(name))

    def systemctl_start(self, unit: str) -> None:
        self.systemctl_calls.append(("start", unit))
        # Pretend the launcher spawned a PID inside the cgroup. The
        # test sets cgroup_pids_map[name] later if it cares.
        for name in list(self.cgroups):
            if unit.startswith(f"qdshell-session-{name}@"):
                self.cgroup_pids_map.setdefault(name, []).append(99000)

    def systemctl_stop(self, unit: str) -> None:
        self.systemctl_calls.append(("stop", unit))

    def tier2_silo_running(self, name: str) -> bool:
        # Fail-closed verification hook: True means the stop did NOT take
        # effect (unit still active or container survives).
        return self.tier2_stop_fails

    # tier2-template launch env (fableplan2 task 04)
    def write_launch_env(self, name: str, content: str):
        self.launch_envs[name] = content
        return Path("/run/qdistro/silo-launch") / f"{name}.env"

    def remove_launch_env(self, name: str) -> None:
        self.launch_envs.pop(name, None)

    def kill_pids(self, pids, sig: int) -> None:
        # Snapshot which cgroups are frozen at the moment we signal — a kill
        # against a frozen cgroup is undeliverable, so stop() must have thawed
        # first. Lets tests assert the pre-thaw actually landed before SIGTERM
        # (the post-stop frozen dict is cleared by cgroup_remove, so checking it
        # after the fact is vacuous).
        self.frozen_at_kill = dict(self.cgroup_frozen)
        for pid in pids:
            self.killed.append((int(pid), int(sig)))
        if self.kill_drains:
            for name in list(self.cgroup_pids_map.keys()):
                self.cgroup_pids_map[name] = []

    # ---- per-silo netns egress (todo/fable-networking task 3) -------------
    # Records the calls the EgressBackend makes so the lifecycle wiring
    # (create→start→stop→delete) can be asserted without root or real netns.

    def _egress_init(self):
        self.netns: set[str] = set()
        self.egress_calls: list[tuple] = []
        self.resolv: dict[str, list[str]] = {}
        self.skuid: dict[int, bool] = {}
        self.watchers: list[tuple] = []           # live link-up watchers

    def netns_create(self, ns: str) -> None:
        self.egress_calls.append(("netns_create", ns))
        self.netns.add(ns)

    def netns_remove(self, ns: str) -> None:
        self.egress_calls.append(("netns_remove", ns))
        self.netns.discard(ns)

    def netns_exists(self, ns: str) -> bool:
        return ns in self.netns

    def link_up(self, ns, ifname):
        self.egress_calls.append(("link_up", ns, ifname))

    def link_del(self, ns, ifname):
        self.egress_calls.append(("link_del", ns, ifname))

    def link_set_netns(self, ifname, ns):
        self.egress_calls.append(("link_set_netns", ifname, ns))

    def addr_add(self, ns, ifname, address):
        self.egress_calls.append(("addr_add", ns, ifname, address))

    def route_add_default_dev(self, ns, ifname):
        self.egress_calls.append(("route_add_default_dev", ns, ifname))

    def route_add_default_via(self, ns, gw):
        self.egress_calls.append(("route_add_default_via", ns, gw))

    def ipv6_disable(self, ns, ifname):
        self.egress_calls.append(("ipv6_disable", ns, ifname))

    def wg_add_dev(self, ifname):
        self.egress_calls.append(("wg_add_dev", ifname))

    def wg_configure(self, ifname, **kw):
        self.egress_calls.append(("wg_configure", ifname, kw))

    def veth_create(self, host_if, peer_if):
        self.egress_calls.append(("veth_create", host_if, peer_if))

    def nat_masquerade(self, subnet, enable):
        self.egress_calls.append(("nat_masquerade", subnet, enable))

    def enable_ip_forward(self):
        self.egress_calls.append(("enable_ip_forward",))

    def dns_start(self, ns, host_if, host_ip):
        self.egress_calls.append(("dns_start", ns, host_if, host_ip))

    def dns_stop(self, ns):
        self.egress_calls.append(("dns_stop", ns))

    def start_link_watcher(self, ns, ifname, on_up):
        self.egress_calls.append(("start_link_watcher", ns, ifname))
        handle = ("watch", ns, ifname, on_up)
        self.watchers.append(handle)
        return handle

    def stop_link_watcher(self, handle):
        self.egress_calls.append(("stop_link_watcher",))
        if handle in self.watchers:
            self.watchers.remove(handle)

    def nft_skuid_drop(self, uid, enable):
        self.egress_calls.append(("nft_skuid_drop", uid, enable))
        self.skuid[int(uid)] = bool(enable)

    def write_netns_resolv(self, ns, nameservers):
        self.egress_calls.append(("write_netns_resolv", ns, list(nameservers)))
        self.resolv[ns] = list(nameservers)

    def remove_netns_resolv(self, ns):
        self.egress_calls.append(("remove_netns_resolv", ns))
        self.resolv.pop(ns, None)

    def egress_ops(self) -> list[str]:
        return [c[0] for c in self.egress_calls]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ops() -> _FakeOps:
    return _FakeOps()


@pytest.fixture
def store(ops: _FakeOps, tmp_path: Path) -> _SiloStore:
    return _SiloStore(ops, config_path=tmp_path / "silos.yaml")


@pytest.fixture
def signals():
    return []


@pytest.fixture
def store_with_signals(ops: _FakeOps, tmp_path: Path, signals):
    return _SiloStore(
        ops, config_path=tmp_path / "silos.yaml",
        on_change=lambda n, s: signals.append((n, s)),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_valid_name(self):
        assert validate_name("work") == "work"

    @pytest.mark.parametrize("bad", [
        "Work", "1work", "work!", "work space", "",
        "x" * 33, "admin", "root", "qdistro",
    ])
    def test_invalid_name(self, bad):
        with pytest.raises(BadArgument):
            validate_name(bad)

    def test_uid_in_range(self):
        assert validate_uid(2000) == 2000
        assert validate_uid(60000) == 60000

    @pytest.mark.parametrize("bad", [0, 100, 1999, 60001, -1, "x"])
    def test_uid_out_of_range(self, bad):
        with pytest.raises(BadArgument):
            validate_uid(bad)


# ---------------------------------------------------------------------------
# Create / delete
# ---------------------------------------------------------------------------

class TestCreate:
    def test_create_writes_state_dir_and_user(self, store, ops):
        silo = store.create("work", 2000)
        assert silo.name == "work"
        assert silo.uid == 2000
        assert silo.state == State.CREATED
        assert ops.users == {"work": 2000}
        assert ops.state_dirs["work"] == (2000, 2000, 0o700)

    def test_create_emits_signal(self, store_with_signals, signals):
        store_with_signals.create("work", 2000)
        assert ("work", State.CREATED) in signals

    def test_create_duplicate_name(self, store):
        store.create("work", 2000)
        with pytest.raises(SiloExists):
            store.create("work", 2001)

    def test_create_duplicate_uid(self, store):
        store.create("work", 2000)
        with pytest.raises(SiloExists):
            store.create("other", 2000)

    def test_create_system_user_exists(self, store, ops):
        ops.users["work"] = 999
        with pytest.raises(SiloExists):
            store.create("work", 2000)

    def test_create_persists_to_yaml(self, store, tmp_path):
        store.create("work", 2000)
        text = (tmp_path / "silos.yaml").read_text()
        assert "name: work" in text
        assert "uid: 2000" in text
        assert "state: Created" in text


class TestDelete:
    def test_delete_refuses_active(self, store):
        store.create("work", 2000)
        store.start("work")
        with pytest.raises(SiloBusy):
            store.delete("work")

    def test_delete_refuses_frozen(self, store):
        store.create("work", 2000)
        store.start("work")
        store.freeze("work")
        with pytest.raises(SiloBusy):
            store.delete("work")

    def test_delete_after_stop(self, store, ops):
        store.create("work", 2000)
        store.start("work")
        store.stop("work")
        store.delete("work")
        assert "work" not in ops.users
        assert "work" not in ops.state_dirs
        with pytest.raises(UnknownSilo):
            store.get("work")

    def test_delete_from_created_state(self, store, ops):
        store.create("work", 2000)
        store.delete("work")
        assert "work" not in ops.users

    def test_delete_unknown(self, store):
        with pytest.raises(UnknownSilo):
            store.delete("nonexistent")


# ---------------------------------------------------------------------------
# Start / stop / freeze / resume
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_start_creates_cgroup_and_unit(self, store, ops):
        store.create("work", 2000)
        store.start("work")
        assert "work" in ops.cgroups
        assert any(call == ("start", "qdshell-session-work@2000.service")
                   for call in ops.systemctl_calls)
        assert store.get("work").state == State.ACTIVE

    def test_start_is_idempotent(self, store, ops):
        store.create("work", 2000)
        store.start("work")
        store.start("work")  # no-op, no second systemctl call
        starts = [c for c in ops.systemctl_calls if c[0] == "start"]
        assert len(starts) == 1

    def test_freeze_then_resume(self, store, ops):
        store.create("work", 2000)
        store.start("work")
        store.freeze("work")
        assert store.get("work").state == State.FROZEN
        assert ops.cgroup_frozen["work"] is True
        store.resume("work")
        assert store.get("work").state == State.ACTIVE
        assert ops.cgroup_frozen["work"] is False

    def test_freeze_idempotent(self, store):
        store.create("work", 2000)
        store.start("work")
        store.freeze("work")
        store.freeze("work")  # no-op
        assert store.get("work").state == State.FROZEN

    def test_resume_from_active_is_idempotent(self, store):
        store.create("work", 2000)
        store.start("work")
        store.resume("work")
        assert store.get("work").state == State.ACTIVE

    def test_resume_from_stopped_rejected(self, store):
        store.create("work", 2000)
        store.start("work")
        store.stop("work")
        with pytest.raises(BadState):
            store.resume("work")

    def test_stop_sends_sigterm(self, store, ops):
        store.create("work", 2000)
        store.start("work")
        # systemctl_start put PID 99000 in the cgroup.
        store.stop("work", grace_s=0)
        sigterms = [k for k in ops.killed if k[1] == signal.SIGTERM]
        assert sigterms, "expected at least one SIGTERM"
        assert store.get("work").state == State.STOPPED

    def test_stop_sigkill_after_grace(self, store, ops):
        store.create("work", 2000)
        store.start("work")
        # Pretend the process refuses to exit on SIGTERM.
        ops.kill_drains = False
        store.stop("work", grace_s=0)
        sigkills = [k for k in ops.killed if k[1] == signal.SIGKILL]
        assert sigkills, "expected at least one SIGKILL after grace"

    def test_stop_unfreezes_first(self, store, ops):
        store.create("work", 2000)
        store.start("work")
        store.freeze("work")
        store.stop("work", grace_s=0)
        assert ops.cgroup_frozen.get("work", False) is False
        assert store.get("work").state == State.STOPPED


# ---------------------------------------------------------------------------
# 02/S14 — orphan-cgroup reaping + freeze/resume lock scope
# ---------------------------------------------------------------------------

class TestS14OrphanCgroup:
    def test_stop_ebusy_leaks_then_startup_reaps(self, ops, tmp_path):
        """02/S14a: a stop() whose cgroup rmdir hits EBUSY leaves the dir
        behind (forcing STOPPED), and the next startup sweep reclaims it once
        the tasks have exited. The old comment claimed autostart papered it
        over, but autostart only STARTS silos — nothing reaped the dir."""
        cfg = tmp_path / "silos.yaml"
        store = _SiloStore(ops, config_path=cfg)
        store.create("work", 2000)
        store.start("work")
        assert "work" in ops.cgroups
        # Stop, but the rmdir hits EBUSY (a task stuck in D at stop time).
        ops.cgroup_remove_ebusy = {"work"}
        store.stop("work", grace_s=0)
        assert store.get("work").state == State.STOPPED
        assert "work" in ops.cgroups, "the cgroup dir leaked (expected)"

        # Daemon restart: the stuck task is gone, the dir is now unpopulated.
        ops.cgroup_remove_ebusy = set()
        ops.cgroup_pids_map["work"] = []
        store2 = _SiloStore(ops, config_path=cfg)
        reaped = store2.reap_orphan_cgroups()
        assert "work" in reaped
        assert "work" not in ops.cgroups

    def test_reap_skips_live_and_populated(self, ops, tmp_path):
        store = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        store.create("live", 2000)
        store.start("live")                       # ACTIVE → must not be reaped
        ops.cgroups.add("ghost")                  # orphan, still has a task
        ops.cgroup_pids_map["ghost"] = [4321]
        ops.cgroups.add("dead")                   # orphan, empty → reap
        ops.cgroup_pids_map["dead"] = []
        reaped = store.reap_orphan_cgroups()
        assert reaped == ["dead"]
        assert "live" in ops.cgroups
        assert "ghost" in ops.cgroups

    def test_autostart_runs_the_sweep(self, ops, tmp_path):
        store = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        ops.cgroups.add("dead")                   # leftover from a prior boot
        ops.cgroup_pids_map["dead"] = []
        store.autostart_pass()
        assert "dead" not in ops.cgroups


class TestS14FreezeLockScope:
    def test_freeze_write_failure_leaves_active(self, store, ops):
        """02/S14b: the cgroup.freeze write runs outside the store lock and the
        ACTIVE→FROZEN transition is deferred until it succeeds; a failed write
        leaves the silo cleanly ACTIVE (it never lies about being FROZEN)."""
        store.create("work", 2000)
        store.start("work")
        ops.cgroup_freeze_should_fail = True
        with pytest.raises(OSError):
            store.freeze("work")
        assert store.get("work").state == State.ACTIVE

    def test_resume_write_failure_leaves_frozen(self, store, ops):
        """Symmetric: a failed unfreeze write leaves the silo FROZEN."""
        store.create("work", 2000)
        store.start("work")
        store.freeze("work")
        ops.cgroup_freeze_should_fail = True
        with pytest.raises(OSError):
            store.resume("work")
        assert store.get("work").state == State.FROZEN

    def test_stop_thaws_even_when_state_says_active(self, store, ops):
        """02/S14b (codex r3 edge): stop() thaws the cgroup unconditionally, so
        a silo whose physical cgroup is frozen while store state reads ACTIVE
        (e.g. a freeze() whose write landed but whose save() then failed) still
        gets thawed before SIGTERM, never SIGTERMing a frozen, undeliverable
        process set."""
        store.create("work", 2000)
        store.start("work")
        ops.cgroup_frozen["work"] = True          # divergence: frozen, ACTIVE
        store.stop("work", grace_s=0)
        # Load-bearing: the cgroup must have been thawed BEFORE the kill (a
        # frozen process never receives the signal). Checking cgroup_frozen
        # after stop is vacuous — cgroup_remove clears it regardless.
        assert ops.frozen_at_kill.get("work") is False
        assert store.get("work").state == State.STOPPED

    def test_freeze_reentrant_stop_from_on_change(self, ops, tmp_path):
        """02/S14b re-entrancy (codex round-2): if an on_change handler
        re-enters stop() when it observes FROZEN, the freeze's cgroup write
        must already have completed and the in-flight claim must already be
        cleared — so the re-entrant stop neither deadlocks nor tears down a
        half-frozen silo. Regression guard: with the transition emitted before
        the write (or before the claim is cleared), frozen_write_seen would be
        None/False here and the silo would be torn down mid-write."""
        observed: dict = {}
        store = None  # captured by the closure; reassigned below before use

        def on_change(n, s):
            if s == State.FROZEN and not observed.get("done"):
                observed["frozen_write_seen"] = ops.cgroup_frozen.get(n)
                observed["done"] = True
                store.stop(n, grace_s=0)      # re-enter on the same thread

        store = _SiloStore(ops, config_path=tmp_path / "silos.yaml",
                           on_change=on_change)
        store.create("work", 2000)
        store.start("work")
        # Run in a worker so a clear/transition-ordering regression (which
        # deadlocks on _stop_cv.wait()) surfaces as a failed join, not a hang
        # that only the CI job timeout would catch.
        import threading
        t = threading.Thread(target=lambda: store.freeze("work"))
        t.start()
        t.join(10)
        assert not t.is_alive(), "freeze() deadlocked under re-entrant stop()"
        assert observed.get("frozen_write_seen") is True
        assert store.get("work").state == State.STOPPED

    def test_lock_released_during_freeze_write(self, store, ops):
        """The blocking cgroup.freeze write must NOT hold the store lock: a
        wedged write may not stall read-only callers like list_silos()."""
        import threading
        store.create("work", 2000)
        store.start("work")
        in_write = threading.Event()
        release = threading.Event()
        orig = ops.cgroup_freeze

        def blocking(name, frozen):
            in_write.set()
            assert release.wait(5), "test deadlock: writer never released"
            return orig(name, frozen)

        ops.cgroup_freeze = blocking
        t = threading.Thread(target=lambda: store.freeze("work"))
        t.start()
        try:
            assert in_write.wait(5), "freeze never reached the cgroup write"
            # The writer is parked inside cgroup_freeze. A lock-taking read
            # must still complete promptly — proving _lock is not held.
            done = threading.Event()
            threading.Thread(
                target=lambda: (store.list_silos(), done.set())).start()
            assert done.wait(5), "list_silos blocked → lock held during write"
        finally:
            release.set()
            t.join(5)
        assert store.get("work").state == State.FROZEN

    def test_stop_waits_for_inflight_freeze(self, store, ops):
        """02/S14b serialization: with freeze() parked inside its lock-free
        cgroup.freeze write, a concurrent stop() for the same silo must BLOCK
        on the in-flight claim until freeze finishes — never tear the silo
        down mid-freeze (the TOCTOU both reviewers flagged)."""
        import threading
        store.create("work", 2000)
        store.start("work")
        in_write = threading.Event()
        release = threading.Event()
        orig = ops.cgroup_freeze

        def parked(name, frozen):
            if frozen:                      # park only the freeze(True) write
                in_write.set()
                assert release.wait(5), "test deadlock: freeze never released"
            return orig(name, frozen)

        ops.cgroup_freeze = parked
        tf = threading.Thread(target=lambda: store.freeze("work"))
        tf.start()
        assert in_write.wait(5), "freeze never reached the cgroup write"

        stop_done = threading.Event()
        threading.Thread(target=lambda: (
            store.stop("work", grace_s=0), stop_done.set())).start()
        # stop() must be parked on the in-flight claim while freeze holds it.
        assert not stop_done.wait(0.5), "stop() raced an in-flight freeze"

        release.set()
        tf.join(5)
        assert stop_done.wait(5), "stop() never completed after freeze released"
        assert store.get("work").state == State.STOPPED


# ---------------------------------------------------------------------------
# State-machine table
# ---------------------------------------------------------------------------

class TestStateMachine:
    def test_illegal_freeze_from_created(self, store):
        store.create("work", 2000)
        with pytest.raises(BadState):
            store.freeze("work")

    def test_illegal_stop_from_created(self, store):
        store.create("work", 2000)
        with pytest.raises(BadState):
            store.stop("work")

    def test_transitions_table_pinned(self):
        # Pin the table so an accidental relaxation surfaces as a
        # test failure. If the design changes, update this assertion.
        assert _STATE_TRANSITIONS[State.CREATED] == frozenset(
            (State.ACTIVE, State.DELETING))
        assert _STATE_TRANSITIONS[State.ACTIVE] == frozenset(
            (State.FROZEN, State.STOPPING, State.STOPPED))
        assert _STATE_TRANSITIONS[State.FROZEN] == frozenset(
            (State.ACTIVE, State.STOPPING, State.STOPPED))
        assert _STATE_TRANSITIONS[State.STOPPING] == frozenset(
            (State.STOPPED,))
        assert _STATE_TRANSITIONS[State.STOPPED] == frozenset(
            (State.ACTIVE, State.DELETING))


# ---------------------------------------------------------------------------
# Persistence + autostart
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_yaml_round_trip(self, ops, tmp_path):
        s1 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        s1.create("work", 2000)
        s1.start("work")
        # New store on the same path picks up the saved file.
        s2 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        loaded = s2.get("work")
        assert loaded.uid == 2000
        assert loaded.state == State.ACTIVE

    def test_yaml_has_schema_comment(self, store, tmp_path):
        store.create("work", 2000)
        text = (tmp_path / "silos.yaml").read_text()
        assert "qdistro-session-manager persistence file" in text
        assert "Schema:" in text
        assert "autostart:" in text

    def test_autostart_pass_starts_marked_silos(self, ops, tmp_path):
        s1 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        s1.create("work", 2000, autostart=True)
        s1.create("scratch", 2001, autostart=False)
        # Simulate reboot: drop the in-memory state, reload from disk.
        ops.cgroups.clear()
        ops.cgroup_pids_map.clear()
        ops.systemctl_calls.clear()
        s2 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        started = s2.autostart_pass()
        assert "work" in started
        assert "scratch" not in started
        assert s2.get("work").state == State.ACTIVE
        # scratch was never started, autostart=False → stays Created.
        assert s2.get("scratch").state == State.CREATED

    def test_active_silo_restarts_across_reboot(self, ops, tmp_path):
        s1 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        s1.create("work", 2000)
        s1.start("work")  # Active gets persisted
        # Reboot: cgroups gone, but the state file still says Active.
        ops.cgroups.clear()
        ops.cgroup_pids_map.clear()
        ops.systemctl_calls.clear()
        s2 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        started = s2.autostart_pass()
        assert "work" in started


# ---------------------------------------------------------------------------
# Review fixups: H1/H2/H3/SEC-H2 regression coverage
# ---------------------------------------------------------------------------

class TestReviewFixups:
    def test_start_rollback_emits_signal(self, ops, tmp_path, signals):
        """H2 — failed start() must emit SiloChanged(Stopped) after
        rolling back so the admin UI doesn't stick on Active."""
        store = _SiloStore(
            ops, config_path=tmp_path / "silos.yaml",
            on_change=lambda n, s: signals.append((n, s)),
        )
        store.create("work", 2000)
        # Force systemctl_start to fail.
        import subprocess

        def _boom(unit):
            raise subprocess.CalledProcessError(1, ["systemctl", "start", unit])

        ops.systemctl_start = _boom
        with pytest.raises(sm.SessionError):
            store.start("work")
        # Saw Active (the optimistic transition) then Stopped (rollback).
        states = [s for (n, s) in signals if n == "work"]
        assert State.ACTIVE in states
        assert states[-1] == State.STOPPED
        assert store.get("work").state == State.STOPPED

    def test_delete_rollback_emits_signal(self, ops, tmp_path, signals):
        """H2 — failed delete() (e.g. userdel error) rolls back to
        Stopped via _force_state, emitting SiloChanged."""
        store = _SiloStore(
            ops, config_path=tmp_path / "silos.yaml",
            on_change=lambda n, s: signals.append((n, s)),
        )
        store.create("work", 2000)
        store.start("work")
        store.stop("work", grace_s=0)
        ops.userdel_should_fail = True
        with pytest.raises(sm.SessionError):
            store.delete("work")
        # Saw Deleting then Stopped (rollback).
        states = [s for (n, s) in signals if n == "work"]
        assert State.DELETING in states
        assert states[-1] == State.STOPPED
        assert store.get("work").state == State.STOPPED

    def test_stop_recovers_from_mid_teardown_error(self, store, ops):
        """H3 — if cgroup_pids raises, stop() must not wedge the silo
        in STOPPING (which has no recovery path)."""
        store.create("work", 2000)
        store.start("work")

        # Make cgroup_pids blow up after the transition to STOPPING.
        def _boom(name):
            raise OSError("synthetic procfs corruption")

        ops.cgroup_pids = _boom
        # stop() should still drive the silo to STOPPED via _force_state.
        # It might raise; either way, the silo is no longer wedged.
        try:
            store.stop("work", grace_s=0)
        except sm.SessionError:
            pass
        assert store.get("work").state == State.STOPPED

    def test_concurrent_stop_both_observe_final_state(self, store, ops):
        """Round-2 review — a second stop() that races with an in-flight
        stop must wait for the in-flight teardown to finish and observe
        the final Stopped state, not return success prematurely."""
        import threading
        import time as _t

        store.create("work", 2000)
        store.start("work")
        assert store.get("work").state == State.ACTIVE

        # Make phase-2 teardown slow so the second caller is forced to
        # wait on the in-flight slot. `in_teardown` fires the instant t1 has
        # entered the slow phase-2 stop, giving t2 a deterministic readiness
        # signal to start (replaces a fixed `time.sleep(0.05)` that only
        # *probably* ordered t1 first). The 0.3s here is the injected work the
        # race is built around, not a scheduling guess — kept as-is.
        orig_stop = ops.systemctl_stop
        in_teardown = threading.Event()

        def _slow_stop(unit):
            in_teardown.set()
            _t.sleep(0.3)
            orig_stop(unit)

        ops.systemctl_stop = _slow_stop

        results: list[tuple[str, str]] = []
        rlock = threading.Lock()

        def _do_stop():
            try:
                store.stop("work", grace_s=1)
                with rlock:
                    results.append(("ok", store.get("work").state))
            except Exception as e:  # noqa: BLE001
                with rlock:
                    results.append(("err", repr(e)))

        t1 = threading.Thread(target=_do_stop)
        t2 = threading.Thread(target=_do_stop)
        t1.start()
        # Wait until t1 is actually inside phase-2 teardown before starting
        # t2, so t2 deterministically races an in-flight stop.
        assert in_teardown.wait(timeout=5), "t1 never entered phase-2 teardown"
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(results) == 2
        assert all(r[0] == "ok" for r in results), results
        # Both callers must observe the silo at its final Stopped state.
        assert all(r[1] == State.STOPPED for r in results), results
        assert store.get("work").state == State.STOPPED

    def test_stop_reentrant_from_on_change_does_not_deadlock(
            self, ops, tmp_path):
        """Round-3 review — the final Stopped on_change must fire AFTER the
        in-flight marker is cleared, so a callback that re-enters stop() on
        the same thread doesn't deadlock waiting on a marker only this
        thread can clear."""
        store_ref: dict = {}
        reentered: list[bool] = []

        def on_change(name, state):
            if name == "work" and state == State.STOPPED and not reentered:
                reentered.append(True)
                # Re-enter stop() on the same thread from the callback.
                store_ref["store"].stop("work", grace_s=0)

        store = _SiloStore(
            ops, config_path=tmp_path / "silos.yaml", on_change=on_change)
        store_ref["store"] = store
        store.create("work", 2000)
        store.start("work")
        # Must return without hanging.
        store.stop("work", grace_s=0)
        assert reentered == [True]
        assert store.get("work").state == State.STOPPED

    def test_load_drops_row_with_invalid_name(self, ops, tmp_path, caplog):
        """SEC-H2 — hand-edited silos.yaml with a path-traversal name
        must be rejected at load(), not silently accepted."""
        cfg = tmp_path / "silos.yaml"
        cfg.write_text(
            "silos:\n"
            '  - name: "../etc/passwd"\n'
            "    uid: 2000\n"
            "    state: Stopped\n"
            "    autostart: false\n"
            "    created_at: 0\n"
            "    last_change: 0\n"
        )
        import logging as _logging
        with caplog.at_level(_logging.ERROR):
            store = _SiloStore(ops, config_path=cfg)
        assert store.list_silos() == []
        assert any("invalid" in r.message.lower() or
                   "dropping" in r.message.lower()
                   for r in caplog.records)

    def test_load_drops_row_with_reserved_name(self, ops, tmp_path, caplog):
        """SEC-H2 — reserved names get the same treatment."""
        cfg = tmp_path / "silos.yaml"
        cfg.write_text(
            "silos:\n"
            "  - name: root\n"
            "    uid: 2000\n"
            "    state: Stopped\n"
            "    autostart: false\n"
            "    created_at: 0\n"
            "    last_change: 0\n"
        )
        import logging as _logging
        with caplog.at_level(_logging.ERROR):
            store = _SiloStore(ops, config_path=cfg)
        assert store.list_silos() == []

    def test_load_drops_row_with_bad_uid_range(self, ops, tmp_path):
        """SEC-H2 — out-of-range uid gets dropped, valid rows survive."""
        cfg = tmp_path / "silos.yaml"
        cfg.write_text(
            "silos:\n"
            "  - name: bogus\n"
            "    uid: 50\n"
            "    state: Stopped\n"
            "    autostart: false\n"
            "    created_at: 0\n"
            "    last_change: 0\n"
            "  - name: good\n"
            "    uid: 2000\n"
            "    state: Stopped\n"
            "    autostart: false\n"
            "    created_at: 0\n"
            "    last_change: 0\n"
        )
        store = _SiloStore(ops, config_path=cfg)
        names = [s.name for s in store.list_silos()]
        assert names == ["good"]


# ---------------------------------------------------------------------------
# tier2-template silos (fableplan2 task 04)
# ---------------------------------------------------------------------------

_LAUNCH = {
    "workload": "browser",
    "template_silo": "browser1",
    "network": "slirp4netns",
    "argv": ["chromium"],
}


class TestTier2TemplateKind:
    def test_validate_kind(self):
        assert sm.validate_kind("tier3-user") == "tier3-user"
        assert sm.validate_kind("tier2-template") == "tier2-template"
        with pytest.raises(BadArgument):
            sm.validate_kind("bogus")

    def test_tier2_uid_must_be_admin(self):
        admin_uid = sm.TIER2_LAUNCH_OWNER_UID
        assert sm.validate_silo_uid(admin_uid, "tier2-template") == admin_uid
        with pytest.raises(BadArgument):
            sm.validate_silo_uid(admin_uid + 1, "tier2-template")
        # tier3 still uses the silo range; admin uid is rejected there.
        assert sm.validate_silo_uid(2001, "tier3-user") == 2001
        with pytest.raises(BadArgument):
            sm.validate_silo_uid(admin_uid, "tier3-user")

    def test_validate_launch_rejects_tier3_stanza(self):
        assert sm.validate_launch("tier3-user", {}) == {}
        with pytest.raises(BadArgument):
            sm.validate_launch("tier3-user", {"workload": "x"})

    def test_validate_launch_normalises(self):
        out = sm.validate_launch("tier2-template", dict(_LAUNCH))
        assert out["workload"] == "browser"
        assert out["network"] == "slirp4netns"
        assert out["argv"] == ["chromium"]

    @pytest.mark.parametrize("bad", [
        {"template_silo": "s", "network": "none"},                  # no workload
        {"workload": "w", "network": "none"},                       # no template_silo
        {"workload": "w", "template_silo": "s", "network": "lan"},  # bad network
        {"workload": "w", "template_silo": "s", "argv": "notalist"},
        {"workload": "../x", "template_silo": "s"},                 # unsafe token
    ])
    def test_validate_launch_rejects_bad(self, bad):
        with pytest.raises(BadArgument):
            sm.validate_launch("tier2-template", bad)

    def test_create_tier2_no_useradd(self, store, ops):
        store.create("browser1", sm.TIER2_LAUNCH_OWNER_UID,
                     kind="tier2-template", launch=dict(_LAUNCH))
        silo = store.get("browser1")
        assert silo.kind == "tier2-template"
        assert silo.launch["template_silo"] == "browser1"
        # No system user / state dir created for a template silo.
        assert "browser1" not in ops.users
        assert "browser1" not in ops.state_dirs

    def test_create_tier2_rejects_non_admin_uid(self, store):
        with pytest.raises(BadArgument):
            store.create("browser1", 2001, kind="tier2-template", launch=dict(_LAUNCH))

    def test_admin_uid_is_fixed_1000(self):
        assert sm.ADMIN_USER_NAME == "admin"
        assert sm.ADMIN_UID == 1000
        assert sm.TIER2_LAUNCH_OWNER_UID == 1000
        assert sm.validate_silo_uid(1000, sm.KIND_TIER2_TEMPLATE) == 1000

    def test_schema_round_trips_tier2(self, ops, tmp_path):
        s1 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        s1.create("browser1", sm.TIER2_LAUNCH_OWNER_UID,
                  kind="tier2-template", launch=dict(_LAUNCH))
        s1.create("dev1", 2001)  # a plain tier3 silo alongside
        # Reload from the persisted file into a fresh store + ops.
        s2 = _SiloStore(_FakeOps(), config_path=tmp_path / "silos.yaml")
        b = s2.get("browser1")
        assert b.kind == "tier2-template"
        assert b.uid == sm.TIER2_LAUNCH_OWNER_UID
        assert b.launch == {"workload": "browser", "template_silo": "browser1",
                            "network": "slirp4netns", "argv": ["chromium"]}
        assert s2.get("dev1").kind == "tier3-user"
        assert s2.get("dev1").launch == {}

    def test_load_quarantines_tier2_wrong_admin_uid(self, ops, tmp_path,
                                                    caplog):
        cfg = tmp_path / "silos.yaml"
        old_admin_uid = sm.TIER2_LAUNCH_OWNER_UID + 1
        cfg.write_text(
            "silos:\n"
            "  - name: oldbrowser\n"
            f"    uid: {old_admin_uid}\n"
            "    state: Stopped\n"
            "    autostart: false\n"
            "    created_at: 1\n"
            "    last_change: 2\n"
            "    kind: tier2-template\n"
            "    launch:\n"
            "      workload: browser\n"
            "      template_silo: oldbrowser\n"
            "      network: none\n"
            "      argv: [\"chromium\"]\n"
        )
        import logging as _logging
        with caplog.at_level(_logging.ERROR):
            store = _SiloStore(ops, config_path=cfg)

        assert store.list_silos() == []
        assert any("quarantining tier2-template row" in r.message
                   for r in caplog.records)

        # A later lifecycle save must not silently erase the row. It remains
        # outside the live `silos:` set, preserved for manual admin repair.
        store.create("dev1", 2001)
        text = cfg.read_text()
        assert "quarantined_silos:" in text
        assert "oldbrowser" in text
        assert str(old_admin_uid) in text

        reloaded = _SiloStore(ops, config_path=cfg)
        assert [s.name for s in reloaded.list_silos()] == ["dev1"]
        reloaded.save()
        text2 = cfg.read_text()
        assert "quarantined_silos:" in text2
        assert "oldbrowser" in text2
        assert str(old_admin_uid) in text2

    def test_quarantine_survives_fallback_parser_reload(self, ops, tmp_path,
                                                        monkeypatch):
        cfg = tmp_path / "silos.yaml"
        old_admin_uid = sm.TIER2_LAUNCH_OWNER_UID + 1
        cfg.write_text(
            "silos:\n"
            "  - name: oldbrowser\n"
            f"    uid: {old_admin_uid}\n"
            "    state: Stopped\n"
            "    autostart: false\n"
            "    created_at: 1\n"
            "    last_change: 2\n"
            "    kind: tier2-template\n"
            "    launch:\n"
            "      workload: browser\n"
            "      template_silo: oldbrowser\n"
            "      network: none\n"
            "      argv: [\"chromium\"]\n"
        )
        store = _SiloStore(ops, config_path=cfg)
        store.create("dev1", 2001)
        assert "oldbrowser" in cfg.read_text()

        import sys
        monkeypatch.setitem(sys.modules, "yaml", None)
        reloaded = _SiloStore(ops, config_path=cfg)
        assert [s.name for s in reloaded.list_silos()] == ["dev1"]
        reloaded.save()
        text = cfg.read_text()
        assert "quarantined_silos:" in text
        assert "oldbrowser" in text
        assert str(old_admin_uid) in text

    def test_start_tier2_exports_env_and_starts_unit(self, store, ops):
        store.create("browser1", sm.TIER2_LAUNCH_OWNER_UID,
                     kind="tier2-template", launch=dict(_LAUNCH))
        store.start("browser1")
        assert store.get("browser1").state == State.ACTIVE
        # Started the tier-2 unit, NOT the tier-3 session launcher; no cgroup.
        assert ("start", "qdistro-tier2-silo@browser1.service") in ops.systemctl_calls
        assert "browser1" not in ops.cgroups
        env = ops.launch_envs["browser1"]
        # The env file must be sourceable by the launcher (`set -a; . file`)
        # and round-trip every value, INCLUDING an argv entry with a single
        # quote (validate_launch allows it; shlex.quote must escape it).
        import json as _json
        recovered = _source_env_via_bash(env)
        assert recovered["TIER2_SILO"] == "browser1"
        assert recovered["TIER2_NETWORK"] == "slirp4netns"
        assert _json.loads(recovered["QD_APP_ARGV_JSON"]) == ["chromium"]
        assert recovered["QD_WORKLOAD"] == "browser"
        assert "QDISTRO_ADMIN_USER" not in recovered

    def test_stop_tier2_clean(self, store, ops):
        store.create("browser1", sm.TIER2_LAUNCH_OWNER_UID,
                     kind="tier2-template", launch=dict(_LAUNCH))
        store.start("browser1")
        store.stop("browser1")
        assert store.get("browser1").state == State.STOPPED
        assert ("stop", "qdistro-tier2-silo@browser1.service") in ops.systemctl_calls
        assert "browser1" not in ops.launch_envs   # env cleared on a clean stop

    def test_stop_tier2_fails_closed_when_container_survives(self, store, ops):
        # fail-closed: if the stop did not take effect (unit still active or
        # the rootless container survives), the store must NOT report STOPPED
        # and must surface an error — never a silent lie hiding an orphan.
        store.create("browser1", sm.TIER2_LAUNCH_OWNER_UID,
                     kind="tier2-template", launch=dict(_LAUNCH))
        store.start("browser1")
        ops.tier2_stop_fails = True
        with pytest.raises(sm.SessionError):
            store.stop("browser1")
        # honest + retryable: ACTIVE (the container may still be running), not
        # STOPPED; the launch env is NOT torn down under the live container.
        assert store.get("browser1").state == State.ACTIVE
        assert "browser1" in ops.launch_envs
        # a retry after the stop genuinely takes effect succeeds.
        ops.tier2_stop_fails = False
        store.stop("browser1")
        assert store.get("browser1").state == State.STOPPED


def _source_env_via_bash(env_text: str) -> dict:
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
        fh.write(env_text)
        path = fh.name
    keys = ["TIER2_SILO", "TIER2_NETWORK", "QD_WORKLOAD", "QD_CONTAINER",
            "QD_APP_ARGV_JSON"]
    script = (f"set -a; . {path}; set +a; "
              + "; ".join(f'printf "%s\\0" "${{{k}}}"' for k in keys))
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         check=True)
    vals = out.stdout.split("\0")
    return dict(zip(keys, vals, strict=False))


def test_launch_env_round_trips_argv_with_single_quote(store, ops):
    # codex r2: an argv entry with a single quote must survive shlex.quote +
    # bash sourcing (a naive single-quote wrapper would corrupt it).
    store.create("b2", sm.TIER2_LAUNCH_OWNER_UID, kind="tier2-template", launch={
        "workload": "browser", "template_silo": "b2", "network": "none",
        "argv": ["chromium", "--user-agent=it's me"]})
    store.start("b2")
    import json as _json
    recovered = _source_env_via_bash(ops.launch_envs["b2"])
    assert _json.loads(recovered["QD_APP_ARGV_JSON"]) == \
        ["chromium", "--user-agent=it's me"]


def test_real_write_launch_env_is_owner_restricted_no_admin_chown(tmp_path,
                                                                  monkeypatch):
    """Security: the launch env is `.`-sourced by the now-root launch helper, so
    it must stay in root's TCB — written 0600 and NOT chowned away to the admin
    uid (the old behaviour, unsafe once the unit became User=root). Tests run
    unprivileged so fchown(0,0) EPERMs (the file stays owned by the test user ==
    the creating/sourcing uid, which is exactly the euid-relative guard's
    contract); we assert the mode is 0600 and the writer no longer hands the
    file to a DIFFERENT uid."""
    import os as _os
    import stat as _stat
    monkeypatch.setattr(sm, "TIER2_LAUNCH_ENV_DIR", tmp_path / "silo-launch")
    real_ops = sm._SystemOps()
    p = real_ops.write_launch_env("work", "TIER2_SILO='work'\n")
    st = _os.stat(p)
    assert _stat.S_IMODE(st.st_mode) == 0o600, oct(st.st_mode)
    # The file is owned by the creating (test) uid — NOT chowned to some other
    # uid. In prod (root) that is root:root; here it is the test user.
    assert st.st_uid == _os.getuid()
    # And the source has no lingering chown to the admin/launch-owner uid.
    src = (sm.__file__ and open(sm.__file__).read()) or ""
    assert "fchown(fd, TIER2_LAUNCH_OWNER_UID" not in src, \
        "write_launch_env must not chown the root-sourced env file to admin"


def test_stop_tier2_stops_unit_and_clears_env(store, ops):
    # M2 fable review: this test and the bad-uid one below were accidentally
    # nested inside the round-trip test above (they took `self` but lived in a
    # module-level function body), so pytest never collected them. Dedented to
    # module-level functions, they exercise the real stop dispatch + loader
    # drop (the behaviour was already correct — only the tests were dead).
    store.create("browser1", sm.TIER2_LAUNCH_OWNER_UID,
                 kind="tier2-template", launch=dict(_LAUNCH))
    store.start("browser1")
    store.stop("browser1")
    assert store.get("browser1").state == State.STOPPED
    assert ("stop", "qdistro-tier2-silo@browser1.service") in ops.systemctl_calls
    assert "browser1" not in ops.launch_envs


def test_loader_drops_tier2_row_with_bad_uid(ops, tmp_path):
    # A hand-edited tier2-template row with a non-admin uid must be dropped
    # (the loader rejects fake uids), not loaded with wrong privileges.
    p = tmp_path / "silos.yaml"
    p.write_text(
        "silos:\n"
        "  - name: browser1\n"
        "    uid: 2001\n"
        "    state: Stopped\n"
        "    autostart: false\n"
        "    created_at: 0\n"
        "    last_change: 0\n"
        "    kind: tier2-template\n"
        "    launch:\n"
        "      workload: browser\n"
        "      template_silo: browser1\n"
        "      network: none\n"
        "      argv: [\"chromium\"]\n"
    )
    s = _SiloStore(ops, config_path=p)
    with pytest.raises(UnknownSilo):
        s.get("browser1")


def test_tier2_launch_round_trips_through_fallback_parser(ops, tmp_path, monkeypatch):
    # M2 fable review: the schema-migration claim says the launch stanza
    # round-trips through BOTH PyYAML and the tolerant fallback parser, but
    # every other round-trip test goes through PyYAML. Mask `yaml` so
    # _yaml_load takes the hand-rolled fallback path, and assert a
    # tier2-template row (kind + nested launch incl. a JSON argv) survives.
    import sys
    s1 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
    s1.create("browser1", sm.TIER2_LAUNCH_OWNER_UID,
              kind="tier2-template", launch=dict(_LAUNCH))

    monkeypatch.setitem(sys.modules, "yaml", None)   # force ImportError path
    assert sm._yaml_load("silos: []\n") == {
        "silos": [], "quarantined_silos": [],
    }   # fallback engaged

    s2 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
    silo = s2.get("browser1")
    assert silo.kind == "tier2-template"
    assert silo.uid == sm.TIER2_LAUNCH_OWNER_UID
    assert silo.launch["workload"] == "browser"
    assert silo.launch["template_silo"] == "browser1"
    assert silo.launch["network"] == "slirp4netns"
    # argv is rendered as a JSON array; the fallback yields it verbatim and the
    # loader normalises it back to a list.
    assert list(silo.launch["argv"]) == ["chromium"]


# ---------------------------------------------------------------------------
# Per-silo netns egress lifecycle (todo/fable-networking task 3)
# ---------------------------------------------------------------------------

from qdistro_silo_egress import TunnelConfig, netns_name  # noqa: E402

_TUN = TunnelConfig(
    name="work", peer_public_key="PUB=", endpoint="vpn.example:51820",
    address="10.7.0.2/32", dns="10.7.0.1", keepalive=25)


@pytest.fixture
def egress_store(ops: _FakeOps, tmp_path: Path) -> _SiloStore:
    # A store whose wg tunnel config + private key resolve successfully, so a
    # `wg:` silo can come up live (not dark) in tests.
    return _SiloStore(
        ops, config_path=tmp_path / "silos.yaml",
        tunnel_resolver=lambda name: _TUN,
        key_provider=lambda name: "PRIVKEY=",
    )


class TestEgressField:
    def test_create_persists_and_roundtrips(self, ops, tmp_path):
        store = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        store.create("work", 2000, egress="wg:work")
        assert store.get("work").egress == "wg:work"
        # Reload from disk: egress survives the yaml round-trip.
        store2 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        assert store2.get("work").egress == "wg:work"

    def test_default_egress_is_none_legacy(self, store):
        # A silo created without egress is the legacy un-managed state.
        assert store.create("work", 2000).egress is None

    def test_invalid_egress_rejected(self, store):
        with pytest.raises(BadArgument):
            store.create("work", 2000, egress="wg:Bad Name")

    def test_egress_rejected_on_tier2(self, store):
        with pytest.raises(BadArgument):
            store.create("browser1", sm.TIER2_LAUNCH_OWNER_UID,
                         kind="tier2-template",
                         launch=dict(_LAUNCH), egress="none")

    def test_loader_drops_bad_egress_row(self, ops, tmp_path):
        cfg = tmp_path / "silos.yaml"
        cfg.write_text(
            "silos:\n"
            "  - name: work\n    uid: 2000\n    state: Stopped\n"
            "    autostart: false\n    created_at: 0\n    last_change: 0\n"
            "    kind: tier3-user\n    egress: 'wg:Bad Name'\n")
        store = _SiloStore(ops, config_path=cfg)
        with pytest.raises(UnknownSilo):
            store.get("work")          # the hand-edited bad row was dropped


class TestEgressLifecycle:
    def test_legacy_silo_touches_no_netns(self, store, ops):
        store.create("work", 2000)               # egress=None
        store.start("work")
        # Legacy silos are never placed in a netns: no create, no wg. start()
        # does a cheap unconditional backstop-clear (nft_skuid_drop False) to
        # sweep any orphaned element, so the silo is explicitly NOT blocked.
        assert "netns_create" not in ops.egress_ops()
        assert "wg_add_dev" not in ops.egress_ops()
        assert netns_name("work") not in ops.netns
        assert ops.skuid.get(2000) is not True   # not dropped/blocked
        assert ("start", "qdshell-session-work@2000.service") in ops.systemctl_calls

    def test_none_silo_comes_up_dark(self, egress_store, ops):
        egress_store.create("work", 2000, egress="none")
        egress_store.start("work")
        ns = netns_name("work")
        assert ns in ops.netns                   # netns created
        assert ops.skuid[2000] is True           # backstop on
        assert "wg_add_dev" not in ops.egress_ops()
        assert ops.resolv[ns] == []              # dark: empty resolv (not host's)

    def test_wg_silo_brings_up_tunnel_before_launch(self, egress_store, ops):
        egress_store.create("work", 2000, egress="wg:work")
        egress_store.start("work")
        eo = ops.egress_ops()
        assert "wg_add_dev" in eo and "link_set_netns" in eo
        # the wg device is created and moved before the launcher unit starts
        assert ops.resolv[netns_name("work")] == ["10.7.0.1"]
        # egress applied before systemctl start (processes see the tunnel first)
        assert ops.egress_calls           # something happened
        assert ops.systemctl_calls[-1][0] == "start"

    def test_stop_tears_down_netns(self, egress_store, ops):
        egress_store.create("work", 2000, egress="wg:work")
        egress_store.start("work")
        egress_store.stop("work", grace_s=0)
        ns = netns_name("work")
        assert ns not in ops.netns               # netns removed on stop
        assert ops.skuid[2000] is False          # backstop cleared

    def test_delete_removes_netns_idempotent(self, egress_store, ops):
        egress_store.create("work", 2000, egress="none")
        egress_store.start("work")
        egress_store.stop("work", grace_s=0)
        egress_store.delete("work")
        assert netns_name("work") not in ops.netns

    def test_wg_dark_when_key_unavailable(self, ops, tmp_path):
        # Default key_provider raises KeyUnavailable -> silo starts dark, but
        # the start still SUCCEEDS (B3: a locked vault must not fail the start).
        store = _SiloStore(ops, config_path=tmp_path / "silos.yaml",
                           tunnel_resolver=lambda n: _TUN)  # default key_provider
        store.create("work", 2000, egress="wg:work")
        store.start("work")                      # must not raise
        assert store.get("work").state == State.ACTIVE
        assert "wg_add_dev" not in ops.egress_ops()
        assert ops.skuid[2000] is True           # backstop still on


class TestSetEgress:
    def test_set_persists_and_audits(self, store, ops):
        store.create("work", 2000)
        store.set_egress("work", "wg:work")
        assert store.get("work").egress == "wg:work"

    def test_clear_to_legacy(self, store):
        store.create("work", 2000, egress="none")
        store.set_egress("work", None)
        assert store.get("work").egress is None

    def test_invalid_value_rejected(self, store):
        store.create("work", 2000)
        with pytest.raises(BadArgument):
            store.set_egress("work", "wg:Bad Name")

    def test_unknown_silo(self, store):
        with pytest.raises(UnknownSilo):
            store.set_egress("nope", "none")

    def test_rejected_on_tier2(self, store):
        store.create("browser1", sm.TIER2_LAUNCH_OWNER_UID,
                     kind="tier2-template",
                     launch=dict(_LAUNCH))
        with pytest.raises(BadArgument):
            store.set_egress("browser1", "none")

    def test_rejected_while_running(self, store):
        # Reconfigure requires a stop/start; refuse to mutate a live silo's
        # policy (the running tunnel wouldn't change anyway).
        store.create("work", 2000)
        store.start("work")
        with pytest.raises(SiloBusy):
            store.set_egress("work", "wg:work")

    def test_idempotent_same_value(self, store):
        store.create("work", 2000, egress="none")
        store.set_egress("work", "none")          # no raise, no change
        assert store.get("work").egress == "none"

    def test_set_takes_effect_next_start(self, egress_store, ops):
        # Policy set while stopped is applied at the next start.
        egress_store.create("work", 2000)
        egress_store.set_egress("work", "wg:work")
        egress_store.start("work")
        assert "wg_add_dev" in ops.egress_ops()

    def test_wg_start_starts_link_watcher_that_reattaches(self, egress_store,
                                                          ops):
        # Opt 3-C: a live wg silo gets a link-up watcher; firing it re-adds the
        # default route (Probe 2 finding 1) via the pure backend.
        egress_store.create("work", 2000, egress="wg:work")
        egress_store.start("work")
        assert "start_link_watcher" in ops.egress_ops()
        assert ops.watchers, "no watcher handle registered"
        _tag, _ns, _ifn, on_up = ops.watchers[-1]
        n = len(ops.egress_calls)
        on_up()                                    # simulate a wg link bounce
        kinds = [c[0] for c in ops.egress_calls[n:]]
        assert "route_add_default_dev" in kinds    # route re-installed on up

    def test_dark_wg_starts_no_watcher(self, ops, tmp_path):
        # A wg silo that comes up dark (no key) has no device to watch.
        store = _SiloStore(ops, config_path=tmp_path / "s.yaml",
                           tunnel_resolver=lambda n: _TUN)  # default key -> dark
        store.create("work", 2000, egress="wg:work")
        store.start("work")
        assert "start_link_watcher" not in ops.egress_ops()
        assert not ops.watchers

    def test_stop_stops_link_watcher(self, egress_store, ops):
        egress_store.create("work", 2000, egress="wg:work")
        egress_store.start("work")
        egress_store.stop("work")
        assert ("stop_link_watcher",) in ops.egress_calls
        assert not ops.watchers

    def test_stale_watcher_callback_is_noop_after_stop(self, egress_store, ops):
        # A link-up event that fires after the watcher was stopped (race past the
        # join timeout, or after a restart under a new tunnel) must NOT re-attach
        # stale state onto the torn-down netns (codex #5: generation guard).
        egress_store.create("work", 2000, egress="wg:work")
        egress_store.start("work")
        _tag, _ns, _ifn, on_up = ops.watchers[-1]
        egress_store.stop("work")                  # bumps the generation
        n = len(ops.egress_calls)
        on_up()                                    # a late link-up event
        assert ops.egress_calls[n:] == []          # self-cancelled, no reattach


class TestEgressReviewFixes:
    """Regression tests for the dual-review fixes (Fable B1/B2 + should-fixes)."""

    def test_set_egress_accepts_direct(self, store):
        # Opt 3-A completed `direct`, so the mutation path now persists it.
        store.create("work", 2000)
        store.set_egress("work", "direct")
        assert store.get("work").egress == "direct"

    def test_create_accepts_direct(self, store):
        s = store.create("work", 2000, egress="direct")
        assert s.egress == "direct"

    def test_direct_start_forwards_and_starts_resolver(self, egress_store, ops):
        # End-to-end at the store level: starting a direct silo enables host
        # forwarding and a per-silo resolver, and NATs its subnet.
        egress_store.create("work", 2000, egress="direct")
        egress_store.start("work")
        seq = ops.egress_ops()
        assert "enable_ip_forward" in seq
        assert "dns_start" in seq
        assert "nat_masquerade" in seq

    def test_start_degrades_to_dark_on_apply_failure(self, ops, tmp_path):
        # B1 follow-up: a transient bring-up failure (e.g. a boot-time endpoint
        # DNS race in `wg set`) must come up dark, not fail the whole start.
        class _FlakyBackend:
            def __init__(self):
                self.applied = []
            def apply(self, ns, uid, policy, o, **kw):
                self.applied.append(policy.spec)
                if policy.mode == "wg":
                    raise RuntimeError("wg set failed (endpoint DNS race)")
                return type("R", (), {"dark": True, "pending": None,
                                      "mode": "none"})()
            def teardown(self, *a, **k):
                pass
        be = _FlakyBackend()
        store = _SiloStore(ops, config_path=tmp_path / "silos.yaml",
                           egress_backend=be, tunnel_resolver=lambda n: _TUN,
                           key_provider=lambda n: "k")
        store.create("work", 2000, egress="wg:work")
        store.start("work")                       # must NOT raise
        assert store.get("work").state == State.ACTIVE
        assert "wg:work" in be.applied and "none" in be.applied  # fell back dark

    def test_legacy_start_removes_stale_netns(self, store, ops):
        # S5: a legacy silo (egress=None) whose netns lingered from a prior
        # policy/crash must have it removed on start, so spawn-tier3 (which keys
        # off netns existence) can't run it in a leftover dark netns.
        ops.netns.add(netns_name("work"))         # stale netns present
        store.create("work", 2000)                # legacy (no egress)
        store.start("work")
        assert netns_name("work") not in ops.netns


class TestNftBenign:
    def test_add_existing_and_delete_missing_are_benign(self):
        assert sm._nft_benign("Error: Could not process rule: File exists")
        assert sm._nft_benign("Error: Could not process rule: No such file or directory")
        assert sm._nft_benign("element does not exist")

    def test_real_error_is_not_benign(self):
        assert not sm._nft_benign("Error: syntax error, unexpected newline")
        assert not sm._nft_benign("")


# ---------------------------------------------------------------------------
# Disposable teardown (P2b — the Dispose D-Bus lease-release surface)
# ---------------------------------------------------------------------------

class _RecordingAudit:
    """Minimal _AuditLog stand-in: records every row the store writes so a
    test can assert the decision/reason without a sqlite backend."""
    def __init__(self):
        self.rows: list[dict] = []

    def record(self, action, silo, *, decision, reason, caller):
        self.rows.append(dict(action=action, silo=silo, decision=decision,
                              reason=reason, caller=caller))


_GOOD_DISP = "disp-pdf-20260612-151828"


class TestDispose:
    def _store(self, ops, tmp_path, audit=None):
        return _SiloStore(ops, config_path=tmp_path / "silos.yaml", audit=audit)

    def test_removes_a_disposable_and_audits_allow(self, ops, tmp_path):
        ops.disp_containers = [_GOOD_DISP]
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        caller = {"uid": 1000, "pid": 42, "exe": "/usr/bin/qs"}
        assert store.dispose(_GOOD_DISP, caller=caller) is True
        assert ops.disp_removed == [_GOOD_DISP]
        assert _GOOD_DISP not in ops.disp_containers
        row = audit.rows[-1]
        assert row["action"] == "dispose"
        assert row["decision"] == "allow"
        assert row["caller"] == caller

    @pytest.mark.parametrize("bad", [
        "qdistro-silo-browser",        # a persistent silo container
        "disp-pdf",                    # missing the timestamp shape
        "disposable",                  # merely starts with the letters
        "disp-pdf-20260612-151828; rm -rf /",  # injection-ish, not disp-shaped
        "",
    ])
    def test_rejects_non_disposable_name_fail_closed(self, ops, tmp_path, bad):
        ops.disp_containers = [bad] if bad else []
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        with pytest.raises(BadArgument):
            store.dispose(bad)
        # The non-disposable name NEVER reached the container remove.
        assert ops.disp_removed == []
        assert audit.rows[-1]["decision"] == "deny"

    def test_podman_rm_failure_returns_false_and_audits_error(self, ops, tmp_path):
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_remove_fails = True
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        assert store.dispose(_GOOD_DISP) is False
        assert ops.disp_removed == [_GOOD_DISP]   # attempted
        assert audit.rows[-1]["decision"] == "error"

    def test_remove_oserror_maps_to_badstate(self, ops, tmp_path):
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_remove_raises = FileNotFoundError("podman")
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        with pytest.raises(BadState):
            store.dispose(_GOOD_DISP)
        assert audit.rows[-1]["decision"] == "error"

    def test_dispose_without_audit_is_noop_safe(self, ops, tmp_path):
        # audit=None disables the durable sink; dispose must still work.
        ops.disp_containers = [_GOOD_DISP]
        store = self._store(ops, tmp_path, audit=None)
        assert store.dispose(_GOOD_DISP) is True


# A well-formed per-spawn launch token (32 lowercase hex, == the qdwin window
# instanceId == the container qdistro_tier2_token label).
_GOOD_TOKEN = "0123456789abcdef0123456789abcdef"


class TestDisposeByToken:
    """dispose_by_token resolves the launch token (window instanceId) to a
    container and tears it down — the taskbar's join when it holds a token but
    not the container name."""
    def _store(self, ops, tmp_path, audit=None):
        return _SiloStore(ops, config_path=tmp_path / "silos.yaml", audit=audit)

    def test_resolves_token_and_disposes(self, ops, tmp_path):
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_token_map = {_GOOD_TOKEN: [_GOOD_DISP]}
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        caller = {"uid": 1000, "pid": 7, "exe": "/usr/bin/qs"}
        assert store.dispose_by_token(_GOOD_TOKEN, caller=caller) is True
        assert ops.disp_removed == [_GOOD_DISP]
        assert _GOOD_DISP not in ops.disp_containers
        # The token->name join is recorded under 'dispose-by-token'...
        join = [r for r in audit.rows if r["action"] == "dispose-by-token"]
        assert join and join[-1]["decision"] == "allow"
        assert _GOOD_DISP in join[-1]["reason"]
        # ...and the concrete teardown under 'dispose' with the RESOLVED name.
        teardown = [r for r in audit.rows if r["action"] == "dispose"]
        assert teardown and teardown[-1]["silo"] == _GOOD_DISP
        assert teardown[-1]["decision"] == "allow"

    @pytest.mark.parametrize("bad", [
        "DEADBEEF",                         # uppercase (regex is lowercase)
        "short",                            # too short (<8 hex)
        "g0123456789abcdef",                # non-hex char
        "0123; rm -rf /",                   # injection-ish
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0",  # 65 > 64
        "",
    ])
    def test_malformed_token_is_badargument_fail_closed(self, ops, tmp_path, bad):
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_token_map = {bad: [_GOOD_DISP]}
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        with pytest.raises(BadArgument):
            store.dispose_by_token(bad)
        # Never reached a podman filter or remove.
        assert ops.disp_removed == []
        assert audit.rows[-1]["action"] == "dispose-by-token"
        assert audit.rows[-1]["decision"] == "deny"

    def test_no_match_is_idempotent_success(self, ops, tmp_path):
        # Token resolves to nothing (already closed/--rm'd/reaped): teardown
        # request is satisfied -> True, audited allow, no remove attempted.
        ops.disp_containers = []
        ops.disp_token_map = {}
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        assert store.dispose_by_token(_GOOD_TOKEN) is True
        assert ops.disp_removed == []
        row = audit.rows[-1]
        assert row["action"] == "dispose-by-token"
        assert row["decision"] == "allow"

    def test_ambiguous_match_is_badstate_no_removal(self, ops, tmp_path):
        # >1 live container for one token must never happen; if it does, refuse
        # rather than guess which to remove.
        other = "disp-pdf-20260612-151900"
        ops.disp_containers = [_GOOD_DISP, other]
        ops.disp_token_map = {_GOOD_TOKEN: [_GOOD_DISP, other]}
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        with pytest.raises(BadState):
            store.dispose_by_token(_GOOD_TOKEN)
        assert ops.disp_removed == []
        assert audit.rows[-1]["decision"] == "error"

    def test_token_for_nondisposable_label_resolves_to_nothing(self, ops, tmp_path):
        # Defence in depth: a token whose only match is an UNLABELLED (non-
        # disposable) container resolves to [] (the dual label filter), so the
        # teardown is a no-op success and never removes the admin container.
        ops.disp_containers = []                       # carries no disp label
        ops.disp_token_map = {_GOOD_TOKEN: ["qdistro-silo-browser"]}
        store = self._store(ops, tmp_path, audit=_RecordingAudit())
        assert store.dispose_by_token(_GOOD_TOKEN) is True
        assert ops.disp_removed == []

    def test_remove_oserror_maps_to_badstate(self, ops, tmp_path):
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_token_map = {_GOOD_TOKEN: [_GOOD_DISP]}
        ops.disp_remove_raises = FileNotFoundError("podman")
        store = self._store(ops, tmp_path, audit=_RecordingAudit())
        with pytest.raises(BadState):
            store.dispose_by_token(_GOOD_TOKEN)

    def test_lookup_failure_fails_closed_not_already_gone(self, ops, tmp_path):
        # A podman/runuser lookup failure must NOT be read as "already gone"
        # (that would fail OPEN: report a live container as torn down). It is a
        # BadState, audited error, with no removal attempted.
        ops.disp_token_lookup_raises = OSError("podman storage locked")
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        with pytest.raises(BadState):
            store.dispose_by_token(_GOOD_TOKEN)
        assert ops.disp_removed == []
        row = audit.rows[-1]
        assert row["action"] == "dispose-by-token"
        assert row["decision"] == "error"


# ---------------------------------------------------------------------------
# Disposable TTL-lease sweep (P2b — the periodic in-session leak backstop)
# ---------------------------------------------------------------------------

_LEASE_TOK = "0123456789abcdef0123456789abcdef"


class TestSweepExpiredLeases:
    """sweep_expired_leases() reaps disposables whose spawn-authored TTL lease
    has elapsed, delegating to dispose() (name re-validation + audit). It must
    be fail-safe (never reap a non-leased / malformed / non-disposable
    candidate) and crash-proof (a single failure never aborts the rest, and it
    never raises — a GLib timer drives it)."""
    def _store(self, ops, tmp_path, audit=None):
        return _SiloStore(ops, config_path=tmp_path / "silos.yaml", audit=audit)

    def _lease(self, ttl="300", created="1000", token=_LEASE_TOK):
        return {"token": token, "ttl": ttl, "created": created}

    def test_reaps_expired_lease_via_dispose_and_audits(self, ops, tmp_path):
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_lease_meta = {_GOOD_DISP: self._lease()}
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        reaped = store.sweep_expired_leases(now=5000.0)   # 1000+300 < 5000
        assert reaped == [_GOOD_DISP]
        assert ops.disp_removed == [_GOOD_DISP]
        assert _GOOD_DISP not in ops.disp_containers
        # The teardown is audited under 'dispose' and marked as a lease reap via
        # the caller exe (the only caller field _AuditLog persists is
        # uid/pid/exe — so the discriminator MUST live in exe, not a dropped
        # extra key).
        row = [r for r in audit.rows if r["action"] == "dispose"][-1]
        assert row["decision"] == "allow"
        assert row["caller"]["exe"] == "session-manager:lease-ttl"

    def test_no_lease_label_is_never_reaped(self, ops, tmp_path):
        # The default (no QDISTRO_DISPOSABLE_TTL at spawn) -> no ttl label ->
        # the interactive disposable is never wall-clock-killed by the sweep.
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_lease_meta = {}            # no lease metadata at all
        store = self._store(ops, tmp_path, _RecordingAudit())
        assert store.sweep_expired_leases(now=9_000_000_000.0) == []
        assert ops.disp_removed == []
        assert _GOOD_DISP in ops.disp_containers

    def test_within_ttl_survives(self, ops, tmp_path):
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_lease_meta = {_GOOD_DISP: self._lease(ttl="5000")}
        store = self._store(ops, tmp_path, _RecordingAudit())
        assert store.sweep_expired_leases(now=2000.0) == []  # age 1000 < 5000
        assert ops.disp_removed == []

    @pytest.mark.parametrize("meta", [
        {"ttl": "0", "created": "1000"},          # opt-out ttl
        {"ttl": "5m", "created": "1000"},         # malformed ttl
        {"ttl": "300", "created": "<no value>"},  # missing created
        {"ttl": "300", "created": "bogus"},       # malformed created
        {"ttl": "<no value>", "created": "1000"},  # missing ttl
    ])
    def test_unleased_or_malformed_candidates_survive(self, ops, tmp_path, meta):
        m = {"token": _LEASE_TOK, **meta}
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_lease_meta = {_GOOD_DISP: m}
        store = self._store(ops, tmp_path, _RecordingAudit())
        assert store.sweep_expired_leases(now=9_000_000_000.0) == []
        assert ops.disp_removed == []

    def test_bad_or_missing_token_label_survives(self, ops, tmp_path):
        # A container labelled qdistro_disposable=1 but with no/garbled token
        # label is not one of our well-formed spawns — skip it (defence in depth
        # on top of the disposable-name guard).
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_lease_meta = {_GOOD_DISP: self._lease(token="NOTHEX")}
        store = self._store(ops, tmp_path, _RecordingAudit())
        assert store.sweep_expired_leases(now=9_000_000_000.0) == []
        assert ops.disp_removed == []

    def test_one_failure_does_not_abort_the_rest(self, ops, tmp_path):
        # The first expired disposable fails to remove (podman rm rc!=0); the
        # second must still be reaped. Order is the enumeration order.
        other = "disp-office-20260612-152000"
        ops.disp_containers = [_GOOD_DISP, other]
        ops.disp_lease_meta = {_GOOD_DISP: self._lease(),
                               other: self._lease()}
        ops.disp_remove_fails = True   # both report rc!=0 -> dispose() False
        store = self._store(ops, tmp_path, _RecordingAudit())
        reaped = store.sweep_expired_leases(now=5000.0)
        # Neither was reaped (both rm-fail), but BOTH were attempted — the loop
        # did not abort after the first.
        assert reaped == []
        assert ops.disp_removed == [_GOOD_DISP, other]

    def test_badstate_on_one_does_not_abort(self, ops, tmp_path):
        # dispose() raising BadState (podman OSError) on one candidate is caught
        # and the sweep continues. Use a remove that raises only for the first.
        other = "disp-office-20260612-152000"
        ops.disp_containers = [_GOOD_DISP, other]
        ops.disp_lease_meta = {_GOOD_DISP: self._lease(),
                               other: self._lease()}

        real_remove = ops.disp_container_remove

        def flaky(name):
            if name == _GOOD_DISP:
                raise FileNotFoundError("podman")
            return real_remove(name)
        ops.disp_container_remove = flaky
        store = self._store(ops, tmp_path, _RecordingAudit())
        reaped = store.sweep_expired_leases(now=5000.0)
        assert reaped == [other]            # second one still reaped
        assert other not in ops.disp_containers

    def test_enumeration_failure_returns_empty_not_raise(self, ops, tmp_path):
        ops.disp_lease_raises = OSError("podman storage locked")
        store = self._store(ops, tmp_path, _RecordingAudit())
        # candidates=None -> store enumerates -> ops raises -> swept to [].
        assert store.sweep_expired_leases() == []

    def test_default_caller_marks_session_manager(self, ops, tmp_path):
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_lease_meta = {_GOOD_DISP: self._lease()}
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        # No explicit caller -> the store stamps its lease caller, whose exe is
        # the durable (uid/pid/exe-only) discriminator persisted by _AuditLog.
        store.sweep_expired_leases(now=5000.0)
        assert store.LEASE_CALLER["exe"] == "session-manager:lease-ttl"
        row = [r for r in audit.rows if r["action"] == "dispose"][-1]
        assert row["caller"]["exe"] == "session-manager:lease-ttl"
        # Defence against shared-dict mutation: the class constant is unchanged.
        assert "uid" in store.LEASE_CALLER and store.LEASE_CALLER["uid"] == 0


# ---------------------------------------------------------------------------
# Process-tree-empty sweep (P2b — reap a disposable collapsed to weston PID1)
# ---------------------------------------------------------------------------

_PT_TOP_EMPTY = "PID COMMAND\n1 weston\n"
_PT_TOP_BUSY = "PID COMMAND\n1 weston\n42 weston-terminal\n"


class TestSweepEmptyProctrees:
    """sweep_empty_proctrees() reaps OPT-IN disposables whose inner tree has
    collapsed to the compositor PID1 (weston) alone, past grace. Two-phase:
    metadata eligibility, then a `podman top` emptiness check ONLY for eligible
    candidates. Fail-safe (never reap a non-opted-in/mid-startup/non-empty/
    non-disposable) and crash-proof (never raises; one failure never aborts the
    rest)."""
    def _store(self, ops, tmp_path, audit=None):
        return _SiloStore(ops, config_path=tmp_path / "silos.yaml", audit=audit)

    def _meta(self, token=_LEASE_TOK, proctree="1", created="1000", grace="30"):
        return {"token": token, "proctree": proctree,
                "created": created, "grace": grace}

    def test_reaps_empty_tree_via_dispose_and_audits(self, ops, tmp_path):
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_proctree_meta = {_GOOD_DISP: self._meta()}
        ops.disp_top_output = {_GOOD_DISP: _PT_TOP_EMPTY}
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        reaped = store.sweep_empty_proctrees(now=5000.0)  # past grace
        assert reaped == [_GOOD_DISP]
        assert ops.disp_removed == [_GOOD_DISP]
        # podman top was consulted for the eligible candidate.
        assert ops.disp_top_calls == [_GOOD_DISP]
        # Audited under 'dispose' with the distinct proctree caller exe.
        row = [r for r in audit.rows if r["action"] == "dispose"][-1]
        assert row["decision"] == "allow"
        assert row["caller"]["exe"] == "session-manager:lease-proctree"

    def test_busy_tree_is_never_reaped(self, ops, tmp_path):
        # An inner client (weston-terminal) is still running -> not empty -> keep.
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_proctree_meta = {_GOOD_DISP: self._meta()}
        ops.disp_top_output = {_GOOD_DISP: _PT_TOP_BUSY}
        store = self._store(ops, tmp_path, _RecordingAudit())
        assert store.sweep_empty_proctrees(now=5000.0) == []
        assert ops.disp_removed == []
        # top WAS consulted (it was eligible) but emptiness was False.
        assert ops.disp_top_calls == [_GOOD_DISP]

    def test_not_opted_in_is_never_inspected_or_reaped(self, ops, tmp_path):
        # A disposable without the proctree label is not even enumerated by the
        # candidate read (the real --filter), so it is never inspected.
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_proctree_meta = {}            # no proctree metadata
        ops.disp_top_output = {_GOOD_DISP: _PT_TOP_EMPTY}
        store = self._store(ops, tmp_path, _RecordingAudit())
        assert store.sweep_empty_proctrees(now=9_000_000_000.0) == []
        assert ops.disp_top_calls == []        # never inspected
        assert ops.disp_removed == []

    def test_within_grace_is_not_inspected(self, ops, tmp_path):
        # Mid-startup (within grace) -> ineligible -> top never consulted, so a
        # transiently-PID1-only just-spawned pod is never reaped.
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_proctree_meta = {_GOOD_DISP: self._meta(created="1000",
                                                         grace="30")}
        ops.disp_top_output = {_GOOD_DISP: _PT_TOP_EMPTY}
        store = self._store(ops, tmp_path, _RecordingAudit())
        assert store.sweep_empty_proctrees(now=1010.0) == []   # age 10 < grace 30
        assert ops.disp_top_calls == []
        assert ops.disp_removed == []

    @pytest.mark.parametrize("meta", [
        {"token": "NOTHEX"},                       # bad token
        {"token": "<no value>"},                   # missing token
        {"created": "<no value>"},                 # no created -> cannot judge age
        {"created": "garbage"},                    # malformed created
        {"proctree": "0"},                         # opt-out value (enumerated but
                                                   # eligibility rejects)
    ])
    def test_guard_skipped_candidates_survive(self, ops, tmp_path, meta):
        m = self._meta()
        m.update(meta)
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_proctree_meta = {_GOOD_DISP: m}
        ops.disp_top_output = {_GOOD_DISP: _PT_TOP_EMPTY}
        store = self._store(ops, tmp_path, _RecordingAudit())
        assert store.sweep_empty_proctrees(now=9_000_000_000.0) == []
        assert ops.disp_top_calls == []           # ineligible -> never inspected
        assert ops.disp_removed == []

    def test_top_failure_is_skip_not_reap(self, ops, tmp_path):
        # podman top returning None (error/timeout) must SKIP, never reap.
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_proctree_meta = {_GOOD_DISP: self._meta()}
        ops.disp_top_output = {_GOOD_DISP: None}
        store = self._store(ops, tmp_path, _RecordingAudit())
        assert store.sweep_empty_proctrees(now=5000.0) == []
        assert ops.disp_top_calls == [_GOOD_DISP]
        assert ops.disp_removed == []

    def test_top_raises_is_skip_not_crash(self, ops, tmp_path):
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_proctree_meta = {_GOOD_DISP: self._meta()}
        ops.disp_top_raises = OSError("podman top exploded")
        store = self._store(ops, tmp_path, _RecordingAudit())
        # Per-candidate top error is caught -> skip, not crash the sweep.
        assert store.sweep_empty_proctrees(now=5000.0) == []
        assert ops.disp_removed == []

    def test_enumeration_failure_is_swept_to_empty(self, ops, tmp_path):
        ops.disp_proctree_raises = OSError("podman storage locked")
        store = self._store(ops, tmp_path, _RecordingAudit())
        assert store.sweep_empty_proctrees() == []

    def test_one_failure_does_not_abort_the_rest(self, ops, tmp_path):
        other = "disp-agent-20260612-152000"
        ops.disp_containers = [_GOOD_DISP, other]
        ops.disp_proctree_meta = {_GOOD_DISP: self._meta(),
                                  other: self._meta()}
        ops.disp_top_output = {_GOOD_DISP: _PT_TOP_EMPTY,
                               other: _PT_TOP_EMPTY}
        # First dispose raises BadState (failed rm); the sweep must still reach
        # and reap the second.
        store = self._store(ops, tmp_path, _RecordingAudit())
        orig = store.dispose
        calls = []

        def flaky(name, caller=None):
            calls.append(name)
            if name == _GOOD_DISP:
                from qdistro_session_manager import BadState as BS
                raise BS("simulated rm failure")
            return orig(name, caller=caller)
        store.dispose = flaky
        reaped = store.sweep_empty_proctrees(now=5000.0)
        assert reaped == [other]
        assert _GOOD_DISP in calls and other in calls

    def test_default_caller_marks_proctree(self, ops, tmp_path):
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_proctree_meta = {_GOOD_DISP: self._meta()}
        ops.disp_top_output = {_GOOD_DISP: _PT_TOP_EMPTY}
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        store.sweep_empty_proctrees(now=5000.0)
        assert store.PROCTREE_CALLER["exe"] == "session-manager:lease-proctree"
        # Distinct from the TTL caller so an audit reader can tell them apart.
        assert store.PROCTREE_CALLER["exe"] != store.LEASE_CALLER["exe"]


# ---------------------------------------------------------------------------
# DisposeByWorkflow (P2b — tear down every disposable a workflow step spawned)
# ---------------------------------------------------------------------------

_WF_ID = "step-1"


class TestDisposeByWorkflow:
    """dispose_by_workflow resolves every disposable carrying the shared
    qdistro_lease_workflow=<id> label and tears them all down. Fail-closed like
    dispose_by_token, and — crucially — must NOT report clean success if any
    matched container failed to dispose (cond 8)."""
    def _store(self, ops, tmp_path, audit=None):
        return _SiloStore(ops, config_path=tmp_path / "silos.yaml", audit=audit)

    def test_reaps_all_in_the_group(self, ops, tmp_path):
        a = "disp-agent-20260612-151828"
        b = "disp-agent-20260612-151900"
        ops.disp_containers = [a, b]
        ops.disp_workflow_map = {_WF_ID: [a, b]}
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        caller = {"uid": 1000, "pid": 9, "exe": "/usr/bin/qs"}
        assert store.dispose_by_workflow(_WF_ID, caller=caller) == 2
        assert sorted(ops.disp_removed) == sorted([a, b])
        join = [r for r in audit.rows if r["action"] == "dispose-by-workflow"]
        assert join[-1]["decision"] == "allow"
        teardowns = [r for r in audit.rows if r["action"] == "dispose"]
        assert {r["silo"] for r in teardowns} == {a, b}

    @pytest.mark.parametrize("bad", [
        "UPPER", "has space", "x;rm -rf /", "-lead", "", "x" * 129,
    ])
    def test_malformed_id_is_badargument_fail_closed(self, ops, tmp_path, bad):
        ops.disp_containers = [_GOOD_DISP]
        ops.disp_workflow_map = {bad: [_GOOD_DISP]}
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        with pytest.raises(BadArgument):
            store.dispose_by_workflow(bad)
        assert ops.disp_removed == []            # never reached a filter/remove
        assert audit.rows[-1]["action"] == "dispose-by-workflow"
        assert audit.rows[-1]["decision"] == "deny"

    def test_no_match_is_idempotent_zero(self, ops, tmp_path):
        ops.disp_containers = []
        ops.disp_workflow_map = {}
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        assert store.dispose_by_workflow(_WF_ID) == 0
        assert ops.disp_removed == []
        assert audit.rows[-1]["decision"] == "allow"

    def test_lookup_failure_fails_closed_not_already_gone(self, ops, tmp_path):
        ops.disp_workflow_lookup_raises = OSError("podman storage locked")
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        with pytest.raises(BadState):
            store.dispose_by_workflow(_WF_ID)
        assert ops.disp_removed == []
        assert audit.rows[-1]["decision"] == "error"

    def test_partial_failure_raises_not_clean_success(self, ops, tmp_path):
        # One of two containers fails to dispose -> the method MUST NOT report
        # clean success (cond 8). The other is still attempted (best-effort).
        a = "disp-agent-20260612-151828"
        b = "disp-agent-20260612-151900"
        ops.disp_containers = [a, b]
        ops.disp_workflow_map = {_WF_ID: [a, b]}
        audit = _RecordingAudit()
        store = self._store(ops, tmp_path, audit)
        orig = store.dispose
        seen = []

        def flaky(name, caller=None):
            seen.append(name)
            if name == a:
                return orig(name, caller=caller)   # succeeds
            return False                            # podman rm "failed"
        store.dispose = flaky
        with pytest.raises(BadState):
            store.dispose_by_workflow(_WF_ID)
        # Both were attempted (best-effort across the group)...
        assert a in seen and b in seen
        # ...and the failure was audited as an error, not a clean allow.
        err = [r for r in audit.rows
               if r["action"] == "dispose-by-workflow" and r["decision"] == "error"]
        assert err

    def test_nondisposable_workflow_match_resolves_to_nothing(self, ops, tmp_path):
        # Defence in depth: a workflow id whose only match is an UNLABELLED
        # (non-disposable) container resolves to [] via the dual label filter ->
        # idempotent 0, never removes the admin container.
        ops.disp_containers = []
        ops.disp_workflow_map = {_WF_ID: ["qdistro-silo-browser"]}
        store = self._store(ops, tmp_path, _RecordingAudit())
        assert store.dispose_by_workflow(_WF_ID) == 0
        assert ops.disp_removed == []


class TestDispLeaseCandidates:
    """The real _SystemOps.disp_lease_candidates() podman parsing, with podman
    stubbed via a fake subprocess.run."""
    def _ops_with_podman(self, monkeypatch, stdout, returncode=0):
        ops = sm._SystemOps()

        def fake_run(argv, **kw):
            return types.SimpleNamespace(
                returncode=returncode, stdout=stdout, stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        return ops

    def test_parses_json_rows(self, monkeypatch):
        # Parsed from `podman ps --format json`: a populated row carries the raw
        # label strings; an ABSENT label is null -> None (parse_lease_seconds maps
        # None to None downstream, so the candidate is skipped).
        rows = json.dumps([
            {"Names": ["disp-pdf-20260612-151828"],
             "Labels": {"qdistro_tier2_token": _LEASE_TOK,
                        "qdistro_lease_ttl": "300",
                        "qdistro_lease_created": "1000"}},
            {"Names": ["disp-x-20260612-152000"], "Labels": {}},
        ])
        ops = self._ops_with_podman(monkeypatch, rows)
        cands = ops.disp_lease_candidates()
        assert cands[0] == {"name": "disp-pdf-20260612-151828",
                            "token": _LEASE_TOK, "ttl": "300",
                            "created": "1000"}
        assert cands[1] == {"name": "disp-x-20260612-152000",
                            "token": None, "ttl": None, "created": None}

    def test_returns_empty_on_podman_failure(self, monkeypatch):
        ops = self._ops_with_podman(monkeypatch, "", returncode=125)
        assert ops.disp_lease_candidates() == []

    def test_skips_malformed_json(self, monkeypatch):
        # Garbled JSON (a truncated stream) fails closed for the whole pass.
        ops = self._ops_with_podman(monkeypatch, '[{"Names": ["disp-')
        assert ops.disp_lease_candidates() == []

    def test_empty_array_is_no_candidates(self, monkeypatch):
        ops = self._ops_with_podman(monkeypatch, "[]")
        assert ops.disp_lease_candidates() == []

    def test_label_value_with_newline_cannot_forge_a_record(self, monkeypatch):
        # The injection the US/splitlines parse was vulnerable to: a label value
        # carrying a newline + a fake disp-* name + an expired lease. Under JSON
        # the newline is escaped inside ONE record's label value — it cannot
        # spill into a second forged record. The genuine row is parsed intact;
        # the embedded bytes are confined to the (here non-token) label value.
        forged = ("x\n" + json.dumps(["disp-evil-20200101-000000"]).strip("[]")
                  + "\x1f300\x1f1000")
        rows = json.dumps([
            {"Names": ["disp-pdf-20260612-151828"],
             "Labels": {"qdistro_tier2_token": _LEASE_TOK,
                        "qdistro_lease_ttl": forged,
                        "qdistro_lease_created": "1000"}},
        ])
        ops = self._ops_with_podman(monkeypatch, rows)
        cands = ops.disp_lease_candidates()
        # Exactly ONE candidate (the real one); the forged bytes stay CONFINED to
        # the genuine row's ttl VALUE — they never became a second record whose
        # NAME is disp-evil. No candidate is named disp-evil, and the forged ttl
        # is non-integer so parse_lease_seconds will reject it (never reaped).
        assert len(cands) == 1
        assert cands[0]["name"] == "disp-pdf-20260612-151828"
        assert all(c["name"] != "disp-evil-20200101-000000" for c in cands)
        assert "disp-evil" in cands[0]["ttl"]  # confined to the value, harmless

    def test_enumeration_timeout_returns_empty(self, monkeypatch):
        ops = sm._SystemOps()

        def boom(argv, **kw):
            raise sm.subprocess.TimeoutExpired(argv, 30)
        monkeypatch.setattr(sm.subprocess, "run", boom)
        assert ops.disp_lease_candidates() == []


class TestDispProctreeCandidates:
    """The real _SystemOps.disp_proctree_candidates() podman parsing (5 US fields)
    and disp_container_top_pids(), with podman stubbed."""
    def _ops_with_podman(self, monkeypatch, stdout, returncode=0):
        ops = sm._SystemOps()

        def fake_run(argv, **kw):
            return types.SimpleNamespace(
                returncode=returncode, stdout=stdout, stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        return ops

    def test_parses_json_rows(self, monkeypatch):
        rows = json.dumps([
            {"Names": ["disp-agent-20260612-151828"],
             "Labels": {"qdistro_tier2_token": _LEASE_TOK,
                        "qdistro_lease_proctree": "1",
                        "qdistro_lease_created": "1000",
                        "qdistro_lease_proctree_grace": "30"}},
            {"Names": ["disp-x-20260612-152000"],
             "Labels": {"qdistro_lease_proctree": "1",
                        "qdistro_lease_created": "1000"}},
        ])
        ops = self._ops_with_podman(monkeypatch, rows)
        cands = ops.disp_proctree_candidates()
        assert cands[0] == {"name": "disp-agent-20260612-151828",
                            "token": _LEASE_TOK, "proctree": "1",
                            "created": "1000", "grace": "30"}
        assert cands[1] == {"name": "disp-x-20260612-152000",
                            "token": None, "proctree": "1",
                            "created": "1000", "grace": None}

    def test_returns_empty_on_podman_failure(self, monkeypatch):
        ops = self._ops_with_podman(monkeypatch, "", returncode=125)
        assert ops.disp_proctree_candidates() == []

    def test_skips_malformed_json(self, monkeypatch):
        ops = self._ops_with_podman(monkeypatch, '{"not": "a list"}')
        assert ops.disp_proctree_candidates() == []

    def test_enumeration_timeout_returns_empty(self, monkeypatch):
        ops = sm._SystemOps()

        def boom(argv, **kw):
            raise sm.subprocess.TimeoutExpired(argv, 30)
        monkeypatch.setattr(sm.subprocess, "run", boom)
        assert ops.disp_proctree_candidates() == []

    def test_top_returns_stdout_for_disposable(self, monkeypatch):
        ops = self._ops_with_podman(monkeypatch, "PID COMMAND\n1 weston\n")
        assert ops.disp_container_top_pids("disp-agent-20260612-151828") \
            == "PID COMMAND\n1 weston\n"

    def test_top_refuses_nondisposable_name(self, monkeypatch):
        # Defence in depth: a non-disposable name never reaches `podman top`.
        called = []

        def fake_run(argv, **kw):
            called.append(argv)
            return types.SimpleNamespace(returncode=0, stdout="x", stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        ops = sm._SystemOps()
        assert ops.disp_container_top_pids("qdistro-silo-browser") is None
        assert called == []

    def test_top_failure_returns_none(self, monkeypatch):
        ops = self._ops_with_podman(monkeypatch, "", returncode=125)
        assert ops.disp_container_top_pids("disp-agent-20260612-151828") is None

    def test_top_timeout_returns_none(self, monkeypatch):
        ops = sm._SystemOps()

        def boom(argv, **kw):
            raise sm.subprocess.TimeoutExpired(argv, 30)
        monkeypatch.setattr(sm.subprocess, "run", boom)
        assert ops.disp_container_top_pids("disp-agent-20260612-151828") is None


class TestDispContainerRemoveTimeout:
    """disp_container_remove must bound `podman rm -f` so a wedged remove never
    blocks the single-flight lease worker forever (M1)."""
    def test_timeout_is_failed_removal_not_raise(self, monkeypatch):
        ops = sm._SystemOps()

        def hang(argv, **kw):
            assert kw.get("timeout"), "podman rm -f must pass a timeout"
            raise sm.subprocess.TimeoutExpired(argv, kw["timeout"])
        monkeypatch.setattr(sm.subprocess, "run", hang)
        # A timed-out rm is a failed (retryable) removal -> False, NOT an
        # exception (keeps the bool contract; unblocks the worker thread).
        assert ops.disp_container_remove("disp-pdf-20260612-151828") is False

    def test_nondisposable_name_still_refused(self):
        ops = sm._SystemOps()
        with pytest.raises(ValueError):
            ops.disp_container_remove("qdistro-silo-browser")


class TestDispLiveTokensFailClosed:
    """disp_live_tokens feeds the export-staging ORPHAN sweep, which DELETES
    every staging dir whose token is NOT in the returned set. So a parse FAILURE
    must map to None ("skip the pass"), NEVER to an empty set — an empty-on-error
    set would delete a LIVE disposable's not-yet-imported artifacts (fail-OPEN
    data loss). A genuine empty array IS an empty set (safe to reap orphans)."""
    def _ops(self, monkeypatch, stdout, returncode=0):
        ops = sm._SystemOps()

        def fake_run(argv, **kw):
            return types.SimpleNamespace(
                returncode=returncode, stdout=stdout, stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        return ops

    def test_genuine_empty_array_is_empty_set(self, monkeypatch):
        # No disposables: a real `[]` -> empty set (safe to reap every orphan).
        assert self._ops(monkeypatch, "[]").disp_live_tokens() == set()

    def test_all_well_formed_tokens_yield_the_full_set(self, monkeypatch):
        tok2 = "f" * 32
        raw = json.dumps([
            {"Names": ["disp-a-20260612-151828"],
             "Labels": {"qdistro_tier2_token": _LEASE_TOK}},
            {"Names": ["disp-b-20260612-152000"],
             "Labels": {"qdistro_tier2_token": tok2}},
        ])
        assert self._ops(monkeypatch, raw).disp_live_tokens() == {_LEASE_TOK,
                                                                  tok2}

    @pytest.mark.parametrize("bad_label", [
        {"qdistro_tier2_token": "NOTHEX"},   # garbled token on a live record
        {},                                  # absent token on a live record
        {"qdistro_tier2_token": None},       # null token
    ])
    def test_any_record_without_a_clean_token_skips_the_pass(
            self, monkeypatch, bad_label):
        # All-or-nothing: a live disposable ALWAYS has a hex token, so a record
        # that can't yield one means the enumeration is untrustworthy -> None
        # (skip), NEVER a smaller set (which would delete the un-accounted live
        # disposable's staging dir). Mixed with a good record, the WHOLE pass is
        # still None — an incomplete set is as dangerous as an empty one here.
        raw = json.dumps([
            {"Names": ["disp-a-20260612-151828"],
             "Labels": {"qdistro_tier2_token": _LEASE_TOK}},   # good
            {"Names": ["disp-b-20260612-152000"], "Labels": bad_label},  # bad
        ])
        assert self._ops(monkeypatch, raw).disp_live_tokens() is None

    def test_non_dict_record_skips_the_pass(self, monkeypatch):
        raw = json.dumps([{"Names": ["disp-a-20260612-151828"],
                           "Labels": {"qdistro_tier2_token": _LEASE_TOK}}, 42])
        assert self._ops(monkeypatch, raw).disp_live_tokens() is None

    def test_array_of_non_dicts_skips_the_pass(self, monkeypatch):
        # The structurally-wrong [1,2,3] case: a valid JSON list whose records
        # are not dicts -> None (not an empty set).
        assert self._ops(monkeypatch, "[1, 2, 3]").disp_live_tokens() is None

    @pytest.mark.parametrize("stdout", [
        "",                          # blank rc=0 stream
        "   ",                       # whitespace
        '[{"Names": ["disp-',        # TRUNCATED json (the regression vector)
        "level=warning msg=blah",    # a stray log line on stdout, not json
        '{"not": "a list"}',         # valid json but not an array
        "null",                      # json null
    ])
    def test_unparseable_or_non_list_returns_none_not_empty_set(
            self, monkeypatch, stdout):
        # MUST be None, NOT set() — None tells the sweep to SKIP (never delete a
        # live disposable's staging dir on a garbled/truncated rc=0 stream).
        assert self._ops(monkeypatch, stdout).disp_live_tokens() is None

    def test_rc_nonzero_returns_none(self, monkeypatch):
        assert self._ops(monkeypatch, "", returncode=125).disp_live_tokens() \
            is None

    def test_timeout_returns_none(self, monkeypatch):
        ops = sm._SystemOps()

        def boom(argv, **kw):
            raise sm.subprocess.TimeoutExpired(argv, 30)
        monkeypatch.setattr(sm.subprocess, "run", boom)
        assert ops.disp_live_tokens() is None


_FULL_ID = "a" * 64
_OTHER_ID = "b" * 64
_DISP = "disp-pdf-20260612-151828"


def _cg_in(cid):
    return ("0::/user.slice/user-1006.slice/user@1006.service/user.slice/"
            f"libpod-{cid}.scope\n")


class TestStuckDescendantCleanup:
    """After a timed-out `podman rm -f`, the cleanup SIGKILLs ONLY this
    container's cgroup-verified HOST pids — never an unrelated host process, and
    it never wedges the worker (everything bounded + fail-safe). These drive the
    real _SystemOps wiring (inspect-id / top-hpid / /proc cgroup / os.kill) with
    a fake subprocess + fake /proc + recorded kills."""

    def _wire(self, monkeypatch, *, full_id, top_out, cgroups,
              rm_times_out=True):
        """full_id: the inspect {{.Id}} result. top_out: `podman top hpid comm`
        stdout. cgroups: {hostpid: /proc cgroup text or None}. rm always TIMES
        OUT first (the trigger), then the retry rm succeeds."""
        ops = sm._SystemOps()
        killed: list[tuple[int, int]] = []
        rm_calls = {"n": 0}

        def fake_run(argv, **kw):
            if "inspect" in argv:
                return types.SimpleNamespace(returncode=0, stdout=full_id,
                                             stderr="")
            if "top" in argv:
                return types.SimpleNamespace(returncode=0, stdout=top_out,
                                             stderr="")
            if argv[-3:-1] == ["rm", "-f"] or ("rm" in argv and "-f" in argv):
                rm_calls["n"] += 1
                if rm_calls["n"] == 1 and rm_times_out:
                    raise sm.subprocess.TimeoutExpired(argv, kw.get("timeout"))
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        def fake_kill(pid, sig):
            killed.append((pid, sig))

        def fake_open(path, *a, **kw):
            import io
            m = str(path)
            if m.startswith("/proc/") and m.endswith("/cgroup"):
                pid = int(m.split("/")[2])
                txt = cgroups.get(pid)
                if txt is None:
                    raise OSError("no such process")
                return io.StringIO(txt)
            raise AssertionError(f"unexpected open {path!r}")

        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        monkeypatch.setattr(sm.os, "kill", fake_kill)
        monkeypatch.setattr("builtins.open", fake_open)
        return ops, killed

    def test_kills_only_cgroup_verified_host_pids(self, monkeypatch):
        # 2001 + 2002 are in THIS container's cgroup -> killed. 2003 is in
        # ANOTHER container's cgroup (an unrelated host process that happens to
        # surface) -> NEVER killed. 2004 has an unreadable cgroup -> skipped.
        top = "HPID COMMAND\n2001 sleep\n2002 conmon\n2003 sleep\n2004 x\n"
        cgroups = {2001: _cg_in(_FULL_ID), 2002: _cg_in(_FULL_ID),
                   2003: _cg_in(_OTHER_ID), 2004: None}
        ops, killed = self._wire(monkeypatch, full_id=_FULL_ID, top_out=top,
                                 cgroups=cgroups)
        assert ops.disp_container_remove(_DISP) is False  # still retryable
        pids = sorted(p for p, _ in killed)
        assert pids == [2001, 2002]
        assert all(sig == signal.SIGKILL for _, sig in killed)

    def test_no_full_id_means_no_kill(self, monkeypatch):
        # inspect returns a non-full id -> the cleanup refuses to kill anything
        # (no authorization anchor). Fail-safe.
        top = "HPID COMMAND\n2001 sleep\n"
        ops, killed = self._wire(monkeypatch, full_id="short",
                                 top_out=top, cgroups={2001: _cg_in(_FULL_ID)})
        assert ops.disp_container_remove(_DISP) is False
        assert killed == []

    def test_top_unusable_kills_nothing(self, monkeypatch):
        # A malformed top row -> parse_podman_top_hpids None -> no kills.
        top = "HPID COMMAND\nnot-a-pid sleep\n"
        ops, killed = self._wire(monkeypatch, full_id=_FULL_ID, top_out=top,
                                 cgroups={})
        assert ops.disp_container_remove(_DISP) is False
        assert killed == []

    def test_all_pids_in_other_container_kills_nothing(self, monkeypatch):
        # Every host pid surfaced belongs to a DIFFERENT container's cgroup ->
        # nothing is ours -> nothing is killed (the #1 hazard: no collateral).
        top = "HPID COMMAND\n2001 sleep\n2002 sleep\n"
        cgroups = {2001: _cg_in(_OTHER_ID), 2002: _cg_in(_OTHER_ID)}
        ops, killed = self._wire(monkeypatch, full_id=_FULL_ID, top_out=top,
                                 cgroups=cgroups)
        assert ops.disp_container_remove(_DISP) is False
        assert killed == []

    def test_cleanup_never_raises_even_if_top_errors(self, monkeypatch):
        # If `podman top` itself raises, the cleanup swallows it and the remove
        # still returns False (the worker must never be wedged by a cleanup).
        ops = sm._SystemOps()
        rm_calls = {"n": 0}

        def fake_run(argv, **kw):
            if "inspect" in argv:
                return types.SimpleNamespace(returncode=0, stdout=_FULL_ID,
                                             stderr="")
            if "top" in argv:
                raise sm.subprocess.TimeoutExpired(argv, kw.get("timeout"))
            if "rm" in argv:
                rm_calls["n"] += 1
                if rm_calls["n"] == 1:
                    raise sm.subprocess.TimeoutExpired(argv, kw.get("timeout"))
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        assert ops.disp_container_remove(_DISP) is False

    def test_successful_rm_skips_cleanup_entirely(self, monkeypatch):
        # The happy path: rm succeeds first try -> NO inspect/top/kill at all.
        ops = sm._SystemOps()
        seen: list[str] = []

        def fake_run(argv, **kw):
            seen.append(" ".join(str(a) for a in argv))
            return types.SimpleNamespace(returncode=0, stdout=_FULL_ID,
                                         stderr="")
        # rm succeeds (rc 0) on the first call; but note _disp_container_full_id
        # runs an inspect BEFORE rm — that is the only pre-rm call, and on rm
        # success the cleanup never runs (no top/kill).
        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        killed = []
        monkeypatch.setattr(sm.os, "kill", lambda p, s: killed.append((p, s)))
        assert ops.disp_container_remove(_DISP) is True
        assert killed == []
        assert not any("top" in c for c in seen)

    def test_proc_cgroup_read_is_bounded_against_a_wedged_dstate_task(
            self, monkeypatch):
        # m1: a /proc/<pid>/cgroup read on a D-state task can block in the kernel
        # with NO bound -> would wedge the single-flight worker. The read is run
        # in a reader thread joined with a deadline; a hung read -> None -> the
        # pid is treated as unverifiable and NEVER killed (fail-safe), and the
        # call returns promptly instead of hanging.
        import time as _t
        ops = sm._SystemOps()

        def hung_open(path, *a, **kw):
            if str(path).endswith("/cgroup"):
                _t.sleep(30)   # simulate a kernel-blocked procfs read
            raise AssertionError("unexpected open")
        monkeypatch.setattr("builtins.open", hung_open)
        start = _t.monotonic()
        # Use the short timeout directly so the test is fast + deterministic.
        assert ops._read_proc_cgroup(2001, timeout=0.2) is None
        assert _t.monotonic() - start < 5.0   # returned promptly, did not hang

    def test_huge_pid_does_not_abort_kill_loop(self, monkeypatch):
        # m2: an out-of-range pid raises OverflowError from os.kill (not OSError).
        # It must be caught so a later VERIFIED straggler is still killed. (In
        # practice a huge pid's cgroup read fails so it never reaches the kill
        # set; this hardens the loop directly.)
        huge = 10 ** 30
        top = f"HPID COMMAND\n{huge} x\n2002 sleep\n"
        cgroups = {huge: _cg_in(_FULL_ID), 2002: _cg_in(_FULL_ID)}
        ops, killed = self._wire(monkeypatch, full_id=_FULL_ID, top_out=top,
                                 cgroups=cgroups)

        real_kill_targets = []

        def fake_kill(pid, sig):
            if pid == huge:
                raise OverflowError("Python int too large to convert to C long")
            real_kill_targets.append(pid)
        monkeypatch.setattr(sm.os, "kill", fake_kill)
        assert ops.disp_container_remove(_DISP) is False
        # The huge pid raised OverflowError but the loop continued and killed 2002.
        assert real_kill_targets == [2002]

    def test_aggregate_cgroup_read_is_bounded_by_pid_cap(self, monkeypatch):
        # A container reporting MANY host pids must not serialise unbounded
        # cgroup reads: at most _STUCK_CLEANUP_MAX_PIDS are inspected. Reads
        # beyond the cap are never attempted, so those pids are excluded from the
        # kill set (fail-safe). Here all pids ARE in our cgroup, but only the
        # first cap-many can be killed.
        n = sm._STUCK_CLEANUP_MAX_PIDS + 50
        pids = list(range(3000, 3000 + n))
        top = "HPID COMMAND\n" + "".join(f"{p} x\n" for p in pids)
        reads: list[int] = []

        ops = sm._SystemOps()
        killed: list[int] = []
        rm_calls = {"n": 0}

        def fake_run(argv, **kw):
            if "inspect" in argv:
                return types.SimpleNamespace(returncode=0, stdout=_FULL_ID,
                                             stderr="")
            if "top" in argv:
                return types.SimpleNamespace(returncode=0, stdout=top, stderr="")
            if "rm" in argv:
                rm_calls["n"] += 1
                if rm_calls["n"] == 1:
                    raise sm.subprocess.TimeoutExpired(argv, kw.get("timeout"))
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        import io

        def fake_open(path, *a, **kw):
            m = str(path)
            if m.startswith("/proc/") and m.endswith("/cgroup"):
                pid = int(m.split("/")[2])
                reads.append(pid)
                return io.StringIO(_cg_in(_FULL_ID))
            raise AssertionError(f"unexpected open {path!r}")

        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        monkeypatch.setattr(sm.os, "kill", lambda p, s: killed.append(p))
        monkeypatch.setattr("builtins.open", fake_open)
        assert ops.disp_container_remove(_DISP) is False
        # No more than the cap of cgroup reads were ever attempted...
        assert len(reads) <= sm._STUCK_CLEANUP_MAX_PIDS
        # ...and at most the cap of kills happened (the rest are left for the
        # next sweep — fail-safe, never an unbounded serial scan).
        assert len(killed) <= sm._STUCK_CLEANUP_MAX_PIDS
        assert killed == pids[:sm._STUCK_CLEANUP_MAX_PIDS]


class TestLeaseSweepScheduler:
    """The single-flight scheduler that the GLib timer drives: a slow/hung
    sweep must never start a second concurrent sweep, and the in-flight flag
    must ALWAYS re-arm so future ticks keep running (the M1 wedge guard)."""

    def test_single_flight_skips_while_running_then_rearms(self):
        # Inject a capturing spawn: it records the worker target WITHOUT running
        # it, so we control exactly when the 'worker' finishes.
        captured = []
        sched = sm._LeaseSweepScheduler(run_sweep=lambda: None,
                                        spawn=lambda target: captured.append(target))
        # First tick: starts a worker (one capture), stays True.
        assert sched.tick() is True
        assert len(captured) == 1
        # Second tick while the first worker has NOT finished: single-flight ->
        # no new worker spawned.
        assert sched.tick() is True
        assert len(captured) == 1
        # The worker completes -> in-flight flag clears.
        captured[0]()
        # Next tick re-arms and spawns again.
        assert sched.tick() is True
        assert len(captured) == 2

    def test_flag_clears_even_when_sweep_raises(self):
        captured = []

        def boom():
            raise RuntimeError("podman wedged")
        sched = sm._LeaseSweepScheduler(run_sweep=boom,
                                        spawn=lambda target: captured.append(target))
        sched.tick()
        captured[0]()                 # worker runs the raising sweep
        # Despite the raise, the flag re-armed -> a later tick spawns again.
        sched.tick()
        assert len(captured) == 2

    def test_real_thread_single_flight(self):
        # A real worker thread blocked on an Event: the second tick must skip,
        # and after the worker is released the scheduler re-arms.
        import threading as _t
        gate = _t.Event()
        ran = []

        def slow():
            ran.append(1)
            gate.wait(5)
        sched = sm._LeaseSweepScheduler(run_sweep=slow)
        assert sched.tick() is True   # spawns a real daemon thread, blocks on gate
        for _ in range(100):          # wait until the worker has entered slow()
            if ran:
                break
            time.sleep(0.01)
        assert ran, "worker thread never started"
        assert sched.tick() is True   # in-flight -> must NOT start a 2nd worker
        assert len(ran) == 1
        gate.set()                    # release the worker
        for _ in range(200):          # let it finish and clear the flag
            if not sched._running:
                break
            time.sleep(0.01)
        assert not sched._running
        sched.tick()                  # re-armed -> spawns again
        gate.set()
        for _ in range(200):
            if len(ran) == 2:
                break
            time.sleep(0.01)
        assert len(ran) == 2
