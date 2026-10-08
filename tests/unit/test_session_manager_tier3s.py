"""Session manager: the tier3s (gVisor runsc) silo kind (tier3s/CONTRACT.md §6).

The store runs as-is against test_session_manager's _FakeOps (extended below
with the tier3s side effects); the real _SystemOps tier3s methods run with
subprocess.run replaced by a recorder. The root launch helper
(session_manager/qdistro-tier3s-silo-launch) runs for real as the test user
under its non-root-only test overrides, with a recording spawn. Nothing here
runs podman, runsc or systemd: placement, ExecStop/ExecStopPost and teardown
on a real system are VM facts (A-ii smoke, A-iii drivers).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import types
from pathlib import Path

import pytest
import qdistro_session_manager as sm
from qdistro_session_manager import (
    BadArgument,
    SessionError,
    State,
    _SiloStore,
    _SystemOps,
)
from test_session_manager import _FakeOps

REPO = Path(__file__).resolve().parents[2]
LAUNCH_HELPER = REPO / "session_manager" / "qdistro-tier3s-silo-launch"
UNIT_FILE = REPO / "session_manager" / "qdistro-tier3s-silo@.service"
DBUS_CONF = REPO / "session_manager" / "org.qdistro.SessionManager1.conf"
INSTALLER = REPO / "scripts" / "install" / "install-session-manager.sh"

LAUNCH = {"workload": "headless-smoke", "template_silo": "smoke",
          "network": "none", "argv": []}
UNIT = "qdistro-tier3s-silo@smoke.service"


class _T3sOps(_FakeOps):
    """_FakeOps plus the tier3s side effects, with failure injection."""

    def __init__(self):
        super().__init__()
        self.profile = "dev"
        self.t3s_running = False          # tier3s_silo_running verdict
        self.t3s_running_after_cleanup = None   # verdict after a cleanup --unit
        self.t3s_cleanups: list[tuple[str, ...]] = []
        self.t3s_cleanup_ok = True
        self.t3s_installed = True
        self.t3s_live: list[str] = []
        self.t3s_live_raises: BaseException | None = None
        self.start_raises: BaseException | None = None
        self.refusal = ""
        self.t3s_bound_starts: list[str] = []
        self.events: list[tuple] = []

    def qdistro_profile(self) -> str:
        return self.profile

    def tier3s_silo_running(self, name: str) -> bool:
        self.events.append(("running?", name))
        if self.t3s_cleanups and self.t3s_running_after_cleanup is not None:
            return self.t3s_running_after_cleanup
        return self.t3s_running

    def tier3s_cleanup(self, *args: str) -> bool:
        self.t3s_cleanups.append(tuple(args))
        self.events.append(("cleanup", *args))
        return self.t3s_cleanup_ok

    def tier3s_installed(self) -> bool:
        return self.t3s_installed

    def tier3s_live_units(self) -> list[str]:
        if self.t3s_live_raises is not None:
            raise self.t3s_live_raises
        return list(self.t3s_live)

    def systemctl_start(self, unit: str) -> None:
        self.events.append(("start", unit))
        if self.start_raises is not None:
            raise self.start_raises
        super().systemctl_start(unit)

    def tier3s_systemctl_start(self, unit: str) -> None:
        # the tier3s unit is Type=notify: a refused launch fails this call
        self.t3s_bound_starts.append(unit)
        self.systemctl_start(unit)

    def tier3s_start_refusal(self, unit: str) -> str:
        self.events.append(("refusal?", unit))
        return self.refusal

    def systemctl_stop(self, unit: str, *, timeout=None) -> bool:
        self.events.append(("stop", unit))
        return super().systemctl_stop(unit, timeout=timeout)


@pytest.fixture
def ops() -> _T3sOps:
    return _T3sOps()


@pytest.fixture
def store(ops, tmp_path) -> _SiloStore:
    return _SiloStore(ops, config_path=tmp_path / "silos.yaml")


def env_of(ops, name="smoke") -> dict[str, str]:
    out = {}
    for ln in ops.tier3s_launch_envs[name].splitlines():
        if ln:
            k, v = ln.split("=", 1)
            out[k] = shlex.split(v)[0]
    return out


def make(store, name="smoke", **launch):
    return store.create(name, sm.ADMIN_UID, kind=sm.KIND_TIER3S,
                        launch={**LAUNCH, **launch})


# --- kind, uid, launch stanza ----------------------------------------------

def test_kind_is_registered():
    assert sm.KIND_TIER3S == "tier3s"
    assert "tier3s" in sm.SILO_KINDS
    assert sm.validate_kind("tier3s") == "tier3s"


def test_uid_must_be_the_admin_launch_owner():
    assert sm.validate_silo_uid(sm.ADMIN_UID, "tier3s") == sm.ADMIN_UID
    for bad in (sm.ADMIN_UID + 1, 2001, 0):
        with pytest.raises(BadArgument, match="tier3s silo uid must be the admin"):
            sm.validate_silo_uid(bad, "tier3s")


def test_launch_stanza_normalised():
    out = sm.validate_launch("tier3s", {"workload": "headless-smoke",
                                        "template_silo": "smoke"})
    assert out == {"workload": "headless-smoke", "template_silo": "smoke",
                   "network": "none", "argv": []}
    out = sm.validate_launch("tier3s", dict(LAUNCH, argv='["a", "b c", ""]'))
    assert out["argv"] == ["a", "b c", ""]


@pytest.mark.parametrize("network", ["pasta", "slirp4netns", "host", "", None, 1])
def test_launch_network_is_none_only(network):
    with pytest.raises(BadArgument, match="network must be 'none'"):
        sm.validate_launch("tier3s", dict(LAUNCH, network=network))


@pytest.mark.parametrize("workload", ["Headless", "../x", "a/b", "-x", "", "x" * 42, None])
def test_launch_workload_is_a_spawn_workload_name(workload):
    with pytest.raises(BadArgument, match="workload"):
        sm.validate_launch("tier3s", dict(LAUNCH, workload=workload))


@pytest.mark.parametrize("binding", ["../smoke", "Smoke", "a:b", "", None, "x" * 33])
def test_launch_template_silo_is_a_silo_name(binding):
    with pytest.raises(BadArgument, match="template_silo"):
        sm.validate_launch("tier3s", dict(LAUNCH, template_silo=binding))


@pytest.mark.parametrize("argv", [["a\0b"], ["a\nb"], [1], "not json", {"a": 1}])
def test_launch_argv_constraints(argv):
    with pytest.raises(BadArgument, match="tier3s launch.argv"):
        sm.validate_launch("tier3s", dict(LAUNCH, argv=argv))


def test_tier2_launch_messages_unchanged_by_the_shared_argv_check():
    with pytest.raises(BadArgument, match="tier2-template launch.argv must be a list"):
        sm.validate_launch("tier2-template", {"workload": "w", "template_silo": "t",
                                              "network": "none", "argv": [1]})


# --- creation ---------------------------------------------------------------

def test_create_makes_no_user_cgroup_link_or_relay(store, ops):
    silo = make(store)
    assert (silo.kind, silo.uid, silo.state) == ("tier3s", sm.ADMIN_UID, State.CREATED)
    assert ops.users == {} and ops.cgroups == set()
    assert ops.launcher_links == set() and ops.relay_policies == {}
    assert ops.systemctl_calls == []


def test_create_refuses_egress(store):
    with pytest.raises(BadArgument, match="egress policy is only valid for tier3-user"):
        store.create("smoke", sm.ADMIN_UID, kind="tier3s", launch=dict(LAUNCH),
                     egress="direct")


@pytest.mark.parametrize("profile", ["daily-driver", "release", "", "prod"])
def test_create_refused_on_a_non_dev_profile(store, ops, tmp_path, profile):
    ops.profile = profile
    with pytest.raises(BadArgument) as ei:
        make(store)
    assert "tier 3s is dev-profile only" in str(ei.value)
    assert "no fallback tier" in str(ei.value)
    assert store.list_silos() == []
    assert not (tmp_path / "silos.yaml").exists()


def test_persists_and_reloads(ops, tmp_path):
    s1 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
    make(s1, argv=["qdistro-tier3s-smoke", "--x"])
    s2 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
    silo = s2.get("smoke")
    assert silo.kind == "tier3s" and silo.uid == sm.ADMIN_UID
    assert silo.launch == {"workload": "headless-smoke", "template_silo": "smoke",
                           "network": "none", "argv": ["qdistro-tier3s-smoke", "--x"]}


def _row(uid, network="none"):
    return ("silos:\n"
            "  - name: smoke\n"
            f"    uid: {uid}\n"
            "    state: Stopped\n"
            "    kind: tier3s\n"
            "    launch:\n"
            "      workload: headless-smoke\n"
            "      template_silo: smoke\n"
            f"      network: {network}\n"
            "      argv: []\n")


def test_loader_quarantines_a_row_of_another_admin_uid(ops, tmp_path, caplog):
    cfg = tmp_path / "silos.yaml"
    cfg.write_text(_row(sm.ADMIN_UID + 1))
    with caplog.at_level(logging.ERROR):
        store = _SiloStore(ops, config_path=cfg)
    assert store.list_silos() == []
    assert any("quarantining tier3s row" in r.message for r in caplog.records)
    store.save()
    assert "quarantined_silos:" in cfg.read_text()
    assert f"tier3s owner uid {sm.ADMIN_UID + 1}" in cfg.read_text()


def test_loader_drops_a_row_with_a_network(ops, tmp_path):
    cfg = tmp_path / "silos.yaml"
    cfg.write_text(_row(sm.ADMIN_UID, network="pasta"))
    assert _SiloStore(ops, config_path=cfg).list_silos() == []


def test_create_tier3s_dbus_method_creates_a_tier3s_row(store, ops):
    if sm.dbus is None:
        pytest.skip("dbus-python unavailable")
    mgr = object.__new__(sm.SessionManager)
    mgr.store = store
    mgr._peer_caller = lambda _s, _c: {"uid": 1000, "pid": 1, "exe": "/bin/t"}
    mgr._require_admin = lambda _s, _c: None
    mgr.CreateTier3sSilo("smoke", "headless-smoke", "smoke", "none",
                         sender=":1.2", conn=object())
    silo = store.get("smoke")
    assert silo.kind == "tier3s" and silo.uid == sm.ADMIN_UID
    with pytest.raises(sm.dbus.DBusException):
        mgr.CreateTier3sSilo("other", "headless-smoke", "other", "pasta",
                             sender=":1.2", conn=object())
    # CreateTemplateSilo keeps its tier-2 kind
    mgr.CreateTemplateSilo("t2", "browser", "t2", "none", sender=":1.2", conn=object())
    assert store.get("t2").kind == "tier2-template"


def test_create_tier3s_is_denied_to_non_admins_in_the_bus_policy():
    text = DBUS_CONF.read_text()
    default = text.split('<policy context="default">', 1)[1]
    assert re.search(r'<deny[^>]*send_member="CreateTier3sSilo"', default)


# --- start --------------------------------------------------------------------

def test_start_exports_the_stanza_and_starts_only_the_tier3s_unit(store, ops):
    make(store)
    store.start("smoke")
    assert store.get("smoke").state == State.ACTIVE
    assert ops.systemctl_calls == [("start", UNIT)]
    assert ops.cgroups == set(), "a tier3s start creates no per-silo cgroup"
    env = env_of(ops)
    assert set(env) == {"TIER3S_SILO", "TIER3S_BINDING", "TIER3S_WORKLOAD",
                        "TIER3S_NETWORK", "TIER3S_LAUNCH_TOKEN", "TIER3S_ARGV_JSON"}
    # paravirt ΔB5: the stanza went to the dedicated tier3s writer, never
    # the shared tier-2 launch-env store.
    assert ops.launch_envs == {}, "a tier3s stanza must never land in the tier-2 dir"
    assert env["TIER3S_SILO"] == "smoke" and env["TIER3S_BINDING"] == "smoke"
    # through the tier3s start (the Type=notify bound), never the generic one
    assert ops.t3s_bound_starts == [UNIT]
    assert env["TIER3S_WORKLOAD"] == "headless-smoke"
    assert env["TIER3S_NETWORK"] == "none"
    assert re.fullmatch(r"[0-9a-f]{32}", env["TIER3S_LAUNCH_TOKEN"])
    # the workload's default app (no argv in the row)
    assert json.loads(env["TIER3S_ARGV_JSON"]) == ["qdistro-tier3s-smoke"]


def test_each_start_commits_a_fresh_token(store, ops):
    make(store, argv=["qdistro-tier3s-smoke", "it's"])
    store.start("smoke")
    first = env_of(ops)
    assert json.loads(first["TIER3S_ARGV_JSON"]) == ["qdistro-tier3s-smoke", "it's"]
    store.stop("smoke")
    store.start("smoke")
    assert env_of(ops)["TIER3S_LAUNCH_TOKEN"] != first["TIER3S_LAUNCH_TOKEN"]


def test_start_from_active_relaunches_a_dead_sandbox(store, ops):
    # The live defect GUI scenario 59 proved (2026-10-07): after the
    # sandboxed app exits on its own the launcher tears everything down —
    # observed_status reads "stopped" but state stays Active, so a plain
    # StartSilo used to return success and launch nothing. Now it must
    # verify liveness and relaunch with a FRESH token.
    make(store)
    store.start("smoke")
    first = env_of(ops)
    ops.observe_silo = lambda *a: (
        "stopped", "launcher inactive and workload absent")
    store.start("smoke")
    assert [e for e in ops.events if e[0] == "start"] == [
        ("start", UNIT), ("start", UNIT)]
    assert store.get("smoke").state == State.ACTIVE
    assert env_of(ops)["TIER3S_LAUNCH_TOKEN"] != first["TIER3S_LAUNCH_TOKEN"]


def test_start_from_active_stays_put_when_the_sandbox_is_live(store, ops):
    # The other half of the contract: a genuinely running silo must not
    # get a second launcher (and the call reports the idempotent reason).
    make(store)
    store.start("smoke")
    store.start("smoke")   # fake probe: unit started, never stopped
    assert [e for e in ops.events if e[0] == "start"] == [("start", UNIT)]


def test_start_from_active_unknown_probe_fails_closed(store, ops):
    # e.g. the launcher is active but the container is absent — neither
    # a second workload nor a false success.
    make(store)
    store.start("smoke")
    ops.observe_silo = lambda *a: (
        "unknown", "launcher active but container absent")
    with pytest.raises(SessionError, match="cannot verify"):
        store.start("smoke")
    assert [e for e in ops.events if e[0] == "start"] == [("start", UNIT)]
    assert store.get("smoke").state == State.ACTIVE


def test_start_from_active_refuses_while_teardown_evidence_survives(store, ops):
    # observe_silo's "stopped"/"failed" verdict covers the unit and the
    # container only — it cannot see a surviving control record. The stop
    # path treats a record (or an unreadable record dir) as unresolved
    # teardown and re-runs `cleanup --unit`; a relaunch must confirm
    # death through that same tier3s_silo_running verifier rather than
    # launching a fresh token over un-reaped launch state.
    make(store)
    store.start("smoke")
    ops.observe_silo = lambda *a: (
        "stopped", "launcher inactive and workload absent")
    ops.t3s_running = True     # a control record of the unit survives
    with pytest.raises(SessionError, match="cannot verify"):
        store.start("smoke")
    assert [e for e in ops.events if e[0] == "start"] == [("start", UNIT)]
    assert store.get("smoke").state == State.ACTIVE
    # Once the verifier reports the launch genuinely gone — unit down,
    # container absent, no record — the same retry relaunches.
    ops.t3s_running = False
    store.start("smoke")
    assert [e for e in ops.events if e[0] == "start"] == [
        ("start", UNIT), ("start", UNIT)]
    assert store.get("smoke").state == State.ACTIVE


def test_template_silo_reaches_the_stanza_as_the_binding(store, ops):
    make(store, "smoke2", template_silo="browser1")
    store.start("smoke2")
    assert env_of(ops, "smoke2")["TIER3S_BINDING"] == "browser1"
    assert env_of(ops, "smoke2")["TIER3S_SILO"] == "smoke2"


def test_start_on_a_non_dev_profile_is_refused_before_any_state_change(store, ops):
    make(store)
    ops.profile = "release"
    with pytest.raises(BadArgument, match="tier 3s is dev-profile only"):
        store.start("smoke")
    assert store.get("smoke").state == State.CREATED
    assert ops.systemctl_calls == [] and ops.tier3s_launch_envs == {}


def test_failed_start_rolls_back_and_falls_back_to_nothing(store, ops):
    """paravirt O6: a failed tier3s start launches nothing else — no tier-2
    unit, no tier-3 session launcher, no cgroup."""
    make(store)
    ops.start_raises = subprocess.CalledProcessError(1, ["systemctl", "start", UNIT])
    with pytest.raises(SessionError):
        store.start("smoke")
    assert store.get("smoke").state == State.STOPPED
    assert [e for e in ops.events if e[0] == "start"] == [("start", UNIT)]
    assert ops.cgroups == set()


def test_a_refused_launch_fails_start_leaves_stopped_and_a_retry_starts(store, ops):
    """astra/fable A r1: the unit is Type=notify, so a launch the spawn
    refuses fails `systemctl start`. StartSilo reports the refusal, the silo
    is Stopped only after the launch is verified gone, and a retry once the
    cause is fixed is a real start (not an idempotent no-op from Active)."""
    make(store)
    ops.start_raises = subprocess.CalledProcessError(1, ["systemctl", "start", UNIT])
    ops.refusal = "REFUSE: broker denied headless-smoke/qdistro-tier3s-smoke"
    with pytest.raises(SessionError, match="refused or failed before it ran: REFUSE: broker denied") as ei:
        store.start("smoke")
    assert not isinstance(ei.value, sm.StartNotCancelled)
    silo = store.get("smoke")
    assert silo.state == State.STOPPED and silo.observed_status == "failed"
    assert "broker denied" in silo.observed_reason
    ev = ops.events
    assert ev.index(("running?", "smoke")) > ev.index(("start", UNIT)), "verified gone after the failed start"
    ops.start_raises = None
    store.start("smoke")
    assert store.get("smoke").state == State.ACTIVE
    assert [e for e in ops.events if e[0] == "start"] == [("start", UNIT), ("start", UNIT)]


def test_a_failed_start_whose_launch_is_not_verified_gone_stays_active(store, ops):
    make(store)
    ops.start_raises = subprocess.CalledProcessError(1, ["systemctl", "start", UNIT])
    ops.t3s_running = True
    with pytest.raises(sm.StartNotCancelled, match="could not be verified gone"):
        store.start("smoke")
    silo = store.get("smoke")
    assert silo.state == State.ACTIVE and silo.start_unresolved
    with pytest.raises(sm.SiloBusy):
        store.delete("smoke")


def test_autostart_of_a_refused_launch_ends_stopped(ops, tmp_path):
    """fable P2-1: an Active row whose launch is now refused is relaunched
    once by the next manager start and then reads Stopped, not Active."""
    cfg = tmp_path / "silos.yaml"
    s1 = _SiloStore(ops, config_path=cfg)
    make(s1)
    s1.start("smoke")
    ops.start_raises = subprocess.CalledProcessError(1, ["systemctl", "start", UNIT])
    s2 = _SiloStore(ops, config_path=cfg)
    s2.autostart_pass()
    assert s2.get("smoke").state == State.STOPPED
    s3 = _SiloStore(ops, config_path=cfg)
    n = len([e for e in ops.events if e[0] == "start"])
    s3.autostart_pass()
    assert len([e for e in ops.events if e[0] == "start"]) == n, "a Stopped row is not relaunched"


def test_unresolved_start_stays_active(store, ops):
    make(store)
    ops.start_raises = sm.StartNotCancelled("timed out")
    with pytest.raises(sm.StartNotCancelled):
        store.start("smoke")
    assert store.get("smoke").state == State.ACTIVE


# --- stop -------------------------------------------------------------------

def test_stop_stops_the_unit_verifies_and_clears_the_stanza(store, ops):
    make(store)
    store.start("smoke")
    assert "smoke" in ops.tier3s_launch_envs, "the stanza was written on start"
    store.stop("smoke")
    assert store.get("smoke").state == State.STOPPED
    assert ("stop", UNIT) in ops.systemctl_calls
    assert ("running?", "smoke") in ops.events
    # paravirt ΔB5: the verified stop removes the stanza from the dedicated
    # tier3s dir via remove_tier3s_launch_env — a call to the tier-2 remover
    # would leave it stranded here.
    assert ops.t3s_cleanups == [] and "smoke" not in ops.tier3s_launch_envs
    assert ops.launch_envs == {}, "no tier-2 stanza was ever touched"
    assert ops.cgroup_frozen == {}, "no per-silo cgroup is touched"


def test_stop_that_leaves_records_runs_the_cleanup_then_reverifies(store, ops):
    make(store)
    store.start("smoke")
    ops.t3s_running, ops.t3s_running_after_cleanup = True, False
    store.stop("smoke")
    assert ops.t3s_cleanups == [("--unit", UNIT)]
    assert store.get("smoke").state == State.STOPPED


def test_stop_fails_closed_when_the_launch_survives(store, ops):
    make(store)
    store.start("smoke")
    ops.t3s_running = True
    ops.t3s_cleanup_ok = False
    with pytest.raises(SessionError, match="did not take effect"):
        store.stop("smoke")
    assert store.get("smoke").state == State.ACTIVE
    assert "smoke" in ops.tier3s_launch_envs
    with pytest.raises(sm.SiloBusy):
        store.delete("smoke")


def test_unacknowledged_stop_stays_active_and_runs_no_cleanup(store, ops):
    make(store)
    store.start("smoke")
    ops.systemctl_stop_unacknowledged = True
    with pytest.raises(SessionError, match="not acknowledged"):
        store.stop("smoke")
    assert store.get("smoke").state == State.ACTIVE
    assert ops.t3s_cleanups == []


def test_delete_after_stop(store, ops):
    make(store)
    store.start("smoke")
    store.stop("smoke")
    store.delete("smoke")
    assert store.list_silos() == []


# --- freeze / resume -----------------------------------------------------------

@pytest.mark.parametrize("op", ["freeze", "resume"])
def test_freeze_and_resume_are_unsupported(store, ops, tmp_path, op):
    audit = sm._AuditLog(tmp_path / "audit.sqlite")
    store._audit = audit
    make(store)
    store.start("smoke")
    with pytest.raises(BadArgument, match="freeze/resume is unsupported for tier3s silos"):
        getattr(store, op)("smoke")
    assert ops.cgroup_frozen == {}
    assert store.get("smoke").state == State.ACTIVE
    row = [r for r in audit.tail() if r["action"] == op][0]
    assert row["decision"] == "deny"


# --- restart reconciliation -----------------------------------------------------

def test_reconciliation_stops_live_units_then_reaps_stale(store, ops):
    ops.t3s_live = [UNIT, "qdistro-tier3s-silo@gone.service"]
    stopped = store.reconcile_tier3s_launches()
    assert stopped == [UNIT, "qdistro-tier3s-silo@gone.service"]
    assert ops.events == [("stop", UNIT), ("stop", "qdistro-tier3s-silo@gone.service"),
                          ("cleanup", "--reap-stale")]


def test_reconciliation_is_skipped_without_the_tier3s_install(store, ops):
    ops.t3s_installed = False
    ops.t3s_live = [UNIT]
    assert store.reconcile_tier3s_launches() == []
    assert ops.events == []


def test_reconciliation_reaps_even_when_the_unit_listing_fails(store, ops):
    ops.t3s_live_raises = subprocess.CalledProcessError(1, ["systemctl"])
    store.reconcile_tier3s_launches()
    assert ops.events == [("cleanup", "--reap-stale")]


def test_restart_reconciles_before_relaunching_with_a_fresh_token(ops, tmp_path):
    """The manager's in-memory state is lost: the old launch's unit is stopped
    and stale state reaped BEFORE the autostart sweep relaunches the silo
    that was Active, with a new token."""
    cfg = tmp_path / "silos.yaml"
    s1 = _SiloStore(ops, config_path=cfg)
    make(s1)
    s1.start("smoke")
    old_token = env_of(ops)["TIER3S_LAUNCH_TOKEN"]
    ops.events.clear()
    ops.t3s_live = [UNIT]
    s2 = _SiloStore(ops, config_path=cfg)       # a new daemon
    s2.autostart_pass()
    assert ops.events[:3] == [("stop", UNIT), ("cleanup", "--reap-stale"), ("start", UNIT)]
    assert s2.get("smoke").state == State.ACTIVE
    assert env_of(ops)["TIER3S_LAUNCH_TOKEN"] != old_token


# --- _SystemOps, real methods with a recorded subprocess -----------------------

class _Rec:
    """subprocess.run stand-in: answers by argv, records every call."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        assert "timeout" in kw, f"unbounded subprocess call {argv}"
        for pred, ans in self.answers:
            if pred(argv):
                if isinstance(ans, BaseException):
                    raise ans
                rc, out = ans
                return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")
        raise AssertionError(f"unexpected call {argv}")


def _is(*words):
    return lambda argv: all(w in argv for w in words)


def _exists(argv):
    """The PMRC-wrapped `container exists` call: its argv carries the script
    element, never a bare 'exists' word (tier3s A r3 P1)."""
    return any("container exists" in str(a) for a in argv)


def _verdict(podman_rc):
    """A completed supervisor chain relaying podman's own verdict."""
    return (0, f"PMRC={podman_rc}\n")


@pytest.fixture
def real_ops(monkeypatch, tmp_path):
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    monkeypatch.setattr(sm, "TIER3S_CTL_DIR", ctl)
    # C2 model A: tier3s podman calls run as the qt3s-<silo> account, which
    # does not exist on the build host — resolve it to a fake passwd entry
    # carrying this silo's GECOS marker (the suffix is the silo name for
    # names that fit the 27-char account truncation; colliding longer names
    # need their own getpwnam override).
    real_getpwnam = sm.pwd.getpwnam
    def fake_getpwnam(name):
        if name.startswith(sm.TIER3S_SILO_ACCT_PREFIX):
            silo = name[len(sm.TIER3S_SILO_ACCT_PREFIX):]
            return sm.pwd.struct_passwd(
                (name, "x", 4242, 4242, f"qdistro tier3s silo {silo}",
                 f"/home/{name}", "/bin/bash"))
        return real_getpwnam(name)
    monkeypatch.setattr(sm.pwd, "getpwnam", fake_getpwnam)
    return _SystemOps()


def _install(monkeypatch, answers):
    rec = _Rec(answers)
    monkeypatch.setattr(sm.subprocess, "run", rec)
    return rec


def test_running_false_only_when_unit_down_container_gone_and_no_record(real_ops, monkeypatch):
    rec = _install(monkeypatch, [(_is("is-active"), (3, "inactive\n")),
                                 (_exists, _verdict(1))])
    assert real_ops.tier3s_silo_running("smoke") is False
    pm = [c for c, _ in rec.calls if any("podman" in str(a) for a in c)][0]
    # podman as the silo account (C2 model A) with the same fixed environment
    # the spawn and cleanup use, wrapped in the PMRC verdict protocol
    # (A r3 P1): the chain's own rc is never the verdict.
    assert pm[:5] == ["runuser", "-u", "qt3s-smoke", "--", "env"]
    assert "-i" in pm and "XDG_RUNTIME_DIR=/run/qdistro-tier3s-rt/4242" in pm
    assert "CONTAINERS_CONF=/usr/lib/qdistro/tier3s/containers.conf" in pm
    assert pm[-4:-2] == ["-c", 'podman container exists "$1"; printf "PMRC=%d\\n" "$?"']
    assert pm[-2:] == ["sh", "qdistro-tier3s-smoke"]


@pytest.mark.parametrize("active", ["active", "deactivating", "activating", "", "unknown"])
def test_running_true_while_the_unit_is_not_definitively_down(real_ops, monkeypatch, active):
    _install(monkeypatch, [(_is("is-active"), (0, active + "\n"))])
    assert real_ops.tier3s_silo_running("smoke") is True


@pytest.mark.parametrize("rc", [0, 125, 2])
def test_running_true_when_the_container_exists_or_the_query_fails(real_ops, monkeypatch, rc):
    _install(monkeypatch, [(_is("is-active"), (3, "failed\n")), (_exists, _verdict(rc))])
    assert real_ops.tier3s_silo_running("smoke") is True


def test_running_true_when_the_supervisor_fails_before_podman(real_ops, monkeypatch):
    """A r3 P1 (the session-manager leg): a bare rc 1 of the runuser chain is
    NOT podman's "absent". No PMRC verdict — a failed chain, garbage, or a
    verdict sharing its output — all count as still running."""
    for ans in [(1, ""), (0, ""), (0, "garbage\n"), (0, "PMRC=1\nextra\n"),
                (0, "PMRC=x\n"), (0, " PMRC=1\n")]:
        _install(monkeypatch, [(_is("is-active"), (3, "inactive\n")), (_exists, ans)])
        assert real_ops.tier3s_silo_running("smoke") is True, ans


def test_running_true_when_the_query_times_out(real_ops, monkeypatch):
    _install(monkeypatch, [(_is("is-active"), (3, "inactive\n")),
                           (_exists, subprocess.TimeoutExpired("podman", 30))])
    assert real_ops.tier3s_silo_running("smoke") is True


def _silo_pw(acct, uid, gecos):
    return sm.pwd.struct_passwd(
        (acct, "x", uid, uid, gecos, f"/home/{acct}", "/bin/bash"))


@pytest.mark.parametrize("gecos,uid", [
    ("qdistro tier3s silo other", 4242),   # the marker of ANOTHER silo
    ("An Ordinary Account", 4242),          # a foreign/recreated account
    ("qdistro tier3s silo smoke", -1),      # the admin uid (-1: resolved below)
    ("qdistro tier3s silo smoke", 80),      # a sub-1000 uid
], ids=["other-silo-marker", "foreign-account", "admin-uid", "low-uid"])
def test_silo_query_rejects_an_unbound_account(real_ops, monkeypatch, gecos, uid):
    """sol model-A r1 P2-3: the qt3s-<silo> account must carry THIS silo's
    GECOS marker on a regular non-admin uid; anything else is not this silo's
    store — the query never reaches podman and the observation is fail-closed
    'still running', never 'stopped'."""
    def fake(name):
        if name == "qt3s-smoke":
            return _silo_pw(name, sm.ADMIN_UID if uid < 0 else uid, gecos)
        raise KeyError(name)
    monkeypatch.setattr(sm.pwd, "getpwnam", fake)
    rec = _install(monkeypatch, [(_is("is-active"), (3, "inactive\n"))])
    assert real_ops.tier3s_silo_running("smoke") is True
    assert len(rec.calls) == 1            # is-active only; podman never ran


def test_silo_query_rejects_a_truncation_collision(real_ops, monkeypatch):
    """sol model-A r1 P2-3: two silo names sharing their first 27 chars map to
    ONE qt3s- account. The owner is observed through its store; the collider's
    marker does not match and the query refuses before podman — fail-closed,
    not 'stopped' on the other's absence."""
    owner, collider = "a" * 28, "a" * 27 + "b"
    acct = "qt3s-" + "a" * 27
    def fake(name):
        if name == acct:
            return _silo_pw(acct, 4242, f"qdistro tier3s silo {owner}")
        raise KeyError(name)
    monkeypatch.setattr(sm.pwd, "getpwnam", fake)
    rec = _install(monkeypatch, [(_is("is-active"), (3, "inactive\n")),
                                 (_exists, _verdict(1))])
    assert real_ops.tier3s_silo_running(owner) is False
    assert len(rec.calls) == 2
    rec = _install(monkeypatch, [(_is("is-active"), (3, "inactive\n"))])
    assert real_ops.tier3s_silo_running(collider) is True
    assert len(rec.calls) == 1            # refused before podman


def test_silo_query_with_no_account_weighs_the_record(real_ops, monkeypatch):
    """The qt3s-<silo> account is provisioned lazily at first launch. When
    NOTHING resolves under it the podman query cannot run — but that is not
    automatically 'running' (astra C2-end P2): the spawn creates the account
    (step 3b) BEFORE its control record and before the first podman call in
    its store, so no record + no account is a proven pre-provisioning
    failure and reads stopped. A record that survives an absent account is
    the missing-identity-after-provisioning case and stays fail-closed."""
    def fake(name):
        raise KeyError(name)
    monkeypatch.setattr(sm.pwd, "getpwnam", fake)
    rec = _install(monkeypatch, [(_is("is-active"), (3, "inactive\n"))])
    assert real_ops.tier3s_silo_running("smoke") is False
    assert len(rec.calls) == 1            # podman is never invoked
    ctl = sm.TIER3S_CTL_DIR / ("b" * 32)
    ctl.mkdir()
    (ctl / "state").write_text(f"schema=1\nunit={UNIT}\n")
    assert real_ops.tier3s_silo_running("smoke") is True


def test_failed_first_start_before_provisioning_recovers(store, ops,
                                                         monkeypatch,
                                                         tmp_path):
    """astra C2-end P2 lifecycle regression: a first launch refused BEFORE
    the qt3s-* account exists (a transient stanza-write or provisioning
    failure) must not wedge the silo Active forever — the real account
    observation proves nothing ran, so the start records Stopped/failed and
    an ordinary repair + start + stop + delete all work."""
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    monkeypatch.setattr(sm, "TIER3S_CTL_DIR", ctl)
    ops.tier3s_silo_running = _SystemOps().tier3s_silo_running
    # the silo account does not resolve: provisioning never ran
    def no_account(name):
        raise KeyError(name)
    monkeypatch.setattr(sm.pwd, "getpwnam", no_account)
    _install(monkeypatch, [(_is("is-active"), (3, "failed\n")),
                           (_exists, _verdict(1))])
    ops.start_raises = SessionError("the launch was refused")
    make(store)
    with pytest.raises(SessionError, match="refused or failed"):
        store.start("smoke")
    silo = store.get("smoke")
    assert silo.state == State.STOPPED
    assert not getattr(silo, "start_unresolved", False)
    # repair: provisioning now yields the bound account; start + stop + delete
    def bound(name):
        if name == "qt3s-smoke":
            return _silo_pw(name, 4242, "qdistro tier3s silo smoke")
        raise KeyError(name)
    monkeypatch.setattr(sm.pwd, "getpwnam", bound)
    ops.start_raises = None
    store.start("smoke")
    assert store.get("smoke").state == State.ACTIVE
    store.stop("smoke")
    assert store.get("smoke").state == State.STOPPED
    store.delete("smoke")
    assert store.list_silos() == []


def test_observe_unbound_silo_account_is_unknown(real_ops, monkeypatch):
    """observation leg: an unbound qt3s-<silo> account yields 'unknown', not
    'stopped' — the store that was not queried cannot vouch absence."""
    def fake(name):
        if name == "qt3s-smoke":
            return _silo_pw(name, 4242, "qdistro tier3s silo other")
        raise KeyError(name)
    monkeypatch.setattr(sm.pwd, "getpwnam", fake)
    rec = _install(monkeypatch, [
        (_is("systemctl", "show"),
         (0, "LoadState=loaded\nActiveState=inactive\nJob=\n"))])
    status, reason = real_ops.observe_silo("smoke", sm.ADMIN_UID, "tier3s")
    assert status == "unknown", reason


def test_running_true_while_a_control_record_of_the_unit_survives(real_ops, monkeypatch):
    _install(monkeypatch, [(_is("is-active"), (3, "inactive\n")), (_exists, _verdict(1))])
    rec = sm.TIER3S_CTL_DIR / ("a" * 32)
    rec.mkdir()
    (rec / "state").write_text(f"schema=1\nunit=qdistro-tier3s-silo@other.service\n")
    assert real_ops.tier3s_silo_running("smoke") is False
    (rec / "state").write_text(f"schema=1\nunit={UNIT}\n")
    assert real_ops.tier3s_silo_running("smoke") is True
    (rec / "state").unlink()            # a record dir without its state still counts
    assert real_ops.tier3s_silo_running("smoke") is True


def test_running_true_when_the_control_dir_is_unreadable(real_ops, monkeypatch):
    _install(monkeypatch, [(_is("is-active"), (3, "inactive\n")), (_exists, _verdict(1))])

    def boom(self):
        raise PermissionError("denied")
    monkeypatch.setattr(sm.Path, "iterdir", boom)
    assert real_ops.tier3s_silo_running("smoke") is True


@pytest.mark.parametrize("active,exists,running,want", [
    ("active", 0, "true", "launcher-running"),
    ("active", 0, "false", "unknown"),
    ("active", 125, "", "unknown"),
    ("active", 1, "", "unknown"),
    ("inactive", 1, "", "stopped"),
    ("failed", 1, "", "failed"),
    ("inactive", 125, "", "unknown"),
])
def test_observe(real_ops, monkeypatch, active, exists, running, want):
    rec = _install(monkeypatch, [
        (_is("systemctl", "show"),
         (0, f"LoadState=loaded\nActiveState={active}\nJob=\n")),
        (_exists, _verdict(exists)),
        (_is("inspect"), (0, running + "\n")),
    ])
    status, _reason = real_ops.observe_silo("smoke", sm.ADMIN_UID, "tier3s")
    assert status == want
    assert UNIT in rec.calls[0][0]
    assert not any("cgroup" in str(c) for c, _ in rec.calls)


def test_observe_supervisor_failure_before_podman_is_unknown(real_ops, monkeypatch):
    """A r3 P1: a bare rc 1 of the runuser chain (podman never ran) must not
    pass for podman's "absent" — without a PMRC verdict the observation is
    unknown, never 'container absent'."""
    for ans in [(1, ""), (0, ""), (0, "PMRC=1\nextra\n")]:
        _install(monkeypatch, [
            (_is("systemctl", "show"),
             (0, "LoadState=loaded\nActiveState=active\nJob=\n")),
            (_exists, ans)])
        status, reason = real_ops.observe_silo("smoke", sm.ADMIN_UID, "tier3s")
        assert status == "unknown" and "unavailable" in reason, ans


def test_live_units_parse(real_ops, monkeypatch):
    out = (f"{UNIT} loaded active running x\n"
           "qdistro-tier3s-silo@b.service loaded deactivating stop-sigterm x\n"
           "qdistro-tier3s-silo@c.service loaded failed failed x\n"
           "qdistro-tier3s-silo@d.service loaded inactive dead x\n")
    rec = _install(monkeypatch, [(_is("list-units"), (0, out))])
    assert real_ops.tier3s_live_units() == [UNIT, "qdistro-tier3s-silo@b.service"]
    assert rec.calls[0][1].get("check") is True


def test_tier3s_start_uses_the_notify_bound(real_ops, monkeypatch):
    rec = _install(monkeypatch, [(_is("start"), (0, ""))])
    real_ops.tier3s_systemctl_start(UNIT)
    argv, kw = rec.calls[0]
    assert argv == ["systemctl", "start", UNIT] and kw["timeout"] == sm._T_TIER3S_START
    assert kw.get("check") is True
    # above the unit's own TimeoutStartSec, so systemd's timeout fires first
    # (its teardown afterwards may outlast the 15 s difference: unresolved)
    assert sm._T_TIER3S_START > int(_unit_kv()["TimeoutStartSec"][0])


def test_tier3s_start_timeout_is_unresolved(real_ops, monkeypatch):
    _install(monkeypatch, [(_is("start"), subprocess.TimeoutExpired(["systemctl"], 1)),
                           (_is("stop"), (0, ""))])
    with pytest.raises(sm.StartNotCancelled):
        real_ops.tier3s_systemctl_start(UNIT)


def test_start_refusal_is_the_spawns_last_refuse_line(real_ops, monkeypatch):
    inv = "c" * 32
    _install(monkeypatch, [
        (_is("InvocationID"), (0, inv + "\n")),
        (lambda a: a[0] == "journalctl" and f"_SYSTEMD_INVOCATION_ID={inv}" in a,
         (0, "spawn-tier3s: probe ok\nspawn-tier3s: REFUSE: broker denied x (decision=deny)\n"))])
    assert real_ops.tier3s_start_refusal(UNIT) == "REFUSE: broker denied x (decision=deny)"
    _install(monkeypatch, [(_is("InvocationID"), subprocess.TimeoutExpired(["systemctl"], 1))])
    assert real_ops.tier3s_start_refusal(UNIT) == ""


def test_an_incomplete_record_blocks_no_unrelated_stop(real_ops, monkeypatch, tmp_path):
    """astra A r1 #1: a control dir without its state (interrupted creation by
    earlier code) still counts for every unit's stop verification (it is not
    ignored), and the stop's `cleanup --unit` recovers it on positive evidence,
    so an unrelated silo's stop verifies. Runs the REAL cleanup (test mode,
    the fake world of test_tier3s_spawn) through _SystemOps.tier3s_cleanup."""
    from test_tier3s_spawn import World
    w = World(tmp_path / "w")
    d = w.ctl / ("d" * 32)
    d.mkdir(mode=0o700)
    d.chmod(0o700)
    monkeypatch.setattr(sm, "TIER3S_CTL_DIR", w.ctl)
    wrapper = tmp_path / "cleanup-wrapper"
    env = w.env()
    wrapper.write_text("#!/bin/bash\nexec env -i " + " ".join(
        shlex.quote(f"{k}={v}") for k, v in env.items()) + f" bash {w.T}/usr/libexec/qdistro/qdistro-tier3s-cleanup \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setattr(sm, "TIER3S_CLEANUP", wrapper)
    real_run = subprocess.run
    _install(monkeypatch, [(_is("is-active"), (3, "inactive\n")), (_exists, _verdict(1)),
                           (lambda a: a[0] == str(wrapper), None)])
    rec = sm.subprocess.run

    def run(argv, **kw):
        if argv[0] == str(wrapper):
            rec.calls.append((list(argv), kw))
            assert "timeout" in kw
            return real_run(argv, **kw)
        return rec(argv, **kw)
    monkeypatch.setattr(sm.subprocess, "run", run)
    other = "qdistro-tier3s-silo@unrelated.service"
    assert real_ops.tier3s_silo_running("unrelated") is True       # counted, not ignored
    assert real_ops.tier3s_cleanup("--unit", other) is True
    assert not d.exists()
    assert real_ops.tier3s_silo_running("unrelated") is False


def test_cleanup_reports_failure(real_ops, monkeypatch):
    _install(monkeypatch, [(_is("--reap-stale"), (1, ""))])
    assert real_ops.tier3s_cleanup("--reap-stale") is False
    _install(monkeypatch, [(_is("--reap-stale"), (0, ""))])
    assert real_ops.tier3s_cleanup("--reap-stale") is True
    _install(monkeypatch, [(_is("--reap-stale"), FileNotFoundError("gone"))])
    assert real_ops.tier3s_cleanup("--reap-stale") is False


def _fake_lstat(uid, mode):
    real = os.lstat

    def fake(p, *a, **kw):
        st = real(p, *a, **kw)
        return os.stat_result((mode, st.st_ino, st.st_dev, st.st_nlink, uid, 0,
                               st.st_size, 0, 0, 0))
    return fake


def test_profile_is_parsed_from_a_root_owned_file(monkeypatch, tmp_path):
    f = tmp_path / "profile"
    f.write_text("# x\nQDISTRO_PROFILE=release\nQDISTRO_PROFILE='dev'\n")
    monkeypatch.setattr(sm, "QDISTRO_PROFILE_PATH", f)
    monkeypatch.setattr(sm.os, "lstat", _fake_lstat(0, 0o100644))
    assert _SystemOps().qdistro_profile() == "dev"


@pytest.mark.parametrize("uid,mode", [(1000, 0o100644), (0, 0o100664), (0, 0o100646),
                                      (0, 0o120777)])
def test_profile_from_an_untrusted_file_is_unset(monkeypatch, tmp_path, uid, mode):
    f = tmp_path / "profile"
    f.write_text("QDISTRO_PROFILE=dev\n")
    monkeypatch.setattr(sm, "QDISTRO_PROFILE_PATH", f)
    monkeypatch.setattr(sm.os, "lstat", _fake_lstat(uid, mode))
    assert _SystemOps().qdistro_profile() == ""


def test_profile_missing_is_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(sm, "QDISTRO_PROFILE_PATH", tmp_path / "nope")
    assert _SystemOps().qdistro_profile() == ""


# --- the root launch helper ------------------------------------------------------

def _helper_env(tmp_path, *, admin_uid="1000"):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "id").write_text(
        "#!/bin/bash\n"
        f'if [ "$1" = -u ] && [ "$2" = admin ]; then echo {admin_uid}; exit 0; fi\n'
        'exec /usr/bin/id "$@"\n')
    (bin_ / "id").chmod(0o755)
    rec = tmp_path / "spawn-record"
    spawn = tmp_path / "spawn-tier3s.sh"
    spawn.write_text("#!/bin/bash\n"
                     f'{{ env | LC_ALL=C sort; printf "ARG=%s\\n" "$@"; }} > {rec}\n')
    spawn.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TIER3S_", "QDISTRO_"))}
    env.update(PATH=f"{bin_}:{os.environ['PATH']}",
               QDISTRO_TIER3S_LAUNCH_ENV_DIR=str(tmp_path / "tier3s-launch"),
               QDISTRO_TIER3S_SPAWN=str(spawn))
    return env, rec


def _stanza_from_the_store(tmp_path, monkeypatch, argv=None, name="smoke") -> Path:
    """Write the stanza with the REAL store + _SystemOps.write_tier3s_launch_env."""
    monkeypatch.setattr(sm, "TIER3S_LAUNCH_ENV_DIR", tmp_path / "tier3s-launch")

    class Ops(_T3sOps):
        write_tier3s_launch_env = _SystemOps.write_tier3s_launch_env
        _write_launch_env_in = _SystemOps._write_launch_env_in

    ops = Ops()
    store = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
    make(store, name, argv=argv or [])
    store.start(name)
    return tmp_path / "tier3s-launch" / f"{name}.env"


def _run_helper(env, name="smoke", **extra):
    return subprocess.run(["bash", str(LAUNCH_HELPER), name], env={**env, **extra},
                          capture_output=True, text=True, timeout=30)


def _record(rec):
    lines = rec.read_text().splitlines()
    env = dict(l.split("=", 1) for l in lines if not l.startswith("ARG=") and "=" in l)
    return env, [l[4:] for l in lines if l.startswith("ARG=")]


def test_helper_execs_the_spawn_with_exactly_the_stanza(tmp_path, monkeypatch):
    f = _stanza_from_the_store(tmp_path, monkeypatch,
                               argv=["qdistro-tier3s-smoke", "a b", "", "it's", "$(x)"])
    token = shlex.split(f.read_text().split("TIER3S_LAUNCH_TOKEN=", 1)[1].splitlines()[0])[0]
    env, rec = _helper_env(tmp_path)
    r = _run_helper(env, TIER3S_SECCOMP_PROFILE="/tmp/x", QDISTRO_PROFILE="dev",
                    TIER3S_ALLOW_PRIVESC="1")
    assert r.returncode == 0, r.stderr
    got, args = _record(rec)
    assert {k: v for k, v in got.items() if k not in ("PWD", "SHLVL", "_")} == {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "NOTIFY_SOCKET": "",
        "TIER3S_ROOT_LAUNCHER": "1", "TIER3S_ADMIN_UID": "1000",
        "TIER3S_LAUNCH_UNIT": UNIT, "TIER3S_LAUNCH_TOKEN": token,
        "TIER3S_SILO": "smoke", "TIER3S_BINDING": "smoke", "TIER3S_NETWORK": "none"}
    assert args == ["headless-smoke", "--", "qdistro-tier3s-smoke", "a b", "", "it's", "$(x)"]
    # systemd's notify socket (Type=notify) is the one variable passed through
    r = _run_helper(env, NOTIFY_SOCKET="/run/systemd/notify")
    assert r.returncode == 0, r.stderr
    assert _record(rec)[0]["NOTIFY_SOCKET"] == "/run/systemd/notify"


def test_helper_parses_and_never_sources_the_stanza(tmp_path):
    env, rec = _helper_env(tmp_path)
    d = tmp_path / "tier3s-launch"
    d.mkdir()
    marker = tmp_path / "pwned"
    (d / "smoke.env").write_text(
        "TIER3S_SILO=smoke\n"
        f"TIER3S_BINDING=smoke;touch\\ {marker}\n"
        "TIER3S_WORKLOAD=headless-smoke\nTIER3S_NETWORK=none\n"
        f"TIER3S_LAUNCH_TOKEN={'a' * 32}\n"
        f"TIER3S_ARGV_JSON='[\"qdistro-tier3s-smoke\", \"$(touch {marker})\"]'\n")
    (d / "smoke.env").chmod(0o600)
    r = _run_helper(env)
    assert r.returncode == 0, r.stderr
    got, args = _record(rec)
    assert got["TIER3S_BINDING"] == f"smoke;touch {marker}"     # passed as data
    assert args[-1] == f"$(touch {marker})"
    assert not marker.exists()


def _write_stanza(tmp_path, **over):
    kv = {"TIER3S_SILO": "smoke", "TIER3S_BINDING": "smoke",
          "TIER3S_WORKLOAD": "headless-smoke", "TIER3S_NETWORK": "none",
          "TIER3S_LAUNCH_TOKEN": "a" * 32,
          "TIER3S_ARGV_JSON": '["qdistro-tier3s-smoke"]'}
    kv.update(over)
    d = tmp_path / "tier3s-launch"
    d.mkdir(exist_ok=True)
    f = d / "smoke.env"
    f.write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in kv.items() if v is not None))
    f.chmod(0o600)
    return f


@pytest.mark.parametrize("over,needle", [
    ({"TIER3S_NETWORK": "pasta"}, "TIER3S_NETWORK must be none"),
    ({"TIER3S_LAUNCH_TOKEN": "XYZ"}, "32 lowercase hex"),
    ({"TIER3S_ARGV_JSON": "[]"}, "non-empty argv[0]"),
    ({"TIER3S_ARGV_JSON": '[""]'}, "non-empty argv[0]"),
    ({"TIER3S_BINDING": None}, "missing"),
    ({"TIER3S_SECCOMP_PROFILE": "/tmp/x"}, "unexpected key"),
    ({"TIER3S_SILO": "other"}, "names silo 'other'"),
    ({"TIER3S_ARGV_JSON": '["qdistro-tier3s-smoke", "a\\u0000b"]'}, "NUL in a value"),
], ids=["network", "token", "empty-argv", "empty-argv0", "missing-key", "unknown-key",
        "silo-mismatch", "nul"])
def test_helper_refuses_a_bad_stanza(tmp_path, over, needle):
    _write_stanza(tmp_path, **over)
    env, rec = _helper_env(tmp_path)
    r = _run_helper(env)
    assert r.returncode == 2, r.stderr
    assert needle in r.stderr
    assert not rec.exists(), "the spawn must not run"


def test_helper_refuses_a_duplicate_key(tmp_path):
    f = _write_stanza(tmp_path)
    f.write_text(f.read_text() + "TIER3S_NETWORK=none\n")
    env, rec = _helper_env(tmp_path)
    r = _run_helper(env)
    assert r.returncode == 2 and "duplicate key" in r.stderr and not rec.exists()


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666], ids=["g+w", "o+w", "a+w"])
def test_helper_refuses_a_writable_stanza(tmp_path, mode):
    _write_stanza(tmp_path).chmod(mode)
    env, rec = _helper_env(tmp_path)
    r = _run_helper(env)
    assert r.returncode == 2 and "writable" in r.stderr and not rec.exists()


def test_helper_refuses_a_symlinked_stanza(tmp_path):
    real = _write_stanza(tmp_path)
    moved = tmp_path / "elsewhere.env"
    real.rename(moved)
    (tmp_path / "tier3s-launch" / "smoke.env").symlink_to(moved)
    env, rec = _helper_env(tmp_path)
    r = _run_helper(env)
    assert r.returncode == 2 and "not a regular file" in r.stderr and not rec.exists()


def test_helper_refuses_a_wrong_admin_uid_and_a_missing_spawn(tmp_path):
    _write_stanza(tmp_path)
    env, rec = _helper_env(tmp_path, admin_uid="1001")
    r = _run_helper(env)
    assert r.returncode == 6 and "uid 1000" in r.stderr and not rec.exists()
    env, rec = _helper_env(tmp_path)
    r = _run_helper(env, QDISTRO_TIER3S_SPAWN=str(tmp_path / "nope"))
    assert r.returncode == 5 and "not installed" in r.stderr


@pytest.mark.parametrize("name", ["Bad", "../x", "a b", ""])
def test_helper_refuses_a_bad_silo_name(tmp_path, name):
    env, rec = _helper_env(tmp_path)
    r = _run_helper(env, name=name)
    assert r.returncode != 0 and not rec.exists()


def test_helper_test_overrides_are_ignored_for_root():
    src = LAUNCH_HELPER.read_text()
    guard = src.index('if [ "$EUID" -ne 0 ]; then')
    assert src.index("QDISTRO_TIER3S_LAUNCH_ENV_DIR") > guard
    assert src.index("QDISTRO_TIER3S_SPAWN") > guard
    assert "SPAWN=/usr/lib/qdistro/tier3s/spawn-tier3s.sh" in src
    assert ". \"$ENV_FILE\"" not in src and "source " not in src


# --- the dedicated stanza dir (paravirt ΔB5) ---------------------------------

def test_stanza_dir_is_dedicated_and_root_0700_everywhere():
    """ΔB5: tier3s stanzas have their own root-0700 dir — the manager constant,
    the helper's default ENV_DIR, and the tmpfiles line all agree, and none of
    them is the shared tier-2 /run/qdistro/silo-launch."""
    assert str(sm.TIER3S_LAUNCH_ENV_DIR) == "/run/qdistro/tier3s-launch"
    assert sm.TIER3S_LAUNCH_ENV_DIR != sm.TIER2_LAUNCH_ENV_DIR
    src = LAUNCH_HELPER.read_text()
    assert "ENV_DIR=/run/qdistro/tier3s-launch" in src
    # The default is fixed; only a NON-ROOT caller may override it (unit tests).
    guard = src.index('if [ "$EUID" -ne 0 ]; then')
    assert src.index("QDISTRO_TIER3S_LAUNCH_ENV_DIR") > guard
    conf = (REPO / "tier3s" / "tmpfiles" / "qdistro-tier3s.conf").read_text()
    assert re.search(r"^d /run/qdistro/tier3s-launch\s+0700 root root", conf, re.M)


def test_tier2_helper_never_reads_a_tier3s_stanza_and_vice_versa():
    """ΔB5's whole point: the tier-2 helper keeps its shared dir, the tier3s
    helper its own, and neither path crosses."""
    t2 = (REPO / "session_manager" / "qdistro-tier2-silo-launch").read_text()
    t3s = LAUNCH_HELPER.read_text()
    assert "ENV_DIR=\"${QDISTRO_SILO_LAUNCH_ENV_DIR:-/run/qdistro/silo-launch}\"" in t2
    assert "tier3s-launch" not in t2
    assert "/run/qdistro/silo-launch" not in t3s
    # and the writers are likewise dedicated
    mgr = (REPO / "session_manager" / "qdistro_session_manager.py").read_text()
    assert "self._ops.write_tier3s_launch_env(" in mgr
    assert "self._ops.remove_tier3s_launch_env(silo_name)" in mgr


def test_real_write_tier3s_launch_env_enforces_dir_mode(tmp_path, monkeypatch):
    """The tmpfiles dir is enforced, not assumed: a pre-existing loose dir is
    tightened to 0700 on write (the plain mkdir default 0755 is not a
    substitute), and the stanza file itself is 0600."""
    import stat as _stat
    d = tmp_path / "tier3s-launch"
    d.mkdir()
    os.chmod(d, 0o755)          # a drifted/loose dir, explicit — umask-proof
    assert _stat.S_IMODE(d.stat().st_mode) == 0o755
    monkeypatch.setattr(sm, "TIER3S_LAUNCH_ENV_DIR", d)
    p = sm._SystemOps().write_tier3s_launch_env("smoke", "TIER3S_SILO='smoke'\n")
    assert _stat.S_IMODE(os.stat(d).st_mode) == 0o700, oct(d.stat().st_mode)
    assert _stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_real_write_tier3s_launch_env_refuses_a_foreign_group(tmp_path,
                                                             monkeypatch):
    """sol B-ii P2-1: a root-owned dir with a non-root gid is NOT root:root
    — the gid is verified, not just the uid."""
    d = tmp_path / "tier3s-launch"
    d.mkdir()
    monkeypatch.setattr(sm, "TIER3S_LAUNCH_ENV_DIR", d)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    real_lstat = os.lstat

    def fake_lstat(p, *a, **k):
        st = real_lstat(p, *a, **k)
        # lstat reports uid 0 but a foreign gid (the best-effort chown failed)
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev,
                               st.st_nlink, 0, 65534, st.st_size,
                               int(st.st_atime), int(st.st_mtime),
                               int(st.st_ctime)))

    monkeypatch.setattr(os, "lstat", fake_lstat)
    with pytest.raises(PermissionError, match="not root:root"):
        sm._SystemOps().write_tier3s_launch_env("smoke", "TIER3S_SILO='smoke'\n")
    assert not (d / "smoke.env").exists()


def test_real_write_tier3s_launch_env_refuses_a_symlinked_dir(tmp_path,
                                                            monkeypatch):
    """Fail closed: a symlinked stanza dir is refused, never written into."""
    real = tmp_path / "real-dir"
    real.mkdir()
    link = tmp_path / "tier3s-launch"
    link.symlink_to(real)
    monkeypatch.setattr(sm, "TIER3S_LAUNCH_ENV_DIR", link)
    with pytest.raises(PermissionError):
        sm._SystemOps().write_tier3s_launch_env("smoke", "TIER3S_SILO='smoke'\n")
    assert not (real / "smoke.env").exists()


def test_tier3s_stanza_never_lands_in_the_tier2_dir(tmp_path, monkeypatch):
    """A tier3s write under the REAL ops touches only the tier3s dir; the
    tier-2 dir and the podapp dir stay empty."""
    t2 = tmp_path / "silo-launch"
    t2.mkdir()
    monkeypatch.setattr(sm, "TIER3S_LAUNCH_ENV_DIR", tmp_path / "tier3s-launch")
    monkeypatch.setattr(sm, "TIER2_LAUNCH_ENV_DIR", t2)
    p = sm._SystemOps().write_tier3s_launch_env("smoke", "TIER3S_SILO='smoke'\n")
    assert p == tmp_path / "tier3s-launch" / "smoke.env"
    assert p.exists() and list(t2.iterdir()) == []
    # and the remover unlinks only in the tier3s dir
    sm._SystemOps().remove_tier3s_launch_env("smoke")
    assert not p.exists()


def test_default_argv_for_the_gui_workloads():
    """ΔB5: the Phase B PoC apps (paravirt O2) start themselves when the
    stanza carries no argv."""
    assert sm.TIER3S_DEFAULT_ARGV["weston-terminal"] == ["weston-terminal"]
    assert sm.TIER3S_DEFAULT_ARGV["foot"] == ["foot"]
    assert sm.TIER3S_DEFAULT_ARGV["headless-smoke"] == ["qdistro-tier3s-smoke"]


@pytest.mark.parametrize("workload,argv", [("weston-terminal", ["weston-terminal"]),
                                           ("foot", ["foot"])])
def test_start_of_a_gui_workload_exports_its_default_argv(store, ops,
                                                          workload, argv):
    make(store, workload=workload)
    store.start("smoke")
    assert json.loads(env_of(ops)["TIER3S_ARGV_JSON"]) == argv


# --- unit file and installer ----------------------------------------------------

def _unit_kv():
    kv = {}
    for ln in UNIT_FILE.read_text().splitlines():
        if ln and not ln.startswith(("#", "[")) and "=" in ln:
            k, v = ln.split("=", 1)
            kv.setdefault(k, []).append(v)
    return kv


def test_unit_file_shape():
    kv = _unit_kv()
    assert kv["User"] == ["root"]
    assert kv["ExecStart"] == ["/usr/libexec/qdistro/qdistro-tier3s-silo-launch %i"]
    assert kv["ExecStop"] == ["-/usr/libexec/qdistro/qdistro-tier3s-cleanup --unit %n"]
    assert kv["ExecStopPost"] == ["/usr/libexec/qdistro/qdistro-tier3s-cleanup --unit %n"]
    assert kv["SuccessExitStatus"] == ["143 137"]
    # astra/fable A r1: a refused launch must fail the start job; astra A r2
    # #4: only the main PID (the root spawn) may acknowledge it, never an
    # admin process in the unit's cgroup (VM: s121 step 7)
    assert kv["Type"] == ["notify"] and kv["NotifyAccess"] == ["main"]
    assert kv["TimeoutStartSec"] == ["120"]
    assert kv["Restart"] == ["no"]
    assert "PartOf" not in kv, "a manager restart must not restart the launch (reconciliation does)"
    # paravirt O11: a manager STOP stops every tier3s launch unit (stop only,
    # never restart), and After= makes the launch stop first
    assert kv["StopPropagatedFrom"] == ["qdistro-session-manager.service"]
    assert kv["After"] == ["qdistro-session-manager.service"]
    for k in ("BindsTo", "Requires", "Requisite", "Upholds", "PropagatesReloadTo", "ReloadPropagatedFrom"):
        assert k not in kv, f"{k}= would couple the launch to manager restarts"
    # the unit name the manager starts and the record's unit= match the helper's
    assert sm.TIER3S_SILO_LAUNCHER_FMT.format(name="x") == "qdistro-tier3s-silo@x.service"
    assert 'TIER3S_LAUNCH_UNIT="qdistro-tier3s-silo@${NAME}.service"' in LAUNCH_HELPER.read_text()


def _installs():
    """(source, destination) of every `install` line in the tier3s block,
    with the block's variables expanded."""
    text = INSTALLER.read_text().replace("\\\n", " ")
    block = text.split("# Tier 3s (gVisor runsc", 1)[1].split('"$SRC/qdistro_silo_launch.py"', 1)[0]
    subst = {"$_qd_t3s_src": "tier3s", "$_qd_t3s_lib": "/usr/lib/qdistro/tier3s",
             "$SRC": "session_manager", "$DEST": "/usr/libexec/qdistro"}
    out = []
    for ln in block.splitlines():
        ln = ln.strip()
        if not ln.startswith("install -o root -g root -m") or "$_qd_f" in ln:
            continue      # the seccomp loop is checked on its own
        for k, v in subst.items():
            ln = ln.replace(k, v)
        parts = shlex.split(ln)
        if len(parts) == 9:              # install -o root -g root -m MODE SRC DEST
            out.append((parts[7], parts[8], parts[6]))
    return block, out


def test_installer_installs_exactly_the_contract_paths():
    block, inst = _installs()
    got = {(s, d) for s, d, _m in inst}
    for f in ("spawn-tier3s.sh", "probe.sh", "tier3s-runsc", "RUNSC_RELEASE"):
        assert (f"tier3s/{f}", f"/usr/lib/qdistro/tier3s/{f}") in got
    assert re.search(r'for _qd_f in "\$_qd_t3s_src"/seccomp/\*\.json; do\n\s+install -o root -g root '
                     r'-m 0644 "\$_qd_f" "\$_qd_t3s_lib/seccomp/\$\(basename "\$_qd_f"\)"', block)
    for f in ("qdistro-tier3s-scope", "qdistro-tier3s-cleanup"):
        assert (f"tier3s/{f}", f"/usr/libexec/qdistro/{f}") in got
    assert ("tier3s/tmpfiles/qdistro-tier3s.conf", "/usr/lib/tmpfiles.d/qdistro-tier3s.conf") in got
    assert ("session_manager/qdistro-tier3s-silo@.service",
            "/etc/systemd/system/qdistro-tier3s-silo@.service") in got
    assert ("session_manager/qdistro-tier3s-silo-launch",
            "/usr/libexec/qdistro/qdistro-tier3s-silo-launch") in got
    modes = {d: m for _s, d, m in inst}
    for d in ("/usr/lib/qdistro/tier3s/spawn-tier3s.sh", "/usr/libexec/qdistro/qdistro-tier3s-scope",
              "/usr/libexec/qdistro/qdistro-tier3s-cleanup",
              "/usr/libexec/qdistro/qdistro-tier3s-silo-launch"):
        assert modes[d] == "0755"
    assert modes["/usr/lib/tmpfiles.d/qdistro-tier3s.conf"] == "0644"
    assert "systemd-tmpfiles --create /usr/lib/tmpfiles.d/qdistro-tier3s.conf" in block


def test_installer_does_not_install_or_provision_runsc():
    block, inst = _installs()
    assert "provision-runsc" not in "".join(s for s, _d, _m in inst)
    code = "\n".join(l for l in block.splitlines() if not l.lstrip().startswith("#"))
    assert "provision-runsc.sh" not in code and "gvisor" not in code.lower()
    assert not any(d.startswith("/usr/libexec/qdistro/runsc") or d.endswith("/tier3s-runsc")
                   and d.startswith("/usr/libexec") for _s, d, _m in inst)


def test_installed_sources_exist():
    _block, inst = _installs()
    for s, _d, _m in inst:
        assert (REPO / s).is_file(), s
    assert (REPO / "tier3s/seccomp/headless-smoke.json").is_file()


# --- installer opt-in (paravirt O10) ---------------------------------------------

def _t3s_installer_block() -> str:
    """The installer's tier3s section, verbatim: from its header comment up to
    the end marker. The tests below EXECUTE this text (the real installer
    lines), with `install`, `live_only`, `systemd-tmpfiles` and `groupadd`
    replaced by recorders, so a guard change in the installer changes what
    they see."""
    text = INSTALLER.read_text()
    start = text.index("# Tier 3s (gVisor runsc; Experimental, dev profile only)")
    end = text.index("# --- end tier 3s ---", start)
    return text[start:end]


def _run_t3s_block(tmp_path, env_value):
    log = tmp_path / "calls.log"
    script = (
        "set -eu\n"
        f"SRC={shlex.quote(str(REPO / 'session_manager'))}\n"
        "DEST=/usr/libexec/qdistro\n"
        f"LOG={shlex.quote(str(log))}\n"
        'install() { printf "install %s\\n" "$*" >> "$LOG"; }\n'
        'live_only() { printf "live_only %s\\n" "$1" >> "$LOG"; }\n'
        'systemd-tmpfiles() { printf "tmpfiles %s\\n" "$*" >> "$LOG"; }\n'
        'groupadd() { printf "groupadd %s\\n" "$*" >> "$LOG"; }\n'
        + _t3s_installer_block()
        + 'echo "BLOCK-END"\n'
    )
    env = {k: v for k, v in os.environ.items() if k != "QDISTRO_TIER3S"}
    if env_value is not None:
        env["QDISTRO_TIER3S"] = env_value
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls


@pytest.mark.parametrize("value", [None, "", "0"])
def test_installer_installs_nothing_tier3s_without_the_flag(tmp_path, value):
    r, calls = _run_t3s_block(tmp_path, value)
    assert r.returncode == 0, r.stderr
    assert "BLOCK-END" in r.stdout
    assert calls == [], calls
    assert "tier 3s not installed (QDISTRO_TIER3S is not 1" in r.stdout


def test_installer_installs_the_contract_paths_with_the_flag(tmp_path):
    r, calls = _run_t3s_block(tmp_path, "1")
    assert r.returncode == 0, r.stderr
    dests = {c.split()[-1] for c in calls if c.startswith("install -o root")}
    seccomp = {f"/usr/lib/qdistro/tier3s/seccomp/{p.name}" for p in (REPO / "tier3s/seccomp").glob("*.json")}
    decls = {f"/usr/lib/qdistro/tier3s/workloads/{p.name}" for p in (REPO / "tier3s/workloads").glob("*.env")}
    cfiles = {f"/usr/lib/qdistro/tier3s/{p.name}" for p in REPO.glob("tier3s/Containerfile.*")}
    want = {"/usr/lib/qdistro/tier3s/spawn-tier3s.sh", "/usr/lib/qdistro/tier3s/probe.sh",
            "/usr/lib/qdistro/tier3s/tier3s-runsc", "/usr/lib/qdistro/tier3s/RUNSC_RELEASE",
            "/usr/lib/qdistro/tier3s/containers.conf",
            "/usr/lib/qdistro/tier3s/qdistro-tier3s-entrypoint", "/usr/lib/qdistro/tier3s/make-tier3s-image.sh",
            "/usr/lib/qdistro/tier3s/headless-smoke.sh", "/usr/lib/qdistro/tier3s/configure-snapshot-repos.sh",
            "/usr/libexec/qdistro/qdistro-tier3s-scope", "/usr/libexec/qdistro/qdistro-tier3s-cleanup",
            "/usr/lib/tmpfiles.d/qdistro-tier3s.conf", "/etc/systemd/system/qdistro-tier3s-silo@.service",
            "/usr/libexec/qdistro/qdistro-tier3s-silo-launch"} | seccomp | decls | cfiles
    assert dests == want, dests ^ want
    assert "live_only systemd-tmpfiles --create qdistro-tier3s.conf" in calls
    # C2 model A: the silo-group marker the spawn requires (accounts are
    # created at first launch, but the group is an install-time fact)
    assert "groupadd --force qdistro-tier3s" in calls
    assert "not installed" not in r.stdout


@pytest.mark.parametrize("value", ["yes", "true", "2", " 1"])
def test_installer_refuses_an_unrecognised_flag_value(tmp_path, value):
    r, calls = _run_t3s_block(tmp_path, value)
    assert r.returncode == 2 and "QDISTRO_TIER3S must be 0 or 1" in r.stderr
    assert calls == [] and "BLOCK-END" not in r.stdout


def test_installer_has_no_tier3s_lines_outside_the_gated_block():
    text = INSTALLER.read_text()
    block = _t3s_installer_block()
    rest = text.replace(block, "")
    code = [ln for ln in rest.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if "tier3s" in ln.lower() or "t3s" in ln], code
