"""tier3s launch path: spawn-tier3s.sh, qdistro-tier3s-scope, qdistro-tier3s-cleanup.

Runs the REAL scripts under their TIER3S_TEST_ROOT hook against a fake world:
PATH fakes for podman, runuser, dbus-send, systemd-run, systemctl, chown and
the binding resolver, a fake probe, and a fake /proc + /sys/fs/cgroup under the
test root. The fakes keep their state in files (the scripts run podman through
`env -i`, so environment knobs would not reach them) and append every call to
one log, so a test can assert what ran and in which order. No podman, runsc or
systemd ever runs.

What this does NOT prove (tier3s/CONTRACT.md §8): scope delegation,
ExecStopPost, process placement and teardown on a real system are VM facts
(A-iii). This proves the scripts' decisions and their order.
"""
import json
import os
import pwd
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
T3S = REPO / "tier3s"
SPAWN = T3S / "spawn-tier3s.sh"
HELPER = T3S / "qdistro-tier3s-scope"
CLEANUP = T3S / "qdistro-tier3s-cleanup"
UID = os.getuid()
ME = pwd.getpwuid(UID).pw_name
TOKEN = "0123456789abcdef0123456789abcdef"
TOKEN2 = "fedcba9876543210fedcba9876543210"
DIGEST = "sha256:" + "a" * 64
ARGV = ["headless-smoke", "--", "qdistro-tier3s-smoke"]

FAKE_PODMAN = r'''#!/bin/bash
F=@F@; T=@T@
echo "podman $*" >> "$F/calls"
while [ $# -gt 0 ]; do
    case "$1" in
        --runtime|--cgroup-manager) shift 2 ;;
        --runtime-flag=*|--cgroup-manager=*) shift ;;
        *) break ;;
    esac
done
sub="$1"; shift
for a in "$@"; do name="$a"; done          # the container is the last argument (except run)
finish() {   # the container's processes end, --rm removes it, its scope goes away
    local c="$F/c/$1" p rel
    [ -d "$c" ] || return 0
    rm -f "$c/exists" "$c/running"
    for p in $(cat "$c/pids" 2>/dev/null); do rm -rf "$T/proc/$p"; done
    rel="$(cat "$c/scope" 2>/dev/null)"
    if [ -n "$rel" ] && [ ! -e "$F/scope_sticky" ]; then
        rm -rf "$T/sys/fs/cgroup$rel"
        echo inactive > "$F/units/${rel##*/}.state"; rm -f "$F/units/${rel##*/}.cgroup"
    fi
}
case "$sub" in
image) exit "$(cat "$F/image_rc" 2>/dev/null || echo 0)" ;;
inspect)
    [ -e "$F/c/$name/running" ] || { echo "Error: no such container $name" >&2; exit 125; }
    cat "$F/c/$name/inspect_line" ;;
container)
    case "$1" in
        exists)
            [ ! -e "$F/query_fail" ] || { echo "Error: database is locked" >&2; exit 125; }
            if [ -e "$F/query_fail_after_stop" ] && grep -q '^podman stop' "$F/calls"; then
                echo "Error: database is locked" >&2; exit 125; fi
            [ -e "$F/c/$name/exists" ] && exit 0; exit 1 ;;
        inspect)
            # a concurrent teardown removes the container under our feet
            if [ -e "$F/inspect_vanish" ]; then finish "$name"; rm -rf "${F:?}/c/$name"
                echo "Error: no such container $name" >&2; exit 125; fi
            [ ! -e "$F/inspect_fail" ] || { echo "Error: inspect failed" >&2; exit 125; }
            cat "$F/c/$name/label" ;;
    esac ;;
stop)
    if [ -e "$F/stop_vanish" ]; then finish "$name"; rm -rf "${F:?}/c/$name"
        echo "Error: no container with name or ID $name found" >&2; exit 125; fi
    [ ! -e "$F/stop_fail" ] || { echo "Error: given PID did not die within timeout" >&2; exit 125; }
    finish "$name" ;;
rm) finish "$name"; rm -rf "${F:?}/c/$name" ;;
ps)
    [ ! -e "$F/ps_fail" ] || { echo "Error: cannot list" >&2; exit 125; }
    fmt=""; prev=""
    for a in "$@"; do [ "$prev" = --format ] && fmt="$a"; prev="$a"; done
    for c in "$F"/c/*/; do
        [ -e "$c/exists" ] || continue
        # render the caller's template like podman 6 does for these fields;
        # `index .Labels` is an error there (.Labels is not a map in ps)
        case "$fmt" in *"index .Labels"*)
            echo 'Error: template: ps:1:13: executing "ps" at <index .Labels "qdistro_tier3s_token">: error calling index: cannot index slice/array with type string' >&2
            exit 125 ;; esac
        line="${fmt//'{{.Label "qdistro_tier3s_token"}}'/$(cat "$c/label")}"
        line="${line//'{{.Label "qdistro_tier3s_unit"}}'/$(cat "$c/unit_label" 2>/dev/null)}"
        line="${line//'{{.Names}}'/$(basename "$c")}"
        case "$line" in *"{{"*) echo "fake podman ps: unsupported template '$fmt'" >&2; exit 125 ;; esac
        echo "$line"
    done ;;
run)
    printf '%s\n' "$@" > "$F/run_argv"
    while [ $# -gt 0 ]; do [ "$1" = --name ] && { name="$2"; break; }; shift; done
    c="$F/c/$name"; mkdir -p "$c"; touch "$c/exists" "$c/running"
    tok="$(sed -n 's/^qdistro_tier3s_token=//p' "$F/run_argv")"
    echo "$tok" > "$c/label"; sed -n 's/^qdistro_tier3s_unit=//p' "$F/run_argv" > "$c/unit_label"
    rel="$(sed -n 's/^0:://p' "$T/proc/$$/cgroup")"; echo "$rel" > "$c/scope"
    srel="$rel"; [ ! -e "$F/sentry_cgroup" ] || srel="$(cat "$F/sentry_cgroup")"
    for spec in "4001 conmon $rel 777001" "4002 gvisor_sentry $srel 777002"; do
        set -- $spec
        mkdir -p "$T/proc/$1"; echo "0::$3" > "$T/proc/$1/cgroup"
        echo "$1 ($2) S 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 $4 0 0" > "$T/proc/$1/stat"
        echo "$1" >> "$c/pids"
    done
    printf '4001\n4002\n' >> "$T/sys/fs/cgroup$rel/cgroup.procs"
    echo "running 4002 4001 cid$tok" > "$c/inspect_line"
    for _ in $(seq 1 600); do
        [ -e "$c/running" ] || exit 143
        [ ! -e "$F/release" ] || break
        [ -e "$F/run_block" ] || break
        sleep 0.02
    done
    finish "$name"; rm -rf "${F:?}/c/$name"
    exit "$(cat "$F/run_rc" 2>/dev/null || echo 0)" ;;
esac
exit 0
'''

FAKE_SYSTEMD_RUN = r'''#!/bin/bash
F=@F@; T=@T@
echo "systemd-run $*" >> "$F/calls"
printf '%s\n' "$@" > "$F/scope_argv"
unit=""
for a in "$@"; do case "$a" in --unit=*) unit="${a#--unit=}" ;; esac; done
while [ $# -gt 0 ] && [ "$1" != -- ]; do shift; done; shift
rel="/system.slice/$unit"; d="$T/sys/fs/cgroup$rel"
mkdir -p "$d"; echo $$ > "$d/cgroup.procs"
for f in cgroup.subtree_control cgroup.threads; do : > "$d/$f"; done
echo max > "$d/memory.max"; echo max > "$d/pids.max"
mkdir -p "$T/proc/$$"; echo "0::$rel" > "$T/proc/$$/cgroup"
echo "$rel" > "$F/units/$unit.cgroup"; echo active > "$F/units/$unit.state"
sed -n 's/^BindsTo=//p' "$F/scope_argv" > "$F/units/$unit.bindsto"
exec "$@"
'''

FAKE_SYSTEMCTL = r'''#!/bin/bash
F=@F@; T=@T@
echo "systemctl $*" >> "$F/calls"
case "$1" in
is-active) s="$(cat "$F/units/$2.state" 2>/dev/null || echo inactive)"; echo "$s"; [ "$s" = active ] ;;
show) for a; do u="$a"; done
    case " $* " in *" BindsTo "*) cat "$F/units/$u.bindsto" 2>/dev/null ;; *) cat "$F/units/$u.cgroup" 2>/dev/null ;; esac
    exit 0 ;;
stop)
    [ ! -e "$F/scope_stop_fail" ] || exit 1
    rel="$(cat "$F/units/$2.cgroup" 2>/dev/null)"
    if [ -n "$rel" ]; then
        for p in $(cat $(find "$T/sys/fs/cgroup$rel" -name cgroup.procs) 2>/dev/null); do rm -rf "$T/proc/$p"; done
        rm -rf "$T/sys/fs/cgroup$rel"
    fi
    echo inactive > "$F/units/$2.state"; rm -f "$F/units/$2.cgroup" ;;
esac
'''

FAKE_RUNUSER = r'''#!/bin/bash
F=@F@
echo "runuser $*" >> "$F/calls"
[ "$1" = -u ] && [ "$3" = -- ] || { echo "fake runuser: bad args $*" >&2; exit 99; }
shift 3; exec "$@"
'''

FAKE_DBUS = r'''#!/bin/bash
F=@F@
act=""; for a; do case "$a" in string:*) act="${a#string:}" ;; esac; done
echo "dbus-send $act" >> "$F/calls"
case "$(cat "$F/dbus_mode" 2>/dev/null)" in
  allow) echo '   string "allow"' ;;
  deny) echo '   string "deny"' ;;
  unknown) echo '   string "unknown"' ;;
  empty) ;;
  disallow) echo '   string "disallow"' ;;
  error) echo "Error org.freedesktop.DBus.Error.ServiceUnknown" >&2; exit 1 ;;
  *) echo "fake dbus-send: no mode" >&2; exit 2 ;;
esac
'''

FAKE_RESOLVER = r'''#!/bin/bash
F=@F@; T=@T@
echo "resolver $*" >> "$F/calls"
rec=0; for a; do [ "$a" = --record ] && rec=1; done
case "$(cat "$F/resolver_mode" 2>/dev/null || echo untemplated)" in
  untemplated) exit 3 ;;
  digest) echo "GENERATION=@DIGEST@"; echo "STATE_PATH=$T/state"; echo "TEMPLATE=t" ;;
  drift) if [ $rec = 1 ]; then echo "GENERATION=sha256:@BBB@"; else echo "GENERATION=@DIGEST@"; fi
         echo "STATE_PATH=$T/state" ;;
  fail) exit 1 ;;
esac
'''

FAKE_CHOWN = r'''#!/bin/bash
echo "chown $*" >> @F@/calls
exec /usr/bin/chown "$@"
'''

FAKE_PROBE = r'''#!/bin/bash
echo "probe $*" >> @F@/calls
rc="$(cat @F@/probe_rc 2>/dev/null || echo 0)"
if [ "$rc" = 0 ]; then echo "RESULT PASS: tier 3s prerequisites present"
else echo "RESULT FAIL: first missing prerequisite: state_root (/run/qdistro-tier3s-runsc/1000 missing)"; fi
exit "$rc"
'''


def write_exec(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


class World:
    """A fake installed system under tmp/root plus PATH fakes under tmp/bin."""

    def __init__(self, tmp, silo="smoke", token=TOKEN):
        self.tmp, self.silo, self.token = tmp, silo, token
        self.T, self.F, self.bin = tmp / "root", tmp / "fake", tmp / "bin"
        for d in (self.F / "units", self.F / "c", self.bin, self.T / "etc/qdistro",
                  self.T / "sys/fs/cgroup/system.slice", self.T / "proc/self", self.T / "state"):
            d.mkdir(parents=True, exist_ok=True)
        (self.F / "calls").write_text("")
        (self.T / "etc/qdistro/profile").write_text("QDISTRO_PROFILE=dev\n")
        lib = self.T / "usr/lib/qdistro/tier3s"
        (lib / "seccomp").mkdir(parents=True)
        shutil.copyfile(T3S / "seccomp/headless-smoke.json", lib / "seccomp/headless-smoke.json")
        libexec = self.T / "usr/libexec/qdistro"
        libexec.mkdir(parents=True)
        (libexec / "qdistro-tier3s-scope").symlink_to(HELPER)       # the real helper
        (libexec / "qdistro-tier3s-cleanup").symlink_to(CLEANUP)    # the real cleanup
        sub = {"@F@": str(self.F), "@T@": str(self.T), "@DIGEST@": DIGEST, "@BBB@": "b" * 64}

        def fill(text):
            for k, v in sub.items():
                text = text.replace(k, v)
            return text
        write_exec(lib / "probe.sh", fill(FAKE_PROBE))
        for name, text in (("podman", FAKE_PODMAN), ("systemd-run", FAKE_SYSTEMD_RUN),
                           ("systemctl", FAKE_SYSTEMCTL), ("runuser", FAKE_RUNUSER),
                           ("dbus-send", FAKE_DBUS), ("qdistro-resolve-binding", FAKE_RESOLVER),
                           ("chown", FAKE_CHOWN)):
            write_exec(self.bin / name, fill(text))
        run = self.T / "run"
        for d, mode in (("qdistro-tier3s-ctl", 0o700), ("qdistro-tier3s", 0o755),
                        ("qdistro-tier3s-runsc", 0o755), (f"qdistro-tier3s-runsc/{UID}", 0o700)):
            (run / d).mkdir(parents=True, exist_ok=True)
            (run / d).chmod(mode)
        self.ctl = run / "qdistro-tier3s-ctl"
        self.launch_parent = run / "qdistro-tier3s"
        self.state_root = run / f"qdistro-tier3s-runsc/{UID}"
        self.unit = (f"qdistro-tier3s-silo@{silo}.service" if silo
                     else f"qdistro-tier3s-app@{token}.service")
        (self.T / "proc/self/cgroup").write_text(f"0::/system.slice/{self.unit}\n")
        self.set_unit(self.unit, "active")
        self.set("dbus_mode", "allow")

    # -- fake controls --
    def set(self, name, value=""):
        (self.F / name).write_text(value)

    def set_unit(self, unit, state):
        (self.F / "units" / f"{unit}.state").write_text(state + "\n")

    def calls(self):
        return [l for l in (self.F / "calls").read_text().splitlines() if l]

    def first(self, prefix):
        for i, l in enumerate(self.calls()):
            if l.startswith(prefix):
                return i
        return None

    def env(self, **kw):
        e = {k: v for k, v in os.environ.items()
             if not k.startswith(("TIER3S_", "QDISTRO_"))}
        e.update(PATH=f"{self.bin}:{os.environ['PATH']}", TIER3S_TEST_ROOT=str(self.T),
                 QDISTRO_PROFILE="dev", TIER3S_ROOT_LAUNCHER="1", TIER3S_ADMIN_UID=str(UID),
                 TIER3S_LAUNCH_UNIT=self.unit, TIER3S_LAUNCH_TOKEN=self.token)
        if self.silo:
            e["TIER3S_SILO"] = self.silo
        for k, v in kw.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        return e

    def spawn(self, argv=ARGV, **kw):
        return subprocess.run(["bash", str(SPAWN), *argv], env=self.env(**kw),
                              capture_output=True, text=True, timeout=60)

    def start(self, argv=ARGV, **kw):
        return subprocess.Popen(["bash", str(SPAWN), *argv], env=self.env(**kw),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def cleanup(self, *args):
        return subprocess.run(["bash", str(CLEANUP), *args], env=self.env(),
                              capture_output=True, text=True, timeout=60)

    def plan(self, **kw):
        r = self.spawn(TIER3S_PRINT_PLAN="1", **kw)
        assert r.returncode == 0, r.stderr
        lines = r.stdout.splitlines()
        kv = dict(l.split("=", 1) for l in lines if not l.startswith(("PODMAN_ARG=", "SCOPE_ARG=")))
        kv["podman"] = [l.split("=", 1)[1] for l in lines if l.startswith("PODMAN_ARG=")]
        kv["scope"] = [l.split("=", 1)[1] for l in lines if l.startswith("SCOPE_ARG=")]
        return kv

    def state(self, token=None):
        f = self.ctl / (token or self.token) / "state"
        return dict(l.split("=", 1) for l in f.read_text().splitlines())

    # -- a running launch built by hand (for cleanup tests) --
    def make_launch(self, token, unit, container, pids=(4001, 4002), alive=True, record=None):
        rel = f"/system.slice/qdistro-tier3s-{token}.scope"
        d = Path(f"{self.T}/sys/fs/cgroup{rel}")
        st = {"schema": "1", "token": token, "container": container, "unit": unit,
              "scope_unit": f"qdistro-tier3s-{token}.scope", "admin_uid": str(UID),
              "runsc_root": f"/run/qdistro-tier3s-runsc/{UID}",
              "per_launch_dir": f"/run/qdistro-tier3s/{token}", "phase": "running",
              "container_id": "cid" + token, "scope_cgroup": rel,
              "conmon_pid": str(pids[0]), "conmon_starttime": "777001",
              "sentry_pid": str(pids[1]), "sentry_starttime": "777002"}
        st.update(record or {})
        cd = self.ctl / token
        cd.mkdir(mode=0o700)
        cd.chmod(0o700)
        (cd / "state").write_text("".join(f"{k}={v}\n" for k, v in sorted(st.items())))
        (cd / "state").chmod(0o600)
        (self.launch_parent / token).mkdir(mode=0o700)
        self.set_unit(unit, "active")
        if not alive:
            return
        d.mkdir(parents=True)
        (d / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids))
        (self.F / "units" / f"qdistro-tier3s-{token}.scope.cgroup").write_text(rel + "\n")
        self.set_unit(f"qdistro-tier3s-{token}.scope", "active")
        for p, comm, start in ((pids[0], "conmon", 777001), (pids[1], "gvisor_sentry", 777002)):
            (self.T / f"proc/{p}").mkdir(parents=True, exist_ok=True)
            (self.T / f"proc/{p}/cgroup").write_text(f"0::{rel}\n")
            (self.T / f"proc/{p}/stat").write_text(f"{p} ({comm}) S " + "0 " * 18 + f"{start} 0 0\n")
        c = self.F / "c" / container
        c.mkdir(parents=True)
        for f in ("exists", "running"):
            (c / f).write_text("")
        (c / "label").write_text(token + "\n")
        (c / "unit_label").write_text(unit + "\n")
        (c / "scope").write_text(rel + "\n")
        (c / "pids").write_text("".join(f"{p}\n" for p in pids))

    def launch_gone(self, token):
        return (not (self.ctl / token).exists() and not (self.launch_parent / token).exists())


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def wait_for(pred, what, proc=None, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        if proc is not None and proc.poll() is not None:
            out, err = proc.communicate()
            raise AssertionError(f"exited rc={proc.returncode} waiting for {what}\n{out}\n{err}")
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# --- the plan: podman command, scope, record locations ----------------------

def test_plan_podman_command_shape(w):
    p = w.plan()
    pa = p["podman"]
    assert pa[:2] == ["--runtime", "/usr/libexec/qdistro/tier3s-runsc"]
    assert "--runtime-flag=network=none" in pa and "--network=none" in pa
    assert "--cgroup-manager=cgroupfs" in pa
    run = pa.index("run")
    assert pa.index("--cgroup-manager=cgroupfs") < run and pa.index("--runtime-flag=network=none") < run
    for pair in (["--security-opt", "label=disable"], ["--security-opt", "no-new-privileges"],
                 ["--userns=keep-id", "--user"], ["--name", "qdistro-tier3s-smoke"],
                 ["--label", f"qdistro_tier3s_token={TOKEN}"],
                 ["--label", f"qdistro_tier3s_unit={w.unit}"]):
        assert any(pa[i:i + 2] == pair for i in range(run, len(pa))), pair
    assert "--cap-drop=ALL" in pa and "--read-only" in pa and "--rm" in pa
    assert "seccomp=/usr/lib/qdistro/tier3s/seccomp/headless-smoke.json" in pa
    assert p["SECCOMP"] == f"{w.T}/usr/lib/qdistro/tier3s/seccomp/headless-smoke.json"
    # tmpfs ownership through podman's U option, never a literal uid= (podman 6.0.2 rejects it)
    assert "/run/user/1000:rw,U,mode=0700" in pa and "/home/admin/.cache:rw,U,mode=0700" in pa
    assert not any("uid=" in a for a in pa)
    # D-A1: no per-call root; D-A3b: no cgroup-parent containment
    assert not any("root=" in a or a == "--root" for a in pa)
    assert not any(a.startswith("--cgroup-parent") for a in pa)
    assert pa[-2:] == ["localhost/qdistro/tier3s-headless-smoke:latest", "qdistro-tier3s-smoke"]
    assert p["NETWORK"] == "none" and p["STATE"] == "none"
    assert p["SPAWN_ACTION"] == "qdistro.tier3s.spawn:headless-smoke/qdistro-tier3s-smoke"


def test_plan_scope_is_delegated_and_bound_to_the_launch_unit(w):
    s = w.plan()["scope"]
    assert s[:3] == ["--scope", f"--unit=qdistro-tier3s-{TOKEN}.scope", "--collect"]
    props = [s[i + 1] for i, a in enumerate(s) if a == "-p"]
    assert "Delegate=yes" in props
    assert f"BindsTo={w.unit}" in props and f"Before={w.unit}" in props
    assert any(p.startswith("TasksMax=") for p in props) and any(p.startswith("MemoryMax=") for p in props)
    i = s.index("--")
    assert s[i + 1:] == [f"{w.T}/usr/libexec/qdistro/qdistro-tier3s-scope", "enter", TOKEN, str(UID), "--", "podman"]


def test_plan_control_record_is_outside_the_per_launch_dir(w):
    p = w.plan()
    assert p["CTL_DIR"] == f"{w.T}/run/qdistro-tier3s-ctl/{TOKEN}"
    assert p["LAUNCH_DIR"] == f"{w.T}/run/qdistro-tier3s/{TOKEN}"
    assert not p["CTL_DIR"].startswith(p["LAUNCH_DIR"])
    assert p["RUNSC_ROOT"] == f"/run/qdistro-tier3s-runsc/{UID}"


def test_plan_has_no_side_effect(w):
    w.plan()
    calls = w.calls()
    assert not [c for c in calls if c.startswith(("dbus-send", "systemd-run", "podman"))], calls
    assert not any("--record" in c for c in calls)
    assert list(w.ctl.iterdir()) == [] and list(w.launch_parent.iterdir()) == []


def test_templated_silo_mounts_state_and_uses_the_digest(w):
    w.set("resolver_mode", "digest")
    p = w.plan()
    pa = p["podman"]
    i = pa.index("-v")
    assert pa[i + 1] == f"{w.T}/state:/home/admin:rw"
    assert pa[-2] == DIGEST and p["IMAGE"] == DIGEST
    # read-only resolution before the plan: no --record
    assert [c for c in w.calls() if c.startswith("resolver")] == ["resolver smoke --launch-env"]


def test_podapp_launch_is_refused_in_phase_a(tmp_path):
    """CONTRACT §1: A-ii ships silos only, so the pod-app path is refused
    explicitly, before the plan, the gate or any side effect."""
    w = World(tmp_path, silo=None)
    for extra in ({}, {"TIER3S_PRINT_PLAN": "1"}):
        r = w.spawn(**extra)
        assert r.returncode == 2
        assert "pod apps (qdistro-tier3s-app@<token>.service) are not shipped in Phase A" in r.stderr
    assert w.first("dbus-send") is None and w.first("podman") is None
    assert not any(w.ctl.iterdir())


def test_binding_defaults_to_the_silo_and_is_resolved_by_name(w):
    p = w.plan()
    assert p["BINDING"] == "smoke"
    assert "resolver smoke --launch-env" in w.calls()


def test_template_binding_is_resolved_instead_of_the_silo_name(w):
    """The silo row's template_silo reaches the spawn as TIER3S_BINDING: the
    binding resolved is that one, the container and unit stay the silo's."""
    w.set("resolver_mode", "digest")
    p = w.plan(TIER3S_BINDING="browser1")
    assert p["BINDING"] == "browser1"
    assert p["CONTAINER"] == "qdistro-tier3s-smoke"
    assert [c for c in w.calls() if c.startswith("resolver")] == ["resolver browser1 --launch-env"]


@pytest.mark.parametrize("bad", ["../x", "Bad", "a b", "x" * 40])
def test_bad_binding_name_refused(w, bad):
    r = w.spawn(TIER3S_BINDING=bad)
    assert r.returncode == 2 and "invalid binding name" in r.stderr
    assert w.first("resolver") is None and w.first("podman") is None


def test_debug_log_dir_adds_runtime_debug_flags(w):
    d = w.tmp / "dbg"
    d.mkdir()
    pa = w.plan(TIER3S_DEBUG_LOG_DIR=str(d))["podman"]
    assert "--runtime-flag=debug" in pa and f"--runtime-flag=debug-log={d}/" in pa


# --- refusals before anything runs ------------------------------------------

def assert_refused_early(w, r, needle):
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert needle in r.stderr, r.stderr
    calls = w.calls()
    assert not [c for c in calls if c.startswith(("dbus-send", "systemd-run"))], calls
    assert not any(c.startswith("podman") and " run " in f" {c} " for c in calls)
    assert not any("--record" in c for c in calls)
    assert list(w.ctl.iterdir()) == [] and list(w.launch_parent.iterdir()) == []


@pytest.mark.parametrize("profile", ["daily-driver", "release", "prod", ""])
def test_hardened_profile_refused(w, profile):
    r = w.spawn(QDISTRO_PROFILE=profile or None) if profile else None
    if not profile:
        (w.T / "etc/qdistro/profile").write_text("QDISTRO_PROFILE=daily-driver\n")
        r = w.spawn(QDISTRO_PROFILE=None)
    assert_refused_early(w, r, "tier 3s is dev-profile only in this PoC")
    assert w.first("probe") is None


@pytest.mark.parametrize("knob", ["TIER3S_SECCOMP_PROFILE", "TIER3S_ALLOW_PRIVESC",
                                  "TIER3S_KEEP_CAPS", "TIER3S_RUNTIME", "TIER3S_CGROUP_PARENT"])
def test_privesc_knobs_refused_from_env(w, knob):
    assert_refused_early(w, w.spawn(**{knob: "/tmp/x"}), f"{knob} is not accepted")


@pytest.mark.parametrize("net", ["pasta", "host", "slirp4netns"])
def test_only_network_none(w, net):
    assert_refused_early(w, w.spawn(TIER3S_NETWORK=net), "network=none only")


def test_direct_launch_without_root_launcher_refused(w):
    assert_refused_early(w, w.spawn(TIER3S_ROOT_LAUNCHER=None), "TIER3S_ROOT_LAUNCHER=1 is required")


def test_launch_unit_must_be_our_own_cgroup(w):
    (w.T / "proc/self/cgroup").write_text("0::/user.slice/user-1000.slice/session-2.scope\n")
    assert_refused_early(w, w.spawn(), "not running in qdistro-tier3s-silo@smoke.service")


def test_launch_unit_must_match_the_silo(w):
    (w.T / "proc/self/cgroup").write_text("0::/system.slice/qdistro-tier3s-silo@other.service\n")
    r = w.spawn(TIER3S_LAUNCH_UNIT="qdistro-tier3s-silo@other.service")
    assert_refused_early(w, r, "does not match this launch")


def test_bad_token_refused(w):
    assert_refused_early(w, w.spawn(TIER3S_LAUNCH_TOKEN="../../etc"), "32 lowercase hex")


def test_probe_failure_refuses_with_its_result(w):
    w.set("probe_rc", "1")
    r = w.spawn()
    assert_refused_early(w, r, "probe failed (rc=1): RESULT FAIL: first missing prerequisite: state_root")


def test_missing_seccomp_profile_refused(w):
    (w.T / "usr/lib/qdistro/tier3s/seccomp/headless-smoke.json").unlink()
    assert_refused_early(w, w.spawn(), "no podman-default fallback")


def test_failed_binding_resolution_refused(w):
    w.set("resolver_mode", "fail")
    assert_refused_early(w, w.spawn(), "binding resolution failed")


# --- the broker gate ---------------------------------------------------------

@pytest.mark.parametrize("mode,needle", [
    ("deny", "decision=deny"), ("unknown", "decision=unknown"), ("empty", "decision=unknown"),
    ("disallow", "unsupported verdict"), ("error", "broker authorization failed")])
def test_every_non_allow_reply_refuses_before_activation_and_podman(w, mode, needle):
    w.set("dbus_mode", mode)
    w.set("resolver_mode", "digest")
    r = w.spawn()
    assert r.returncode == 2, r.stderr
    assert needle in r.stderr
    calls = w.calls()
    assert calls[w.first("dbus-send")] == "dbus-send qdistro.tier3s.spawn:headless-smoke/qdistro-tier3s-smoke"
    assert not any("--record" in c for c in calls), calls          # no activation record
    assert not any(c.startswith(("systemd-run", "podman")) for c in calls), calls   # no podman run
    assert list(w.ctl.iterdir()) == [] and list(w.launch_parent.iterdir()) == []


def test_gate_order_probe_resolve_gate_record_then_podman(w):
    w.set("resolver_mode", "digest")
    r = w.spawn()
    assert r.returncode == 0, r.stderr
    calls = w.calls()
    probe = w.first("probe")
    resolve_ro = calls.index("resolver smoke --launch-env")
    gate = w.first("dbus-send")
    record = calls.index("resolver smoke --record --launch-env")
    image = next(i for i, c in enumerate(calls) if c.startswith("podman image exists"))
    scope = w.first("systemd-run")
    run = next(i for i, c in enumerate(calls) if c.startswith("podman") and " run --rm " in c)
    assert probe < resolve_ro < gate < record < image < scope < run, calls


def test_binding_drift_between_resolution_and_activation_refuses(w):
    w.set("resolver_mode", "drift")
    r = w.spawn()
    assert r.returncode == 2
    assert "changed between resolution and activation" in r.stderr
    assert not any(c.startswith(("systemd-run", "podman")) for c in w.calls())


# --- a launch end to end against the fakes -----------------------------------

def test_launch_records_then_cleans_up_on_normal_exit(w):
    w.set("run_block")
    p = w.start()
    try:
        wait_for(lambda: (w.ctl / TOKEN / "state").exists() and w.state().get("phase") == "running",
                 "phase=running", p)
        st = w.state()
        rel = f"/system.slice/qdistro-tier3s-{TOKEN}.scope"
        assert st["container"] == "qdistro-tier3s-smoke" and st["unit"] == w.unit
        assert st["scope_unit"] == f"qdistro-tier3s-{TOKEN}.scope" and st["scope_cgroup"] == rel
        assert st["runsc_root"] == f"/run/qdistro-tier3s-runsc/{UID}" and st["admin_uid"] == str(UID)
        assert (st["sentry_pid"], st["sentry_starttime"]) == ("4002", "777002")
        assert (st["conmon_pid"], st["conmon_starttime"]) == ("4001", "777001")
        assert st["per_launch_dir"] == f"/run/qdistro-tier3s/{TOKEN}"
        assert stat.S_IMODE((w.ctl / TOKEN).stat().st_mode) == 0o700
        assert stat.S_IMODE((w.ctl / TOKEN / "state").stat().st_mode) == 0o600
        ld = w.launch_parent / TOKEN
        assert stat.S_IMODE(ld.stat().st_mode) == 0o700
        assert list(ld.iterdir()) == []                 # the exported dir holds no control state
        # the scope helper ran podman as admin with a fixed environment
        argv = (w.F / "scope_argv").read_text().splitlines()
        assert argv[argv.index("--") + 1:argv.index("--") + 3] == [
            f"{w.T}/usr/libexec/qdistro/qdistro-tier3s-scope", "enter"]
        w.set("release")
        out, err = p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0, err
    assert f"LAUNCH_TOKEN={TOKEN}" in out
    assert w.launch_gone(TOKEN), err


def test_podman_failure_propagates_and_cleans(w):
    w.set("run_rc", "125")
    r = w.spawn()
    assert r.returncode == 125, r.stderr
    assert w.launch_gone(TOKEN)


def test_sentry_outside_the_scope_tears_down(w):
    w.set("run_block")
    w.set("sentry_cgroup", "/user.slice/user-1000.slice/user@1000.service/escaped.scope")
    r = w.spawn()
    assert r.returncode == 2, r.stderr
    assert "OUTSIDE the owning scope" in r.stderr
    assert any(c.startswith("podman stop") for c in w.calls())
    assert w.launch_gone(TOKEN)


def test_missing_image_refuses_and_cleans(w):
    w.set("image_rc", "1")
    r = w.spawn()
    assert r.returncode == 2 and "not in admin's store" in r.stderr
    assert w.first("systemd-run") is None
    assert w.launch_gone(TOKEN)


def test_missing_state_root_refuses_before_any_record(w):
    w.state_root.rmdir()
    r = w.spawn()
    assert r.returncode == 2 and "runsc state root" in r.stderr
    assert w.first("systemd-run") is None and list(w.ctl.iterdir()) == []


def test_spawn_reaps_a_stale_launch_and_leaves_a_live_one(w):
    w.make_launch(TOKEN2, "qdistro-tier3s-silo@gone.service", "qdistro-tier3s-gone", pids=(5001, 5002))
    w.set_unit("qdistro-tier3s-silo@gone.service", "failed")
    live = "33333333333333333333333333333333"
    w.make_launch(live, "qdistro-tier3s-silo@live.service", "qdistro-tier3s-live", pids=(6001, 6002))
    r = w.spawn()
    assert r.returncode == 0, r.stderr
    assert w.launch_gone(TOKEN2)
    assert (w.ctl / live / "state").exists() and (w.launch_parent / live).exists()
    assert not any(c.startswith("podman stop") and "qdistro-tier3s-live" in c for c in w.calls())


# --- the scope helper ----------------------------------------------------------

def helper_world(w, token=TOKEN, procs_extra="", child=False):
    """Run the real helper the way systemd-run --scope would: our fake
    systemd-run creates the scope cgroup with the helper's pid, then execs it."""
    rel = f"/system.slice/qdistro-tier3s-{token}.scope"
    if procs_extra or child:
        d = Path(str(w.T / "sys/fs/cgroup") + rel)
        d.mkdir(parents=True)
        if child:
            (d / "sub").mkdir()
    env = w.env()
    script = (f'{w.bin}/systemd-run --scope --unit=qdistro-tier3s-{token}.scope -- '
              f'"$@"')
    if procs_extra:
        script = (f'{w.bin}/systemd-run --scope --unit=qdistro-tier3s-{token}.scope -- '
                  f'bash -c \'echo {procs_extra} >> "{w.T}/sys/fs/cgroup{rel}/cgroup.procs"; exec "$@"\' x "$@"')
    return env, script


def run_helper(w, args, token=TOKEN, **kw):
    env, script = helper_world(w, token, **kw)
    return subprocess.run(["bash", "-c", script, "x", str(HELPER), *args], env=env,
                          capture_output=True, text=True, timeout=30)


def test_helper_delegates_exactly_four_paths_and_runs_podman_as_admin(w):
    r = run_helper(w, ["enter", TOKEN, str(UID), "--", "podman", "version"])
    assert r.returncode == 0, r.stderr
    d = f"{w.T}/sys/fs/cgroup/system.slice/qdistro-tier3s-{TOKEN}.scope"
    chowns = [c for c in w.calls() if c.startswith("chown")]
    assert chowns == [f"chown {UID} -- {d} {d}/cgroup.procs {d}/cgroup.subtree_control {d}/cgroup.threads"]
    assert not any("memory.max" in c or "pids.max" in c for c in chowns)
    ru = [c for c in w.calls() if c.startswith("runuser")]
    assert len(ru) == 1 and ru[0].startswith(f"runuser -u {ME} -- /usr/bin/env -i PATH=/usr/bin:/bin ")
    assert ru[0].endswith(f"{w.bin}/podman version")
    assert w.calls()[-1] == "podman version"


@pytest.mark.parametrize("args,needle", [
    (["enter", TOKEN, str(UID), "--", "sh", "-c", "id"], "usage:"),
    (["run", TOKEN, str(UID), "--", "podman", "ps"], "usage:"),
    (["enter", "XYZ", str(UID), "--", "podman", "ps"], "32 lowercase hex"),
    (["enter", TOKEN, "0", "--", "podman", "ps"], "non-root numeric uid"),
    (["enter", TOKEN2, str(UID), "--", "podman", "ps"], f"not in qdistro-tier3s-{TOKEN2}.scope"),
], ids=["not-podman", "bad-mode", "bad-token", "root-uid", "foreign-scope"])
def test_helper_refuses_bad_invocations(w, args, needle):
    r = run_helper(w, args)
    assert r.returncode == 2, r.stderr
    assert needle in r.stderr, r.stderr
    assert not any(c.startswith(("chown", "runuser")) for c in w.calls())


def test_helper_refuses_a_scope_that_already_holds_processes(w):
    r = run_helper(w, ["enter", TOKEN, str(UID), "--", "podman", "ps"], procs_extra="99999")
    assert r.returncode == 2 and "already holds processes" in r.stderr, r.stderr
    assert not any(c.startswith(("chown", "runuser")) for c in w.calls())


def test_helper_refuses_a_scope_with_child_cgroups(w):
    r = run_helper(w, ["enter", TOKEN, str(UID), "--", "podman", "ps"], child=True)
    assert r.returncode == 2 and "child cgroups" in r.stderr, r.stderr


# --- cleanup: the final teardown path (D-A1, D-A8) ----------------------------

def test_cleanup_tears_down_a_running_launch(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    r = w.cleanup(TOKEN)
    assert r.returncode == 0, r.stderr
    assert w.launch_gone(TOKEN)
    calls = w.calls()
    assert any(c == f"podman stop -t 10 qdistro-tier3s-smoke" for c in calls), calls
    # podman runs as the recorded admin, never as root
    assert all(calls[i - 1].startswith("runuser -u") for i, c in enumerate(calls) if c.startswith("podman"))


def test_cleanup_with_missing_state_root_preserves_record_and_scope(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.state_root.rename(w.tmp / "aside")
    r = w.cleanup(TOKEN)
    assert r.returncode == 3, r.stderr
    assert "refusing to query or stop" in r.stderr
    assert (w.ctl / TOKEN / "state").exists() and (w.launch_parent / TOKEN).exists()
    assert not any(c.startswith(("podman", "systemctl stop")) for c in w.calls())
    assert (Path(str(w.T / "sys/fs/cgroup") + f"/system.slice/qdistro-tier3s-{TOKEN}.scope")).exists()
    # recovery with the right root: complete teardown
    (w.tmp / "aside").rename(w.state_root)
    r = w.cleanup(TOKEN)
    assert r.returncode == 0, r.stderr
    assert w.launch_gone(TOKEN)


@pytest.mark.parametrize("damage", ["mode", "symlink", "owner-record"])
def test_cleanup_with_replaced_state_root_preserves_record(w, damage):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    if damage == "mode":
        w.state_root.chmod(0o755)
    elif damage == "symlink":
        w.state_root.rename(w.tmp / "real")
        w.state_root.symlink_to(w.tmp / "real")
    r = w.cleanup(TOKEN) if damage != "owner-record" else None
    if damage == "owner-record":
        # a record whose runsc_root is not the wrapper's root for its admin uid
        w2 = w.ctl / TOKEN / "state"
        w2.write_text(w2.read_text().replace(f"runsc_root=/run/qdistro-tier3s-runsc/{UID}",
                                             "runsc_root=/run/user/1000/runsc"))
        r = w.cleanup(TOKEN)
        assert "is not the wrapper's root" in r.stderr
    assert r.returncode != 0
    assert (w.ctl / TOKEN / "state").exists()
    assert not any(c.startswith("podman") for c in w.calls())


def test_cleanup_failed_podman_query_is_not_no_container(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("query_fail")
    r = w.cleanup(TOKEN)
    assert r.returncode == 4, r.stderr
    assert "NOT treating it as absent" in r.stderr
    assert "torn down" not in r.stderr
    assert (w.ctl / TOKEN / "state").exists() and (w.launch_parent / TOKEN).exists()
    assert not any(c.startswith("systemctl stop") for c in w.calls())


def test_cleanup_failed_stop_preserves_record_and_scope(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("stop_fail")
    r = w.cleanup(TOKEN)
    assert r.returncode == 5, r.stderr
    assert (w.ctl / TOKEN / "state").exists()
    assert not any(c.startswith("systemctl stop") for c in w.calls())
    (w.F / "stop_fail").unlink()
    assert w.cleanup(TOKEN).returncode == 0 and w.launch_gone(TOKEN)


@pytest.mark.parametrize("flag", ["inspect_vanish", "stop_vanish"])
def test_cleanup_container_removed_concurrently_is_torn_down(w, flag):
    # launcher SIGKILL: systemd stops the scope while ExecStopPost runs, and
    # podman run --rm removes the container between two of our podman calls
    # (A-iii qci s122). A definitive "absent" on re-query continues the teardown.
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set(flag)
    r = w.cleanup(TOKEN)
    assert r.returncode == 0, r.stderr
    assert "vanished during" in r.stderr and "torn down" in r.stderr
    assert w.launch_gone(TOKEN)


def test_cleanup_failed_inspect_of_a_present_container_preserves(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("inspect_fail")
    r = w.cleanup(TOKEN)
    assert r.returncode == 4 and "podman inspect of qdistro-tier3s-smoke failed" in r.stderr
    assert (w.ctl / TOKEN / "state").exists() and "torn down" not in r.stderr


def test_cleanup_failed_stop_with_a_failing_requery_preserves(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("stop_fail")
    w.set("query_fail_after_stop")
    r = w.cleanup(TOKEN)
    assert r.returncode == 5, r.stderr
    assert (w.ctl / TOKEN / "state").exists()


def test_cleanup_refuses_a_container_with_another_token(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    (w.F / "c/qdistro-tier3s-smoke/label").write_text(TOKEN2 + "\n")
    r = w.cleanup(TOKEN)
    assert r.returncode == 4 and "carries token" in r.stderr
    assert not any(c.startswith("podman stop") for c in w.calls())


def test_cleanup_stops_a_scope_that_does_not_empty(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("scope_sticky")            # podman stop leaves the scope's processes behind
    r = w.cleanup(TOKEN)
    assert r.returncode == 0, r.stderr
    assert f"systemctl stop qdistro-tier3s-{TOKEN}.scope" in w.calls()
    assert w.launch_gone(TOKEN)


def test_cleanup_preserves_when_the_scope_will_not_empty(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("scope_sticky")
    w.set("scope_stop_fail")
    r = w.cleanup(TOKEN)
    assert r.returncode == 6, r.stderr
    assert (w.ctl / TOKEN / "state").exists()


def test_cleanup_refuses_a_scope_at_another_cgroup(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    (w.F / "units" / f"qdistro-tier3s-{TOKEN}.scope.cgroup").write_text(
        f"/user.slice/qdistro-tier3s-{TOKEN}.scope\n")
    r = w.cleanup(TOKEN)
    assert r.returncode == 6 and "not the recorded" in r.stderr
    assert (w.ctl / TOKEN / "state").exists()
    assert not any(c.startswith(("podman stop", "podman rm", "systemctl stop")) for c in w.calls())


def test_cleanup_reports_a_recorded_process_alive_outside_the_scope(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    (w.F / "c/qdistro-tier3s-smoke/pids").write_text("4001\n")   # the Sentry survives the stop
    r = w.cleanup(TOKEN)
    assert r.returncode == 7, r.stderr
    assert "recorded sentry 4002 is alive outside the scope" in r.stderr
    assert (w.ctl / TOKEN / "state").exists()


@pytest.mark.parametrize("field,value", [
    ("admin_uid", "0"), ("scope_unit", f"qdistro-tier3s-{TOKEN2}.scope"),
    ("per_launch_dir", "/home/admin"), ("container", "../x"), ("unit", "sshd.service")])
def test_cleanup_rejects_a_malformed_record(w, field, value):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke", record={field: value})
    r = w.cleanup(TOKEN)
    assert r.returncode == 1, r.stderr
    assert (w.ctl / TOKEN / "state").exists()
    assert not any(c.startswith("podman") for c in w.calls())


def test_cleanup_rejects_a_loose_record(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    (w.ctl / TOKEN / "state").chmod(0o644)
    r = w.cleanup(TOKEN)
    assert r.returncode == 1 and "not a root 0600 file" in r.stderr


def test_two_launches_tearing_down_one_preserves_the_other(w):
    w.make_launch(TOKEN, "qdistro-tier3s-silo@a.service", "qdistro-tier3s-a", pids=(4001, 4002))
    w.make_launch(TOKEN2, "qdistro-tier3s-silo@b.service", "qdistro-tier3s-b", pids=(5001, 5002))
    r = w.cleanup("--unit", "qdistro-tier3s-silo@a.service")
    assert r.returncode == 0, r.stderr
    assert w.launch_gone(TOKEN)
    assert (w.ctl / TOKEN2 / "state").exists() and (w.launch_parent / TOKEN2).exists()
    assert (w.F / "c/qdistro-tier3s-b/running").exists()
    assert (w.T / "proc/5002/stat").exists()
    assert not any("qdistro-tier3s-b" in c or TOKEN2 in c for c in w.calls()
                   if c.startswith(("podman stop", "podman rm", "systemctl stop")))


def test_reap_stale_skips_live_units_and_reaps_dead_ones(w):
    w.make_launch(TOKEN, "qdistro-tier3s-silo@a.service", "qdistro-tier3s-a", pids=(4001, 4002))
    w.make_launch(TOKEN2, "qdistro-tier3s-silo@b.service", "qdistro-tier3s-b", pids=(5001, 5002))
    w.set_unit("qdistro-tier3s-silo@b.service", "failed")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 0, r.stderr
    assert (w.ctl / TOKEN / "state").exists() and w.launch_gone(TOKEN2)


def test_reap_stale_podman_listing_failure_is_an_error(w):
    w.set("ps_fail")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 1 and "nothing reaped by label" in r.stderr


def test_reap_stale_reaps_an_unrecorded_labelled_container(w):
    w.make_launch(TOKEN2, "qdistro-tier3s-silo@b.service", "qdistro-tier3s-b", pids=(5001, 5002))
    w.set_unit("qdistro-tier3s-silo@b.service", "inactive")
    shutil.rmtree(w.ctl / TOKEN2)                    # the manager lost the record
    r = w.cleanup("--reap-stale")
    assert r.returncode == 0, r.stderr
    assert "UNRECORDED labelled container qdistro-tier3s-b" in r.stderr
    assert "podman rm -f -t 10 qdistro-tier3s-b" in w.calls()
    assert f"systemctl stop qdistro-tier3s-{TOKEN2}.scope" in w.calls()
    assert not (w.launch_parent / TOKEN2).exists()


def test_reap_stale_keeps_a_labelled_container_of_a_live_unit(w):
    w.make_launch(TOKEN2, "qdistro-tier3s-silo@b.service", "qdistro-tier3s-b", pids=(5001, 5002))
    shutil.rmtree(w.ctl / TOKEN2)
    r = w.cleanup("--reap-stale")
    assert r.returncode == 0, r.stderr
    assert (w.F / "c/qdistro-tier3s-b/running").exists()


def test_reap_stale_stops_a_stale_scope_then_removes_the_orphan_dir(w):
    # the record is gone, the launch unit is not live, but its scope is still
    # live (systemd's BindsTo stop not finished): the reaper waits, stops the
    # scope itself and removes the per-launch dir (A-iii s122 finding)
    (w.launch_parent / TOKEN).mkdir()
    w.set_unit(f"qdistro-tier3s-{TOKEN}.scope", "active")
    (w.F / f"units/qdistro-tier3s-{TOKEN}.scope.bindsto").write_text("qdistro-tier3s-silo@a.service\n")
    w.set_unit("qdistro-tier3s-silo@a.service", "inactive")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 0, r.stderr
    assert f"systemctl stop qdistro-tier3s-{TOKEN}.scope" in w.calls()
    assert not (w.launch_parent / TOKEN).exists()


def test_reap_stale_leaves_an_orphan_dir_whose_scope_serves_a_live_unit(w):
    (w.launch_parent / TOKEN).mkdir()
    w.set_unit(f"qdistro-tier3s-{TOKEN}.scope", "active")
    (w.F / f"units/qdistro-tier3s-{TOKEN}.scope.bindsto").write_text("qdistro-tier3s-silo@a.service\n")
    w.set_unit("qdistro-tier3s-silo@a.service", "active")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 0, r.stderr
    assert (w.launch_parent / TOKEN).exists()
    assert f"systemctl stop qdistro-tier3s-{TOKEN}.scope" not in w.calls()


def test_cleanup_without_record_removes_only_an_orphan_dir(w):
    (w.launch_parent / TOKEN).mkdir()
    w.set_unit(f"qdistro-tier3s-{TOKEN}.scope", "active")
    r = w.cleanup(TOKEN)
    assert r.returncode == 1 and (w.launch_parent / TOKEN).exists()
    w.set_unit(f"qdistro-tier3s-{TOKEN}.scope", "inactive")
    assert w.cleanup(TOKEN).returncode == 0
    assert not (w.launch_parent / TOKEN).exists()


# --- test hooks are refused for root -----------------------------------------

def root_cmd(cmd):
    if os.geteuid() == 0:
        return cmd
    if not shutil.which("unshare") or subprocess.run(["unshare", "-r", "true"],
                                                     capture_output=True).returncode != 0:
        pytest.skip("needs euid 0 or a user namespace")
    return [shutil.which("unshare"), "-r", "--", *cmd]


@pytest.mark.parametrize("script,args", [
    (SPAWN, ARGV), (CLEANUP, [TOKEN]), (HELPER, ["enter", TOKEN, "1000", "--", "podman", "ps"])])
def test_test_root_hook_is_refused_for_root(w, script, args):
    r = subprocess.run(root_cmd(["/bin/bash", str(script), *args]), env=w.env(),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "TIER3S_TEST_ROOT is a unit-test hook and is refused for root" in r.stderr
    assert w.calls() == []


# --- seccomp profile and image recipe ------------------------------------------

def test_seccomp_profile_is_rendered_and_decided():
    r = subprocess.run(["python3", "tier3s/seccomp/make-profiles.py", "--check"], cwd=REPO,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    d = json.loads((T3S / "seccomp/headless-smoke.json").read_text())
    dec = d["tier3sDecisions"]
    assert sorted(dec) == sorted(["fchmodat2", "llistxattr", "setfsuid", "setfsgid",
                                  "fadvise64", "link", "syslog"])
    allowed = {n for g in d["syscalls"] if g["action"] == "SCMP_ACT_ALLOW" for n in g["names"]}
    for name, v in dec.items():
        assert (name in allowed) == (v["decision"] == "ALLOW"), name
        assert v["reason"]
    assert dec["fchmodat2"]["decision"] == "DENY" and "inert" in dec["fchmodat2"]["reason"]
    assert d["defaultAction"] == "SCMP_ACT_ERRNO"


def test_seccomp_generator_refuses_an_inert_allow(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("mkp", T3S / "seccomp/make-profiles.py")
    mkp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mkp)
    monkeypatch.chdir(REPO)
    monkeypatch.setitem(mkp.WORKLOADS["headless-smoke"], "fchmodat2", ("ALLOW", "x"))
    with pytest.raises(AssertionError, match="inert"):
        mkp.render("headless-smoke")


def test_make_tier3s_image_builds_on_the_pin(tmp_path):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    seen = tmp_path / "seen"
    pin = [l.split("=", 1)[1] for l in (REPO / "snapshot.conf").read_text().splitlines()
           if l.startswith("snapshot=")][0]
    write_exec(fakebin / "podman", f'''#!/bin/bash
case "$1" in
build) for a; do last="$a"; done; ls "$last" > {seen}.ctx; cat "$last/SNAPSHOT" > {seen}
       printf '%s\\n' "$@" > {seen}.args ;;
image) case "$*" in *org.qdistro.snapshot*) cat {seen} ;; *.Id*) echo imgid ;; *) echo sha256:dd ;; esac ;;
esac
''')
    env = dict(os.environ, PATH=f"{fakebin}:{os.environ['PATH']}", TMPDIR=str(tmp_path))
    r = subprocess.run(["bash", str(T3S / "make-tier3s-image.sh"), "headless-smoke"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert seen.read_text().strip() == pin
    assert "--tag\nqdistro/tier3s-headless-smoke:latest" in (tmp_path / "seen.args").read_text()
    assert sorted((tmp_path / "seen.ctx").read_text().split()) == sorted(
        ["Containerfile.headless-smoke", "SNAPSHOT", "configure-snapshot-repos.sh", "headless-smoke.sh"])
    assert "IMAGE_ID=imgid" in r.stdout and f"IMAGE_SNAPSHOT={pin}" in r.stdout
    assert not (T3S / "SNAPSHOT").exists()


def test_image_recipe_pins_repos_before_refresh_and_sets_identity():
    cf = (T3S / "Containerfile.headless-smoke").read_text()
    assert "FROM registry.opensuse.org/opensuse/tumbleweed:${SNAPSHOT}" in cf
    assert cf.index("configure-snapshot-repos.sh /usr/lib/qdistro/tier3s/SNAPSHOT") < cf.index("zypper --non-interactive --gpg-auto-import-keys refresh")
    assert "glibc-locale-base" in cf and "LANG=C.UTF-8" in cf
    assert "admin:x:1000:1000:qdistro admin:/home/admin:/bin/bash" in cf
    assert 'org.qdistro.snapshot="${SNAPSHOT}"' in cf
    assert (T3S / "make-tier3s-image.sh").read_text().splitlines()[1] == \
        "# copied from tier2/make-tier2-image.sh, unify later"
    assert "/usr/lib/qdistro/tier3s/SNAPSHOT" in (T3S / "configure-snapshot-repos.sh").read_text()
