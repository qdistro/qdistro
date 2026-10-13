"""Executable behaviour tests for the tier-5 VM lifecycle.

spawn-tier5.sh and qdistro-tier5-cleanup.sh run unmodified against a stub
host (tier5_fakes.py): fake virsh/qemu-img/pkexec on PATH, a fake guest that
is a real process the scripts' own /proc liveness oracle recognises, and
real flock(1) locks in a private XDG_RUNTIME_DIR. Every test asserts
resource postconditions — domain definitions, live qemu processes, overlay
files, bridge clients — not script text.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
from tier5_fakes import (
    BRIDGE_PATH,
    CLEANUP,
    SPAWN,
    FakeHost,
    derived_name,
    finish,
    manifest_for,
)

pytestmark = pytest.mark.slow

APP = "tier5/foot"
OTHER = "tier5/firefox"
VM = derived_name(APP)


@pytest.fixture
def host(tmp_path):
    h = FakeHost(tmp_path)
    yield h
    h.close()


def _session_up(host, vm=VM, count=1):
    host.wait_log(f"GUEST-EXEC {vm}", count=count)


def _app_exit(host, vm=VM, session=None):
    """End the published app of every session on vm, or of the n-th
    guest-exec (0-based) only."""
    if session is None:
        host.flag(f"app-exit-{vm}")
    else:
        host.flag(f"app-exit-pid-{4242 + session}")


def _torn_down(host, vm=VM):
    return (not host.defined(vm) and not host.qemu_alive(vm)
            and not host.overlay(vm).exists()
            and not Path(f"{host.overlay(vm)}.appid").exists())


# -- privilege split --------------------------------------------------------

def test_lifecycle_refuses_root(host, tmp_path):
    # ensures: the rewritten lifecycle never runs as root — root file
    #          operations inside admin-writable dirs were the bug class
    fakeid = tmp_path / "rootbin"
    fakeid.mkdir()
    (fakeid / "id").write_text("#!/bin/sh\necho 0\n")
    (fakeid / "id").chmod(0o755)
    for script in ("spawn", "cleanup"):
        env = host.env()
        env["PATH"] = f"{fakeid}:{env['PATH']}"
        argv = (["bash", str(CLEANUP), VM] if script == "cleanup" else
                ["bash", str(SPAWN), "--vm", VM, "--", "foot"])
        r = subprocess.run(argv, env=env, capture_output=True, text=True,
                           timeout=30, check=False)
        assert r.returncode == 2, (script, r.stderr)
        assert "not root" in r.stderr
    assert host.log("bridge.log") == ""
    assert host.log() == ""


def test_bridge_gets_only_fixed_path_and_bounded_args(host):
    # ensures: the one privileged step is pkexec of the pinned bridge path
    #          with exactly the VM name, port and the launch token qdshell
    #          correlates on — no executable, path or environment rides along
    p = host.spawn(VM, APP)
    _session_up(host)
    line = host.log("bridge.log").splitlines()[0].split()
    assert line[:3] == [BRIDGE_PATH, "--vm", VM]
    assert line[3] == "--port" and 20000 <= int(line[4]) < 40000
    assert line[5] == "--token"
    _app_exit(host)
    rc, out, err = finish(p)
    assert rc == 0, err
    assert f"LAUNCH_TOKEN={line[6]}" in out.splitlines()


def test_cancelled_authentication_aborts_before_any_vm_work(host):
    # ensures: a denied/cancelled pkexec prompt starts no guest and leaves
    #          no overlay behind
    host.flag("auth-cancel")
    rc, _out, err = finish(host.spawn(VM, APP))
    assert rc == 9, err
    assert "bridge did not start the waypipe client" in err
    assert "STARTED" not in host.log() and "DEFINED" not in host.log()
    assert not host.overlay(VM).exists()


# -- cold start, reuse, refusal ---------------------------------------------

def test_cold_start_then_app_exit_tears_everything_down(host):
    # ensures: one launch creates overlay+sidecar+domain, and the app's exit
    #          leaves no domain, qemu, overlay, sidecar or waypipe client
    p = host.spawn(VM, APP)
    _session_up(host)
    assert host.defined(VM) and host.qemu_alive(VM)
    sidecar = Path(f"{host.overlay(VM)}.appid").read_text().split()
    st = host.overlay(VM).stat()
    assert sidecar == [APP, str(st.st_dev), str(st.st_ino)]
    xml = (host.state / "domains" / f"{VM}.xml").read_text()
    assert f">{APP}</qdistro:appId>" in xml
    assert str(host.overlay(VM)) in xml
    _app_exit(host)
    rc, _out, err = finish(p)
    assert rc == 0, err
    assert _torn_down(host)
    assert host.clients_alive() == []
    assert "BRIDGE-EXIT eof" in host.log("bridge.log")


def test_simultaneous_cold_starts_share_one_guest(host):
    # ensures: two racing launches of one app define/start the guest once;
    #          the loser adopts it, and only the last session out tears down
    a = host.spawn(VM, APP)
    b = host.spawn(VM, APP)
    _session_up(host, count=2)
    assert host.log().count(f"STARTED {VM}") == 1
    assert host.log().count(f"DEFINED {VM}") == 1
    _app_exit(host)
    ra, _, ea = finish(a)
    rb, _, eb = finish(b)
    assert (ra, rb) == (0, 0), ea + eb
    assert "adopted running domain" in ea + eb
    assert _torn_down(host)


def test_relaunch_adopts_and_first_exit_leaves_guest_running(host):
    # ensures: a second session of the same app attaches to the running
    #          guest, and the first session's exit does not reap it
    a = host.spawn(VM, APP)
    _session_up(host)
    b = host.spawn(VM, APP)
    _session_up(host, count=2)
    assert host.log().count(f"STARTED {VM}") == 1
    _app_exit(host, session=0)
    ra, _, ea = finish(a)
    assert ra == 0, ea
    assert "adopted by another session" in ea
    assert host.defined(VM) and host.qemu_alive(VM)
    assert host.overlay(VM).exists()
    _app_exit(host)
    rb, _, eb = finish(b)
    assert rb == 0, eb
    assert _torn_down(host)


def test_different_app_is_refused_and_owner_keeps_its_guest(host):
    # ensures: a launch whose app does not match the domain's bound appId
    #          is refused without touching the guest, disk or locks
    a = host.spawn(VM, APP)
    _session_up(host)
    rc, _out, err = finish(host.spawn(VM, OTHER))
    assert rc == 3, err
    assert "refusing cross-app adoption" in err
    assert host.qemu_alive(VM) and host.overlay(VM).exists()
    _app_exit(host)
    assert finish(a)[0] == 0
    assert _torn_down(host)


def test_stopped_foreign_domain_is_not_reclaimed(host):
    # ensures: a defined-but-stopped domain bound to another app is neither
    #          redefined, started nor deleted
    a = host.spawn(VM, OTHER, TIER5_KEEP_DOMAIN=1)
    _session_up(host)
    os.killpg(a.pid, signal.SIGTERM)
    finish(a)
    subprocess.run(["virsh", "destroy", VM], env=host.env(), check=True)
    assert host.defined(VM) and not host.qemu_alive(VM)
    rc, _out, err = finish(host.spawn(VM, APP))
    assert rc == 3, err
    assert "refusing to reclaim another app's guest" in err
    assert host.defined(VM) and host.overlay(VM).exists()
    assert host.log().count(f"STARTED {VM}") == 1


def test_overlay_without_binding_sidecar_is_preserved_and_refused(host):
    # ensures: an overlay file not bound to this app by its sidecar is never
    #          booted under our binding nor deleted
    stray = host.overlay(VM)
    stray.write_text(str(host.base))
    rc, _out, err = finish(host.spawn(VM, APP))
    assert rc == 3, err
    assert "refusing to boot it" in err
    assert stray.exists() and "STARTED" not in host.log()


def test_crash_residue_sidecar_is_replaced_on_next_create(host):
    # ensures: a crash between stamping the sidecar and linking the disk
    #          does not wedge the app — our own lone record is replaced
    Path(f"{host.overlay(VM)}.appid").write_text(f"{APP} 1 2\n")
    Path(f"{host.overlay(VM)}.partial.abc123").write_text("junk")
    p = host.spawn(VM, APP)
    _session_up(host)
    assert not Path(f"{host.overlay(VM)}.partial.abc123").exists()
    _app_exit(host)
    assert finish(p)[0] == 0
    assert _torn_down(host)


def test_invalid_explicit_policy_key_fails_before_any_launch(host):
    # ensures: an explicit key outside the charset is a hard error, never a
    #          basename fallback that would let two invalid appIds share a
    #          binding (sol r4)
    for key in ("bad key", "-x", "a" * 129, "tier5/x\nfoo"):
        rc, _out, err = finish(host.spawn(VM, key))
        assert rc == 3 and "invalid policy key" in err, (key, err)
    assert host.log("bridge.log") == "" and "STARTED" not in host.log()


def test_concurrent_sessions_keep_separate_logs(host):
    # ensures: two live sessions of one app never share waypipe logs
    a = host.spawn(VM, APP)
    b = host.spawn(VM, APP)
    _session_up(host, count=2)
    logs = sorted(p.name for p in host.run.glob(f"tier5-{VM}-foot-*-client.log"))
    assert len(logs) == 2, logs
    _app_exit(host)
    assert finish(a)[0] == 0 and finish(b)[0] == 0


# -- the two interim lifecycle defects --------------------------------------

def test_idle_window_contention_is_not_adoption(host):
    # ensures: (interim defect 1) a wrapper that is still DECIDING — holding
    #          the session hold but never establishing a session, e.g. a
    #          relaunch that will refuse — does not make the idle wrapper
    #          walk away from teardown and orphan the guest
    p = host.spawn(VM, APP, TIER5_IDLE_SHUTDOWN_SECS=3)
    _session_up(host)
    hold = host.run / "qdistro-tier5" / f"hold-{VM}"
    decider = subprocess.Popen(["flock", "-s", str(hold), "sleep", "8"])
    _app_exit(host)
    try:
        rc, _out, err = finish(p, timeout=60)
    finally:
        decider.wait()
    assert rc == 0, err
    assert "adopted by a relaunch" not in err
    assert "adopted by another session" not in err
    assert "waiting for another wrapper's decision" in err
    assert _torn_down(host)


def test_long_graceful_shutdown_then_relaunch_cold_starts(host):
    # ensures: (interim defect 2) a relaunch arriving while the previous
    #          session holds the exclusive teardown claim through a graceful
    #          shutdown longer than the decision wait cold-starts afterwards
    #          instead of failing on a fixed session-hold timeout
    host.flag("acpi-delay", "5")
    a = host.spawn(VM, APP, TIER5_SHUTDOWN_GRACE_SECS=20)
    _session_up(host)
    _app_exit(host, session=0)
    host.wait_log("shutdown --mode acpi", timeout=30)
    host.flag("acpi-delay", "0.2")
    t0 = time.monotonic()
    b = host.spawn(VM, APP, TIER5_DECISION_WAIT_SECS=1)
    ra, _, ea = finish(a)
    assert ra == 0, ea
    _session_up(host, count=2)
    assert time.monotonic() - t0 > 2
    assert host.log().count(f"STARTED {VM}") == 2
    _app_exit(host)
    rb, _, eb = finish(b)
    assert rb == 0, eb
    assert "timed out waiting" not in eb
    assert _torn_down(host)


def test_idle_window_hands_off_to_an_established_relaunch(host):
    # ensures: the keep-warm window ends without teardown once a relaunch
    #          has ESTABLISHED a session on the guest
    a = host.spawn(VM, APP, TIER5_IDLE_SHUTDOWN_SECS=30)
    _session_up(host)
    _app_exit(host, session=0)
    time.sleep(2.5)
    b = host.spawn(VM, APP)
    _session_up(host, count=2)
    ra, _, ea = finish(a)
    assert ra == 0, ea
    assert "adopted by a relaunch" in ea
    assert host.qemu_alive(VM) and host.log().count(f"STARTED {VM}") == 1
    _app_exit(host)
    assert finish(b)[0] == 0
    assert _torn_down(host)


# -- crashes and failures -----------------------------------------------------

def test_killed_wrapper_drops_its_client_and_guest_is_adopted(host):
    # ensures: a SIGKILLed wrapper's bridge sees EOF and removes its waypipe
    #          client; the surviving guest is adopted by the next launch and
    #          torn down at its end
    a = host.spawn(VM, APP)
    _session_up(host)
    # only the wrapper: the real bridge is root, out of admin's reach
    os.kill(a.pid, signal.SIGKILL)
    a.wait()
    host.wait_log("BRIDGE-EXIT eof", path="bridge.log", timeout=10)
    assert host.qemu_alive(VM)
    b = host.spawn(VM, APP)
    _session_up(host, count=2)
    assert host.log().count(f"STARTED {VM}") == 1
    _app_exit(host)
    rb, _, eb = finish(b)
    assert rb == 0 and "adopted running domain" in eb, eb
    assert _torn_down(host)
    assert host.clients_alive() == []


def test_guest_crash_ends_session_and_cleans_up(host):
    # ensures: a guest that dies under a session ends it, and the dead
    #          guest's definition and overlay are removed
    p = host.spawn(VM, APP)
    _session_up(host)
    subprocess.run(["virsh", "destroy", VM], env=host.env(), check=True)
    rc, _out, err = finish(p)
    assert rc == 0, err
    assert _torn_down(host)


def test_bridge_exit_ends_session(host):
    # ensures: when the waypipe client chain dies, the session ends and
    #          follows the teardown policy instead of running headless
    p = host.spawn(VM, APP)
    _session_up(host)
    host.flag("bridge-die")
    rc, _out, err = finish(p)
    assert rc == 0, err
    assert "bridge exited" in err
    assert _torn_down(host)


def test_libvirt_query_outage_keeps_the_overlay(host):
    # ensures: when libvirt cannot confirm the domain is gone, the overlay
    #          is retained and the wrapper reports failure
    p = host.spawn(VM, APP)
    _session_up(host)
    host.flag("fail-list")
    _app_exit(host)
    rc, _out, err = finish(p)
    assert rc == 5, err
    assert "retaining overlay" in err
    assert host.overlay(VM).exists()


def test_undefine_retries_with_ephemeral_state_and_metadata_cleanup(host):
    # ensures: a domain whose plain undefine is refused for retained
    #          nvram/snapshot metadata is still fully removed
    host.flag("undefine-needs-metadata")
    p = host.spawn(VM, APP)
    _session_up(host)
    _app_exit(host)
    rc, _out, err = finish(p)
    assert rc == 0, err
    assert (f"undefine {VM} --managed-save --snapshots-metadata "
            "--checkpoints-metadata --nvram --tpm") in host.log()
    assert _torn_down(host)


def test_overlay_is_kept_while_the_definition_remains(host):
    # ensures: the overlay is never unlinked under a still-defined domain
    #          (it could be started against a deleted disk), and the
    #          wrapper reports the incomplete reap
    host.flag("undefine-fails")
    p = host.spawn(VM, APP)
    _session_up(host)
    _app_exit(host)
    rc, _out, err = finish(p)
    assert rc == 5, err
    assert "still defined or state unverifiable" in err
    assert host.defined(VM) and host.overlay(VM).exists()
    assert not host.qemu_alive(VM)


def test_unanswered_guest_agent_fails_after_the_clamped_deadline(host):
    # ensures: a guest whose qemu-guest-agent never answers fails the launch
    #          after the bounded (30..600 s) boot deadline and is torn down
    host.flag("qga-dead")
    t0 = time.monotonic()
    rc, _out, err = finish(host.spawn(VM, APP, TIER5_QGA_TIMEOUT_SECS=1),
                           timeout=120)
    assert rc == 7, err
    assert "never responded after 30s" in err
    assert time.monotonic() - t0 >= 29
    assert _torn_down(host)


def test_query_outage_before_create_refuses(host):
    # ensures: an unverifiable defined-state never reads as "absent"
    host.flag("fail-list")
    rc, _out, err = finish(host.spawn(VM, APP))
    assert rc == 3, err
    assert "STARTED" not in host.log()


# -- orphan reaper ------------------------------------------------------------

def _orphan(host, app_id):
    vm = derived_name(app_id)
    disk = host.overlay(vm)
    disk.write_text(str(host.base))
    st = disk.stat()
    Path(f"{disk}.appid").write_text(f"{app_id} {st.st_dev} {st.st_ino}\n")
    return disk


def test_reaper_removes_retired_orphans_only(host):
    # ensures: a crashed wrapper's overlay is reaped only when its app is
    #          retired from a valid catalogue; a published app's orphan, an
    #          overlay whose sidecar binds another inode, and anything while
    #          the catalogue is unverifiable are kept
    retired = _orphan(host, "tier5/gone")
    owned = _orphan(host, OTHER)
    copied = _orphan(host, "tier5/copied")
    Path(f"{copied}.appid").write_text("tier5/copied 1 2\n")
    # outside the t5-<stub>-<hash> namespace: never ours to reap
    foreign = host.images / "mydisk.qcow2"
    foreign.write_text(str(host.base))
    # no catalogue yet: retirement is unverifiable, nothing is reaped
    _launch_and_finish(host, 0)
    assert owned.exists() and retired.exists() and copied.exists()
    manifest_for(host, [APP, OTHER])
    _launch_and_finish(host, 1)
    assert not retired.exists() and not Path(f"{retired}.appid").exists()
    assert owned.exists() and copied.exists() and foreign.exists()


def _launch_and_finish(host, session):
    p = host.spawn(VM, APP)
    _session_up(host, count=session + 1)
    _app_exit(host, session=session)
    rc, _out, err = finish(p)
    assert rc == 0, err


# -- liveness oracle ------------------------------------------------------------

def _oracle(script: Path, name: str) -> str:
    body = re.search(r"^qemu_pids_for_domain\(\) \{\n.*?^\}\n",
                     script.read_text(), re.DOTALL | re.MULTILINE).group(0)
    return subprocess.run(
        ["bash", "-c", body + 'qemu_pids_for_domain "$1"; echo " rc=$?"',
         "_", name], capture_output=True, text=True, check=True).stdout


@pytest.mark.parametrize("script", [SPAWN, CLEANUP],
                         ids=["spawn", "cleanup"])
def test_liveness_oracle_matches_only_the_named_guest(host, script):
    # ensures: a guest is "live" only when one of our qemu processes
    #          carries `-name guest=<name>,` as a discrete argument pair —
    #          a longer name, the string inside another argument, a regex
    #          look-alike or a non-qemu process never count (sol r15/r18/r19)
    qemu = str(host.bin / "qemu-system-x86_64")
    procs = [subprocess.Popen(argv) for argv in (
        [qemu, "-name", "guest=t5-xy,debug-threads=on"],
        [qemu, "-drive", "file=/tmp/-name guest=t5-x,backup.qcow2"],
        [qemu, "-name", "guest=t5xa,debug-threads=on"],
        ["sleep", "30", "-name", "guest=t5-x,"],
        [qemu, "-name", "guest=t5-live,debug-threads=on"],
    )]
    try:
        time.sleep(0.3)
        assert _oracle(script, "t5-x").strip() == "rc=1"
        assert _oracle(script, "t5.a").strip() == "rc=1"
        assert _oracle(script, "t5-live").strip() \
            == f"{procs[-1].pid} rc=0"
    finally:
        for p in procs:
            p.kill()
            p.wait()
    assert _oracle(script, "t5-live").strip() == "rc=1"


# -- cleanup tool -------------------------------------------------------------

def test_cleanup_tool_refuses_a_live_session(host):
    # ensures: the cleanup tool never destroys a guest a wrapper still holds
    p = host.spawn(VM, APP)
    _session_up(host)
    r = host.cleanup_tool(VM)
    assert r.returncode == 4, r.stderr
    assert host.qemu_alive(VM) and host.overlay(VM).exists()
    _app_exit(host)
    assert finish(p)[0] == 0


def test_cleanup_tool_removes_a_leftover_guest(host):
    # ensures: a guest left by a KEEP_DOMAIN/crashed session is destroyed,
    #          undefined and its overlay removed — only after confirmed death
    a = host.spawn(VM, APP, TIER5_KEEP_DOMAIN=1)
    _session_up(host)
    _app_exit(host)
    assert finish(a)[0] == 0
    assert host.qemu_alive(VM)
    r = host.cleanup_tool(VM)
    assert r.returncode == 0, r.stderr
    assert _torn_down(host)


def test_cleanup_tool_refuses_a_foreign_binding(host):
    # ensures: a t5-named domain whose binding derives a different name is
    #          not destroyed
    a = host.spawn(VM, OTHER, TIER5_KEEP_DOMAIN=1)
    _session_up(host)
    _app_exit(host)
    finish(a)
    r = host.cleanup_tool(VM)
    assert r.returncode == 3, r.stderr
    assert host.qemu_alive(VM) and host.defined(VM)


# -- installed tools ------------------------------------------------------------

_VIRSH_USE = {
    "list": ["--all", "--name"], "dumpxml": [], "define": [], "start": [],
    "domstate": [], "shutdown": ["--mode"], "destroy": [], "dominfo": [],
    "undefine": ["--managed-save", "--snapshots-metadata",
                 "--checkpoints-metadata", "--nvram", "--tpm"],
    "qemu-agent-command": ["--timeout"],
}


def test_scripts_use_only_known_virsh_subcommands():
    # ensures: every virsh subcommand the scripts issue is one the stub host
    #          serves AND one the installed-virsh test below checks
    for src in (SPAWN, CLEANUP):
        code = "\n".join(line for line in src.read_text().splitlines()
                         if not line.lstrip().startswith("#")
                         and "for tool in" not in line)
        used = set(re.findall(r"(?:^|[\s($])virsh\s+([a-z][a-z-]+)", code,
                              re.MULTILINE))
        assert used and used <= set(_VIRSH_USE), used - set(_VIRSH_USE)


@pytest.mark.skipif(shutil.which("virsh") is None,
                    reason="virsh not installed on this host")
def test_virsh_subcommands_and_flags_exist_in_installed_virsh():
    # ensures: every subcommand/flag the lifecycle uses is real virsh(1)
    #          syntax (a nonexistent `virsh dompid` once survived six
    #          source-only review rounds)
    for cmd, flags in _VIRSH_USE.items():
        r = subprocess.run(["virsh", "help", cmd], capture_output=True,
                           text=True, check=False)
        assert r.returncode == 0, cmd
        for f in flags:
            assert f in r.stdout, (cmd, f)


@pytest.mark.skipif(shutil.which("qemu-img") is None,
                    reason="qemu-img not installed on this host")
def test_qemu_img_overlay_round_trip_with_installed_tool(tmp_path):
    # ensures: the create/info invocations the lifecycle issues work with
    #          the installed qemu-img and report the backing file the
    #          scripts compare against
    base = tmp_path / "base.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(base), "1M"],
                   check=True, capture_output=True)
    ov = tmp_path / "ov.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2",
                    "-b", str(base), str(ov)], check=True,
                   capture_output=True)
    info = subprocess.run(["qemu-img", "info", "-U", "--output=json",
                           str(ov)], check=True, capture_output=True,
                          text=True).stdout
    assert f'"backing-filename": "{base}"' in info
