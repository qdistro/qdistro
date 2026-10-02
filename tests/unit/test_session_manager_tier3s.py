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
    for ln in ops.launch_envs[name].splitlines():
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
    assert env["TIER3S_SILO"] == "smoke" and env["TIER3S_BINDING"] == "smoke"
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
    assert ops.systemctl_calls == [] and ops.launch_envs == {}


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
    store.stop("smoke")
    assert store.get("smoke").state == State.STOPPED
    assert ("stop", UNIT) in ops.systemctl_calls
    assert ("running?", "smoke") in ops.events
    assert ops.t3s_cleanups == [] and "smoke" not in ops.launch_envs
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
    assert "smoke" in ops.launch_envs
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


@pytest.fixture
def real_ops(monkeypatch, tmp_path):
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    monkeypatch.setattr(sm, "TIER3S_CTL_DIR", ctl)
    return _SystemOps()


def _install(monkeypatch, answers):
    rec = _Rec(answers)
    monkeypatch.setattr(sm.subprocess, "run", rec)
    return rec


def test_running_false_only_when_unit_down_container_gone_and_no_record(real_ops, monkeypatch):
    rec = _install(monkeypatch, [(_is("is-active"), (3, "inactive\n")),
                                 (_is("exists"), (1, ""))])
    assert real_ops.tier3s_silo_running("smoke") is False
    pm = [c for c, _ in rec.calls if "podman" in c][0]
    # podman as admin with the same fixed environment the spawn and cleanup use
    assert pm[:5] == ["runuser", "-u", "admin", "--", "env"]
    assert "-i" in pm and "XDG_RUNTIME_DIR=/run/user/1000" in pm
    assert pm[-3:] == ["container", "exists", "qdistro-tier3s-smoke"]


@pytest.mark.parametrize("active", ["active", "deactivating", "activating", "", "unknown"])
def test_running_true_while_the_unit_is_not_definitively_down(real_ops, monkeypatch, active):
    _install(monkeypatch, [(_is("is-active"), (0, active + "\n"))])
    assert real_ops.tier3s_silo_running("smoke") is True


@pytest.mark.parametrize("rc", [0, 125, 2])
def test_running_true_when_the_container_exists_or_the_query_fails(real_ops, monkeypatch, rc):
    _install(monkeypatch, [(_is("is-active"), (3, "failed\n")), (_is("exists"), (rc, ""))])
    assert real_ops.tier3s_silo_running("smoke") is True


def test_running_true_when_the_query_times_out(real_ops, monkeypatch):
    _install(monkeypatch, [(_is("is-active"), (3, "inactive\n")),
                           (_is("exists"), subprocess.TimeoutExpired("podman", 30))])
    assert real_ops.tier3s_silo_running("smoke") is True


def test_running_true_while_a_control_record_of_the_unit_survives(real_ops, monkeypatch):
    _install(monkeypatch, [(_is("is-active"), (3, "inactive\n")), (_is("exists"), (1, ""))])
    rec = sm.TIER3S_CTL_DIR / ("a" * 32)
    rec.mkdir()
    (rec / "state").write_text(f"schema=1\nunit=qdistro-tier3s-silo@other.service\n")
    assert real_ops.tier3s_silo_running("smoke") is False
    (rec / "state").write_text(f"schema=1\nunit={UNIT}\n")
    assert real_ops.tier3s_silo_running("smoke") is True
    (rec / "state").unlink()            # a record dir without its state still counts
    assert real_ops.tier3s_silo_running("smoke") is True


def test_running_true_when_the_control_dir_is_unreadable(real_ops, monkeypatch):
    _install(monkeypatch, [(_is("is-active"), (3, "inactive\n")), (_is("exists"), (1, ""))])

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
        (_is("exists"), (exists, "")),
        (_is("inspect"), (0, running + "\n")),
    ])
    status, _reason = real_ops.observe_silo("smoke", sm.ADMIN_UID, "tier3s")
    assert status == want
    assert UNIT in rec.calls[0][0]
    assert not any("cgroup" in str(c) for c, _ in rec.calls)


def test_live_units_parse(real_ops, monkeypatch):
    out = (f"{UNIT} loaded active running x\n"
           "qdistro-tier3s-silo@b.service loaded deactivating stop-sigterm x\n"
           "qdistro-tier3s-silo@c.service loaded failed failed x\n"
           "qdistro-tier3s-silo@d.service loaded inactive dead x\n")
    rec = _install(monkeypatch, [(_is("list-units"), (0, out))])
    assert real_ops.tier3s_live_units() == [UNIT, "qdistro-tier3s-silo@b.service"]
    assert rec.calls[0][1].get("check") is True


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
               QDISTRO_SILO_LAUNCH_ENV_DIR=str(tmp_path / "silo-launch"),
               QDISTRO_TIER3S_SPAWN=str(spawn))
    return env, rec


def _stanza_from_the_store(tmp_path, monkeypatch, argv=None, name="smoke") -> Path:
    """Write the stanza with the REAL store + _SystemOps.write_launch_env."""
    monkeypatch.setattr(sm, "TIER2_LAUNCH_ENV_DIR", tmp_path / "silo-launch")

    class Ops(_T3sOps):
        write_launch_env = _SystemOps.write_launch_env
        _write_launch_env_in = _SystemOps._write_launch_env_in

    ops = Ops()
    store = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
    make(store, name, argv=argv or [])
    store.start(name)
    return tmp_path / "silo-launch" / f"{name}.env"


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
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "TIER3S_ROOT_LAUNCHER": "1", "TIER3S_ADMIN_UID": "1000",
        "TIER3S_LAUNCH_UNIT": UNIT, "TIER3S_LAUNCH_TOKEN": token,
        "TIER3S_SILO": "smoke", "TIER3S_BINDING": "smoke", "TIER3S_NETWORK": "none"}
    assert args == ["headless-smoke", "--", "qdistro-tier3s-smoke", "a b", "", "it's", "$(x)"]


def test_helper_parses_and_never_sources_the_stanza(tmp_path):
    env, rec = _helper_env(tmp_path)
    d = tmp_path / "silo-launch"
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
    d = tmp_path / "silo-launch"
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
], ids=["network", "token", "empty-argv", "empty-argv0", "missing-key", "unknown-key",
        "silo-mismatch"])
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
    (tmp_path / "silo-launch" / "smoke.env").symlink_to(moved)
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
    assert src.index("QDISTRO_SILO_LAUNCH_ENV_DIR") > guard
    assert src.index("QDISTRO_TIER3S_SPAWN") > guard
    assert "SPAWN=/usr/lib/qdistro/tier3s/spawn-tier3s.sh" in src
    assert ". \"$ENV_FILE\"" not in src and "source " not in src


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
    assert kv["Restart"] == ["no"]
    assert "PartOf" not in kv, "a manager restart must not restart the launch (reconciliation does)"
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
