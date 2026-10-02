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
for fd in 8 9; do [ ! -e "/proc/$$/fd/$fd" ] || echo "podman HELD lock fd $fd" >> "$F/calls"; done
while [ $# -gt 0 ]; do
    case "$1" in
        --runtime|--cgroup-manager) shift 2 ;;
        --runtime-flag=*|--cgroup-manager=*) shift ;;
        *) break ;;
    esac
done
sub="$1"; shift
for a in "$@"; do name="$a"; done          # the container is the last argument (except run)
if [[ "${name:-}" =~ ^[0-9a-f]{64}$ ]]; then   # a container ID: the container dir that carries it
    for c in "$F"/c/*/; do [ "$(cat "$c/id" 2>/dev/null)" = "$name" ] && { name="$(basename "$c")"; break; }; done
fi
if [ -e "$F/hang_name" ] && [ "${name:-}" = "$(cat "$F/hang_name")" ] && [ "$sub" != run ]; then
    # astra A r2 #2: a helper that ignores SIGTERM, in the call's process group
    [ ! -e "$F/hang_desc" ] || bash -c "trap '' TERM; echo \$\$ >> '$F/desc.pids'; exec sleep 600" &
    echo $$ >> "$F/hung.pids"; sleep 600; exit 0      # a wedged podman call (timeout(1) must kill it)
fi
finish() {   # the container's processes end, --rm removes it, its scope goes away
    local c="$F/c/$1" p rel
    [ -d "$c" ] || return 0
    rm -f "$c/running"
    # rm_keep: the marker survives — an rm that exits 0 but keeps the
    # container, which only the post-removal verdict can expose (A r3 P1)
    [ -e "$F/rm_keep" ] || rm -f "$c/exists"
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
    [ ! -e "$F/inspect_delay" ] || sleep "$(cat "$F/inspect_delay")"
    [ -e "$F/c/$name/running" ] || { echo "Error: no such container $name" >&2; exit 125; }
    # a launch that never reaches running (fable A r2 P3-3)
    if [ -e "$F/inspect_created" ]; then echo "created 0 0 x"; exit 0; fi
    cat "$F/c/$name/inspect_line" ;;
container)
    case "$1" in
        exists)
            [ ! -e "$F/query_fail" ] || { echo "Error: database is locked" >&2; exit 125; }
            if [ -e "$F/query_fail_after_stop" ] && grep -q '^podman stop' "$F/calls"; then
                echo "Error: database is locked" >&2; exit 125; fi
            # exists_stdout: junk on stdout the PMRC verdict then shares —
            # a verdict must be alone on the call's output (A r3 P1/P3-4)
            [ ! -e "$F/exists_stdout" ] || cat "$F/exists_stdout"
            [ -e "$F/c/$name/exists" ] && exit 0; exit 1 ;;
        inspect)
            # a concurrent teardown removes the container under our feet
            if [ -e "$F/inspect_vanish" ]; then finish "$name"; rm -rf "${F:?}/c/$name"
                echo "Error: no such container $name" >&2; exit 125; fi
            [ ! -e "$F/inspect_fail" ] || { echo "Error: inspect failed" >&2; exit 125; }
            [ -e "$F/c/$name/exists" ] || { echo "Error: no such container $name" >&2; exit 125; }
            # astra A r2 #2: a helper in a NEW session keeps the captured
            # output open after the call returned (a process-group kill
            # misses it; only the call scope's cgroup would get it)
            if [ -e "$F/inspect_leak" ]; then setsid sleep 600 & echo $! >> "$F/leak.pids"; fi
            echo "$(cat "$F/c/$name/id") $(cat "$F/c/$name/label")" ;;
    esac ;;
stop)
    if [ -e "$F/stop_vanish" ]; then finish "$name"; rm -rf "${F:?}/c/$name"
        echo "Error: no container with name or ID $name found" >&2; exit 125; fi
    [ ! -e "$F/stop_fail" ] || { echo "Error: given PID did not die within timeout" >&2; exit 125; }
    finish "$name" ;;
rm) [ -e "$F/rm_keep" ] || { finish "$name"; rm -rf "${F:?}/c/$name"; } ;;
ps)
    [ ! -e "$F/ps_fail" ] || { echo "Error: cannot list" >&2; exit 125; }
    if [ -e "$F/ps_garbage" ]; then echo '{"not": "a list"'; exit 0; fi
    fmt=""; filt=""; prev=""
    for a in "$@"; do [ "$prev" = --format ] && fmt="$a"; [ "$prev" = --filter ] && filt="$a"; prev="$a"; done
    [ "$fmt" = json ] || { echo "fake podman ps: only --format json is modelled, got '$fmt'" >&2; exit 125; }
    # podman 6's JSON: Labels is a map, Names a list (the label FILES hold the
    # raw label bytes, newlines and '|' included)
    python3 - "$F/c" "$filt" <<'PY'
import json, os, sys
root, filt = sys.argv[1], sys.argv[2]
want = filt.split("=", 2)[2] if filt.count("=") >= 2 else None
out = []
for n in sorted(os.listdir(root)):
    d = os.path.join(root, n)
    if not os.path.exists(os.path.join(d, "exists")):
        continue
    def rd(f):
        try:
            with open(os.path.join(d, f)) as fh:
                v = fh.read()
        except FileNotFoundError:
            return None
        return v[:-1] if v.endswith("\n") else v
    labels = {}
    if rd("label") is not None:
        labels["qdistro_tier3s_token"] = rd("label")
    if rd("unit_label") is not None:
        labels["qdistro_tier3s_unit"] = rd("unit_label")
    if "qdistro_tier3s_token" not in labels or (want is not None and labels["qdistro_tier3s_token"] != want):
        continue
    out.append({"Id": rd("id"), "Names": [n], "Labels": labels})
print(json.dumps(out, indent=1))
PY
    ;;
run)
    printf '%s\n' "$@" > "$F/run_argv"
    while [ $# -gt 0 ]; do [ "$1" = --name ] && { name="$2"; break; }; shift; done
    c="$F/c/$name"; mkdir -p "$c"; touch "$c/exists" "$c/running"
    tok="$(sed -n 's/^qdistro_tier3s_token=//p' "$F/run_argv")"
    echo "$tok" > "$c/label"; sed -n 's/^qdistro_tier3s_unit=//p' "$F/run_argv" > "$c/unit_label"
    echo "$tok$tok" > "$c/id"
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
case " $* " in
*" --unit=qdistro-t3s-call-"*)
    # the cleanup's per-call scope (astra A r2 #2): logged on its own line,
    # its cgroup modelled as an (empty) dir the cleanup must SIGKILL
    # (cgroup.kill) after the call; callscope_sticky keeps a process in it.
    # sdrun_fail_at=<n>: the n-th call-scope StartTransientUnit fails BEFORE
    # the payload (runuser/podman) ever runs — the A r3 P1 provenance hole.
    unit=""; for a in "$@"; do case "$a" in --unit=*) unit="${a#--unit=}" ;; esac; done
    n=$(cat "$F/sdrun_n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$F/sdrun_n"
    if [ -e "$F/sdrun_fail_at" ] && [ "$n" -ge "$(cat "$F/sdrun_fail_at")" ]; then
        echo "callscope $unit FAILED (injected StartTransientUnit failure)" >> "$F/callscopes"
        echo "Failed to start transient scope unit: injected failure" >&2
        exit 1
    fi
    echo "callscope $unit $*" >> "$F/callscopes"
    d="$T/sys/fs/cgroup/system.slice/$unit"; mkdir -p "$d"; : > "$d/cgroup.procs"
    [ ! -e "$F/callscope_sticky" ] || echo 999999 > "$d/cgroup.procs"
    while [ $# -gt 0 ] && [ "$1" != -- ]; do shift; done; shift
    exec "$@" ;;
esac
echo "systemd-run $*" >> "$F/calls"
echo "systemd-run NOTIFY_SOCKET=${NOTIFY_SOCKET-unset}" >> "$F/env_seen"
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
[ "$1" != --no-ask-password ] || shift
echo "systemctl $*" >> "$F/calls"
state() {   # the unit's ActiveState as systemd would answer it
    if [ -s "$F/units/$1.seq" ]; then          # a scripted state sequence, one state per query
        s="$(head -1 "$F/units/$1.seq")"; sed -i 1d "$F/units/$1.seq"; echo "$s" > "$F/units/$1.state"
    else s="$(cat "$F/units/$1.state" 2>/dev/null || echo inactive)"; fi
}
case "$1" in
is-active)
    [ ! -e "$F/units/$2.fail" ] || { echo "Failed to connect to bus" >&2; exit 1; }
    state "$2"; echo "$s"; [ "$s" = active ] ;;
show) for a; do u="$a"; done
    case " $* " in
        *" ActiveState "*)
            [ ! -e "$F/units/$u.fail" ] || { echo "Failed to connect to bus" >&2; exit 1; }
            [ ! -e "$F/show_delay" ] || sleep "$(cat "$F/show_delay")"
            [ ! -e "$F/units/$u.hang_before" ] || sleep 600
            state "$u"; echo "$s"
            # astra A r2 #1: an answer printed by a query that then hangs or fails
            [ ! -e "$F/units/$u.hang_after" ] || { echo $$ >> "$F/hung.pids"; sleep 600; }
            [ ! -e "$F/units/$u.fail_after" ] || exit 1
            exit 0 ;;
        *" BindsTo "*) [ ! -e "$F/show_fail" ] || exit 1; cat "$F/units/$u.bindsto" 2>/dev/null ;;
        *" ControlGroup "*) [ ! -e "$F/show_cg_fail" ] || exit 1
            [ ! -e "$F/show_cg_fail_after_stop" ] || ! grep -q '^podman stop' "$F/calls" || exit 1
            cat "$F/units/$u.cgroup" 2>/dev/null ;;
        *) exit 1 ;;
    esac
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
# runuser_fail: the privilege drop itself fails (the payload never runs) —
# rc 1 the supervisor produces, not podman's "absent" (A r3 P1)
[ ! -e "$F/runuser_fail" ] || { echo "runuser: injected setup failure" >&2; exit 1; }
shift 3; exec "$@"
'''

FAKE_GETENT = r'''#!/bin/bash
F=@F@
# getent_hang: a wedged NSS lookup — must die at its bound, not hold the
# token lock for the lock's whole allowance (fable A r3 P3-2)
[ ! -e "$F/getent_hang" ] || sleep 600
exec /usr/bin/getent "$@"
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

FAKE_NOTIFY = r'''#!/bin/bash
echo "systemd-notify $* NOTIFY_SOCKET=${NOTIFY_SOCKET-unset}" >> @F@/calls
echo "$PPID" > @F@/notify_ppid
exit "$(cat @F@/notify_rc 2>/dev/null || echo 0)"
'''

# fable A r2 P3-6: is the global lock held while .new-<token> is removed?
FAKE_RM = r'''#!/bin/bash
case " $* " in *"/.new-"*)
    if flock -n @T@/run/qdistro-tier3s-ctl/.lock true 2>/dev/null; then echo "rm .new unlocked" >> @F@/calls
    else echo "rm .new locked" >> @F@/calls; fi ;;
esac
exec /usr/bin/rm "$@"
'''

FAKE_MV = r'''#!/bin/bash
case " $* " in *"/.new-"*) [ ! -e @F@/mv_new_fail ] || { echo "fake mv: refused" >&2; exit 1; } ;; esac
exec /usr/bin/mv "$@"
'''

FAKE_PROBE = r'''#!/bin/bash
echo "probe $*" >> @F@/calls
echo "probe NOTIFY_SOCKET=${NOTIFY_SOCKET-unset}" >> @F@/env_seen
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
                           ("chown", FAKE_CHOWN), ("systemd-notify", FAKE_NOTIFY),
                           ("getent", FAKE_GETENT),
                           ("rm", FAKE_RM), ("mv", FAKE_MV)):
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

    def cleanup(self, *args, **kw):
        return subprocess.run(["bash", str(CLEANUP), *args], env=self.env(**kw),
                              capture_output=True, text=True, timeout=60)

    def cleanup_bg(self, *args, **kw):
        return subprocess.Popen(["bash", str(CLEANUP), *args], env=self.env(**kw),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

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
        # a spawn-created scope always carries BindsTo=<launch unit>
        (self.F / "units" / f"qdistro-tier3s-{token}.scope.bindsto").write_text(unit + "\n")
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
        (c / "id").write_text(token + token + "\n")
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
    # stop and rm act on the inspected container ID, never on a name another
    # container could take meanwhile
    assert f"podman stop -t 10 {TOKEN}{TOKEN}" in calls, calls
    assert f"podman rm -f --ignore {TOKEN}{TOKEN}" in calls, calls
    # podman runs as the recorded admin, never as root, and never holding a
    # lock fd (a long-lived podman child would keep the lock)
    assert all(calls[i - 1].startswith("runuser -u") for i, c in enumerate(calls)
               if c.startswith("podman") and "HELD" not in c)
    assert not any("HELD lock fd" in c for c in calls), calls


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


def test_cleanup_systemd_run_failure_is_not_an_absent_verdict(w):
    """astra+fable A r3 P1: a refused StartTransientUnit exits the
    timeout→systemd-run→runuser→podman chain with a bare 1 BEFORE podman
    runs. Without an in-call PMRC verdict there is no "absent": the run
    fails, the record and the launch survive, and no podman call ever ran."""
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    (w.F / "sdrun_fail_at").write_text("1")
    r = w.cleanup(TOKEN)
    assert r.returncode == 4, r.stderr
    assert "podman query failed" in r.stderr and "torn down" not in r.stderr
    assert (w.ctl / TOKEN / "state").exists() and (w.launch_parent / TOKEN).exists()
    assert not any(c.startswith("podman") for c in w.calls())
    assert (w.F / "c/qdistro-tier3s-smoke/exists").exists()


def test_cleanup_runuser_failure_is_not_an_absent_verdict(w):
    """A r3 P1: the privilege drop itself failing (the scope came up, podman
    never ran) is likewise a failed query — never "absent"."""
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("runuser_fail")
    r = w.cleanup(TOKEN)
    assert r.returncode == 4, r.stderr
    assert "torn down" not in r.stderr
    assert (w.ctl / TOKEN / "state").exists() and (w.launch_parent / TOKEN).exists()
    assert not any(c.startswith("podman") for c in w.calls())


def test_cleanup_exists_verdict_that_shares_its_output_is_a_failed_query(w):
    """The PMRC line counts only when it IS the whole output: junk on the
    call's stdout (a leaking layer above podman) must never smuggle an
    "absent" past the verdict check."""
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    (w.F / "exists_stdout").write_text("garbage from an earlier layer\n")
    r = w.cleanup(TOKEN)
    assert r.returncode == 4 and "torn down" not in r.stderr
    assert (w.ctl / TOKEN / "state").exists()
    assert (w.F / "c/qdistro-tier3s-smoke/exists").exists()


def test_cleanup_failed_vanish_recheck_preserves(w):
    """The re-check after a failed inspect gets the same provenance: a
    supervisor failure there is a failed query, never 'vanished'."""
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("inspect_fail")
    # calls in the scope chain: exists, inspect — the verdict re-check is third
    (w.F / "sdrun_fail_at").write_text("3")
    r = w.cleanup(TOKEN)
    assert r.returncode == 4 and "podman inspect of qdistro-tier3s-smoke failed" in r.stderr
    assert (w.ctl / TOKEN / "state").exists() and "torn down" not in r.stderr
    assert not any(c.startswith(("podman stop", "podman rm")) for c in w.calls())
    assert (w.F / "c/qdistro-tier3s-smoke/exists").exists()


def test_cleanup_post_rm_check_with_a_failed_chain_preserves(w):
    """Post-removal: a failed supervisor chain is "still present or query
    failed" — never the absent verdict that finishes a teardown."""
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    # exists, inspect, stop, rm run; the post-rm exists check's chain fails
    (w.F / "sdrun_fail_at").write_text("5")
    r = w.cleanup(TOKEN)
    assert r.returncode == 5 and "still present or query failed" in r.stderr
    assert (w.ctl / TOKEN / "state").exists() and "torn down" not in r.stderr
    assert not any(c.startswith("systemctl stop") for c in w.calls())


def test_cleanup_rm_that_quietly_keeps_the_container_is_not_torn_down(w):
    """podman rm exits 0 but the container survives: the post-rm verdict
    PMRC=0 must block the teardown just like a failed query."""
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("rm_keep")
    r = w.cleanup(TOKEN)
    assert r.returncode == 5 and "still present or query failed" in r.stderr
    assert (w.ctl / TOKEN / "state").exists() and "torn down" not in r.stderr
    assert (w.F / "c/qdistro-tier3s-smoke/exists").exists()


def test_reap_stale_labelled_recheck_with_a_failed_chain_preserves(w):
    """--reap-stale's labelled reaper: its post-rm existence check is a PMRC
    verdict too — a failed supervisor chain must not close the reaper leg."""
    w.make_launch(TOKEN2, "qdistro-tier3s-silo@b.service", "qdistro-tier3s-b", pids=(5001, 5002))
    w.set_unit("qdistro-tier3s-silo@b.service", "inactive")
    shutil.rmtree(w.ctl / TOKEN2)                    # the manager lost the record
    # the record loop is empty; calls: podman ps (listing), rm, then the check
    (w.F / "sdrun_fail_at").write_text("3")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 1 and "still present or query failed" in r.stderr
    # the rm itself ran (it may), but a bare chain rc must not read as the
    # "absent" verdict: the leg is an error, not a quiet success


def test_a_call_that_overflows_its_output_file_is_a_failed_query(w):
    """fable A r3 P3-4: a call writing more than OUT_MAX to its out file is
    a failed query — the cap message proves the bound, and the record stays."""
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    (w.F / "exists_stdout").write_text("x" * 200000)
    r = w.cleanup(TOKEN)
    assert r.returncode == 4 and "cap" in r.stderr and "failed query" in r.stderr, r.stderr
    assert (w.ctl / TOKEN / "state").exists() and "torn down" not in r.stderr
    assert (w.F / "c/qdistro-tier3s-smoke/exists").exists()


def test_an_nss_lookup_is_bounded_under_the_token_lock(w):
    """fable A r3 P3-2: the admin NSS lookup holds the token lock, so it must
    be bounded — a wedged getent is killed at its (deadline-capped) bound and
    the query fails closed, never waited on for the lock's full allowance."""
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("getent_hang")
    t0 = time.time()
    r = w.cleanup(TOKEN, TIER3S_TEST_TMO="3")
    took = time.time() - t0
    assert r.returncode == 4 and "no user for uid" in r.stderr
    assert took < 30, f"an unbounded getent held the run for {took:.0f}s"
    assert (w.ctl / TOKEN / "state").exists() and (w.launch_parent / TOKEN).exists()
    assert not any(c.startswith("podman") for c in w.calls())


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


@pytest.mark.parametrize("sticky", [False, True])
def test_reap_stale_reaps_an_unrecorded_labelled_container(w, sticky):
    w.make_launch(TOKEN2, "qdistro-tier3s-silo@b.service", "qdistro-tier3s-b", pids=(5001, 5002))
    w.set_unit("qdistro-tier3s-silo@b.service", "inactive")
    shutil.rmtree(w.ctl / TOKEN2)                    # the manager lost the record
    if sticky:
        w.set("scope_sticky")                        # the scope outlives the container
    r = w.cleanup("--reap-stale")
    assert r.returncode == 0, r.stderr
    assert "UNRECORDED labelled container qdistro-tier3s-b" in r.stderr
    assert f"podman rm -f -t 10 {TOKEN2}{TOKEN2}" in w.calls()
    # a scope that outlives its container (live, bound to the dead b.service)
    # is stopped through the guarded orphan path, never unconditionally
    # (sol A-iii r4 P1); an emptied scope ends by itself
    assert (f"systemctl stop qdistro-tier3s-{TOKEN2}.scope" in w.calls()) == sticky
    assert (w.F / f"units/qdistro-tier3s-{TOKEN2}.scope.state").read_text().strip() == "inactive"
    assert not (w.launch_parent / TOKEN2).exists()


@pytest.mark.parametrize("label", ["", "sshd.service"])
def test_reap_stale_preserves_a_container_without_a_valid_unit_label(w, label):
    # sol A-iii r3 P1: no positive 'dead' launch unit, no reap
    w.make_launch(TOKEN2, "qdistro-tier3s-silo@b.service", "qdistro-tier3s-b", pids=(5001, 5002))
    shutil.rmtree(w.ctl / TOKEN2)
    (w.F / "c/qdistro-tier3s-b/unit_label").write_text(label + "\n" if label else "")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 1 and "has no valid qdistro_tier3s_unit label" in r.stderr
    assert (w.F / "c/qdistro-tier3s-b/running").exists()
    assert not any(c.startswith(("podman rm", "systemctl stop")) for c in w.calls())


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


def _orphan_with_live_scope(w, bound="qdistro-tier3s-silo@a.service"):
    (w.launch_parent / TOKEN).mkdir()
    w.set_unit(f"qdistro-tier3s-{TOKEN}.scope", "active")
    (w.F / f"units/qdistro-tier3s-{TOKEN}.scope.bindsto").write_text(bound + "\n")


@pytest.mark.parametrize("case", ["show-fails", "no-bindsto", "foreign-bindsto"])
def test_reap_stale_preserves_a_live_scope_whose_owner_is_unknown(w, case):
    # sol A-iii r1 P1: a failed or ambiguous BindsTo lookup must never lead
    # to stopping a live scope
    _orphan_with_live_scope(w, {"show-fails": "qdistro-tier3s-silo@a.service", "no-bindsto": "",
                                "foreign-bindsto": "sshd.service"}[case])
    if case == "show-fails":
        w.set("show_fail")
    w.set_unit("qdistro-tier3s-silo@a.service", "inactive")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 1, r.stderr
    assert "cannot tell which launch unit owns" in r.stderr
    assert f"systemctl stop qdistro-tier3s-{TOKEN}.scope" not in w.calls()
    assert (w.launch_parent / TOKEN).exists()


def test_reap_stale_rechecks_the_launch_unit_before_stopping_the_scope(w):
    # the bound unit is not live at the first look but live again by the time
    # the bounded wait ends: the scope is left alone
    _orphan_with_live_scope(w)
    (w.F / "units/qdistro-tier3s-silo@a.service.seq").write_text("inactive\nactive\n")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 1, r.stderr
    assert "is live again or unknown" in r.stderr
    assert f"systemctl stop qdistro-tier3s-{TOKEN}.scope" not in w.calls()
    assert (w.launch_parent / TOKEN).exists()


# sol A-iii r2 P1: a failed unit-state query is "unknown", never "not live"

def test_reap_stale_unknown_unit_state_preserves_a_recorded_launch(w):
    w.make_launch(TOKEN, "qdistro-tier3s-silo@a.service", "qdistro-tier3s-a", pids=(4001, 4002))
    (w.F / "units/qdistro-tier3s-silo@a.service.fail").write_text("")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 1 and "cannot tell whether qdistro-tier3s-silo@a.service is live" in r.stderr
    assert (w.ctl / TOKEN / "state").exists() and (w.F / "c/qdistro-tier3s-a/running").exists()
    assert not any(c.startswith(("podman stop", "podman rm", "systemctl stop")) for c in w.calls())


def test_reap_stale_unknown_unit_state_preserves_an_unrecorded_container(w):
    w.make_launch(TOKEN2, "qdistro-tier3s-silo@b.service", "qdistro-tier3s-b", pids=(5001, 5002))
    shutil.rmtree(w.ctl / TOKEN2)
    (w.F / "units/qdistro-tier3s-silo@b.service.fail").write_text("")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 1 and "NOT reaping qdistro-tier3s-b" in r.stderr
    assert (w.F / "c/qdistro-tier3s-b/running").exists() and (w.launch_parent / TOKEN2).exists()
    assert not any(c.startswith(("podman rm", "systemctl stop")) for c in w.calls())


@pytest.mark.parametrize("which", ["scope", "bound-unit"])
def test_reap_stale_unknown_state_preserves_an_orphan_scope(w, which):
    _orphan_with_live_scope(w)
    w.set_unit("qdistro-tier3s-silo@a.service", "inactive")
    unit = f"qdistro-tier3s-{TOKEN}.scope" if which == "scope" else "qdistro-tier3s-silo@a.service"
    (w.F / f"units/{unit}.fail").write_text("")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 1 and "cannot tell whether" in r.stderr
    assert f"systemctl stop qdistro-tier3s-{TOKEN}.scope" not in w.calls()
    assert (w.launch_parent / TOKEN).exists()


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


# --- astra + fable Phase A r1 ----------------------------------------------------
# sol A-iii r4 P1 / astra 3 / fable P1-1: a valid but STALE unit name never
# authorizes the teardown of a live scope that another unit owns.

def _live_b_with_stale_name(w, where):
    """A live launch of b.service (token TOKEN2, its scope bound to b) whose
    record or container label names the valid but dead old.service."""
    w.make_launch(TOKEN2, "qdistro-tier3s-silo@b.service", "qdistro-tier3s-b", pids=(5001, 5002))
    w.set_unit("qdistro-tier3s-silo@old.service", "inactive")
    if where == "record":
        st = w.ctl / TOKEN2 / "state"
        st.write_text(st.read_text().replace("unit=qdistro-tier3s-silo@b.service",
                                             "unit=qdistro-tier3s-silo@old.service"))
    else:
        shutil.rmtree(w.ctl / TOKEN2)
        (w.F / "c/qdistro-tier3s-b/unit_label").write_text("qdistro-tier3s-silo@old.service\n")


@pytest.mark.parametrize("where", ["record", "label"])
def test_reap_stale_refuses_a_stale_unit_name_on_a_live_scope_owned_by_another_unit(w, where):
    _live_b_with_stale_name(w, where)
    r = w.cleanup("--reap-stale")
    assert r.returncode == 1, r.stderr
    assert "is bound to 'qdistro-tier3s-silo@b.service', not to qdistro-tier3s-silo@old.service" in r.stderr
    assert (w.F / "c/qdistro-tier3s-b/running").exists() and (w.T / "proc/5002/stat").exists()
    assert not any(c.startswith(("podman stop", "podman rm", "systemctl stop")) for c in w.calls())
    if where == "record":
        assert (w.ctl / TOKEN2 / "state").exists()
    assert (w.launch_parent / TOKEN2).exists()


def _scopeless_container(w, name, token, unit_label):
    c = w.F / "c" / name
    c.mkdir(parents=True)
    for f, v in (("exists", ""), ("running", ""), ("label", token + "\n"), ("id", token * 2 + "\n"),
                 ("unit_label", unit_label)):
        (c / f).write_text(v)
    return c


@pytest.mark.parametrize("label", ["", "sshd.service\n"])
def test_reap_stale_preserves_a_scopeless_container_without_a_valid_unit_label(w, label):
    c = _scopeless_container(w, "qdistro-tier3s-nolabel", TOKEN2, label)
    r = w.cleanup("--reap-stale")
    assert r.returncode == 1 and "has no valid qdistro_tier3s_unit label" in r.stderr
    assert c.exists()
    assert not any(x.startswith("podman rm") for x in w.calls())


def test_reap_stale_rechecks_the_owner_before_any_stop(w):
    # the recorded unit reads dead at the reaper's first look, live again by
    # the time the teardown checks the scope's owner: nothing is stopped
    w.make_launch(TOKEN2, "qdistro-tier3s-silo@b.service", "qdistro-tier3s-b", pids=(5001, 5002))
    (w.F / "units/qdistro-tier3s-silo@b.service.seq").write_text("inactive\nactive\n")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 1, r.stderr
    assert "its owner qdistro-tier3s-silo@b.service is live (again)" in r.stderr
    assert not any(c.startswith(("podman stop", "podman rm", "systemctl stop")) for c in w.calls())
    assert (w.ctl / TOKEN2 / "state").exists()


def test_cleanup_a_dead_scope_with_a_populated_cgroup_preserves(w):
    # systemd reports the scope inactive/failed but its cgroup still holds a
    # process (abandoned): never "gone"
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("scope_sticky")
    w.set_unit(f"qdistro-tier3s-{TOKEN}.scope", "failed")
    r = w.cleanup(TOKEN)
    assert r.returncode == 6, r.stderr
    assert "holds processes or cannot be read" in r.stderr
    assert (w.ctl / TOKEN / "state").exists()


def test_reap_stale_reaps_a_labelled_container_that_has_no_scope(w):
    # the s122 "ghost": a labelled container started outside any launch unit
    # (no scope, no record) whose unit label is dead is still reaped
    c = w.F / "c/qdistro-tier3s-ghost"
    c.mkdir(parents=True)
    for f, v in (("exists", ""), ("running", ""), ("label", TOKEN2 + "\n"), ("id", TOKEN2 * 2 + "\n"),
                 ("unit_label", "qdistro-tier3s-silo@ghost.service\n")):
        (c / f).write_text(v)
    r = w.cleanup("--reap-stale")
    assert r.returncode == 0, r.stderr
    assert f"podman rm -f -t 10 {TOKEN2}{TOKEN2}" in w.calls()
    assert not c.exists()


def test_reap_stale_except_unit_reaps_only_an_older_token_of_the_spawn_unit(w):
    # fable P3-6: the spawn's own unit is live; its record under an OLDER token
    # is stale (one unit runs one launch); its NEW token is never a candidate
    w.make_launch(TOKEN2, w.unit, "qdistro-tier3s-old", pids=(5001, 5002))
    newer = "44444444444444444444444444444444"
    w.make_launch(newer, w.unit, "qdistro-tier3s-new", pids=(6001, 6002))
    r = w.cleanup("--reap-stale", "--except-unit", w.unit, "--token", newer)
    assert r.returncode == 0, r.stderr
    assert w.launch_gone(TOKEN2)
    assert (w.ctl / newer / "state").exists() and (w.F / "c/qdistro-tier3s-new/running").exists()
    # without the exception the live unit's records are not stale at all
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 0 and (w.ctl / TOKEN / "state").exists() and (w.ctl / newer / "state").exists()


@pytest.mark.parametrize("args", [["--except-unit", "qdistro-tier3s-silo@smoke.service"],
                                  ["--token", TOKEN], ["--except-unit", "sshd.service", "--token", TOKEN],
                                  ["--deadline", "0"]])
def test_reap_stale_option_validation(w, args):
    r = w.cleanup("--reap-stale", *args)
    assert r.returncode == 2 and "usage" in r.stderr
    assert w.calls() == []


# astra 2: a failed scope query or recursive scan never reads as "gone/empty"

def _created_record(w, token=TOKEN):
    """A phase=created record (no scope_cgroup, no pids yet) whose scope is
    live with a process and whose container is absent."""
    w.make_launch(token, w.unit, "qdistro-tier3s-smoke")
    st = w.ctl / token / "state"
    st.write_text("".join(l + "\n" for l in st.read_text().splitlines()
                          if not l.startswith(("scope_cgroup=", "conmon_", "sentry_", "container_id="))
                          ).replace("phase=running", "phase=created"))
    shutil.rmtree(w.F / "c/qdistro-tier3s-smoke")


@pytest.mark.parametrize("flag", ["show_cg_fail", "show_cg_fail_after_stop"])
def test_cleanup_a_failed_controlgroup_query_preserves(w, flag):
    if flag == "show_cg_fail":
        _created_record(w)
    else:
        w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")     # the LATER lookup fails
    w.set(flag)
    r = w.cleanup(TOKEN)
    assert r.returncode == 6, r.stderr
    assert "ControlGroup query for" in r.stderr and "torn down" not in r.stderr
    assert (w.ctl / TOKEN / "state").exists() and (w.launch_parent / TOKEN).exists()
    assert f"systemctl stop qdistro-tier3s-{TOKEN}.scope" not in w.calls()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
@pytest.mark.parametrize("damage", ["unreadable-procs", "unreadable-dir"])
def test_cleanup_a_failed_recursive_scan_is_not_empty(w, damage):
    # the recorded container, conmon and Sentry are gone, but the scope has a
    # child cgroup the scan cannot read; the scope cannot be stopped
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("scope_sticky")
    w.set("scope_stop_fail")
    d = Path(f"{w.T}/sys/fs/cgroup/system.slice/qdistro-tier3s-{TOKEN}.scope")
    (d / "cgroup.procs").write_text("")          # the recorded processes ended
    (d / "sub").mkdir()
    (d / "sub/cgroup.procs").write_text("4999\n")
    target = d / "sub/cgroup.procs" if damage == "unreadable-procs" else d / "sub"
    target.chmod(0)
    try:
        r = w.cleanup(TOKEN)
    finally:
        target.chmod(0o755)
    assert r.returncode == 6, r.stderr
    assert "torn down" not in r.stderr
    assert (w.ctl / TOKEN / "state").exists()


def test_tree_scan_counts_a_child_cgroup_process(w):
    # the recursive scan sees a descendant in a child cgroup (gofer/stub
    # leftovers), so the scope is stopped rather than declared empty
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("scope_sticky")
    d = Path(f"{w.T}/sys/fs/cgroup/system.slice/qdistro-tier3s-{TOKEN}.scope")
    (d / "cgroup.procs").write_text("")
    (d / "sub").mkdir()
    (d / "sub/cgroup.procs").write_text("4999\n")
    r = w.cleanup(TOKEN)
    assert r.returncode == 0, r.stderr
    assert f"systemctl stop qdistro-tier3s-{TOKEN}.scope" in w.calls()


# astra 1 / fable P2-3: the record appears complete or not at all

def test_spawn_killed_before_publication_leaves_no_record(w):
    import fcntl
    w.set("run_block")
    lock = open(w.ctl / ".lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)            # the global lock is busy
    p = subprocess.Popen(["bash", str(SPAWN), *ARGV], env=w.env(TIER3S_TEST_TMO="1"),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True)
    try:
        wait_for(lambda: any(c.startswith("podman ps") for c in w.calls()), "the reaper ran", p)
        time.sleep(0.5)                          # the spawn now waits on the lock
        assert p.poll() is None
        os.killpg(p.pid, 9)                      # SIGKILL: no trap runs
        p.communicate(timeout=10)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    assert sorted(x.name for x in w.ctl.iterdir()) == [".lock"]
    assert list(w.launch_parent.iterdir()) == []
    assert w.first("systemd-run") is None
    r = w.cleanup("--reap-stale")
    assert r.returncode == 0, r.stderr


def test_published_record_is_complete(w):
    w.set("run_block")
    seen = []
    p = w.start()
    try:
        # every time the record dir exists, its state already has every create field
        def complete():
            d = w.ctl / TOKEN
            if d.exists():
                st = w.state()
                seen.append(st.get("phase"))
                assert {"schema", "token", "container", "unit", "scope_unit", "admin_uid",
                        "runsc_root", "per_launch_dir", "phase"} <= set(st)
                return st.get("phase") == "running"
            return False
        wait_for(complete, "phase=running", p)
        w.set("release")
        p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0 and w.launch_gone(TOKEN)


def test_reap_stale_removes_a_dead_spawns_unpublished_record(w):
    d = w.ctl / f".new-{TOKEN}"
    d.mkdir(mode=0o700)
    (d / "state").write_text("schema=1\n")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 0, r.stderr
    assert not d.exists()


def _incomplete(w, token=TOKEN):
    d = w.ctl / token
    d.mkdir(mode=0o700)
    d.chmod(0o700)
    (w.launch_parent / token).mkdir()
    return d


@pytest.mark.parametrize("how", [["--reap-stale"], ["--unit", "qdistro-tier3s-silo@unrelated.service"], [TOKEN]])
def test_an_incomplete_record_is_recovered_when_nothing_ran_under_it(w, how):
    # earlier code could leave a record dir without its state (interrupted
    # creation): restart recovery (--reap-stale), an unrelated unit's stop
    # (--unit) and the token's own cleanup all recover it on positive evidence
    d = _incomplete(w)
    r = w.cleanup(*how)
    assert r.returncode == 0, r.stderr
    assert "incomplete control record removed" in r.stderr
    assert not d.exists() and not (w.launch_parent / TOKEN).exists()


@pytest.mark.parametrize("case", ["scope-live", "scope-unknown", "container", "listing-fails"])
def test_an_incomplete_record_is_preserved_without_positive_evidence(w, case):
    d = _incomplete(w)
    if case == "scope-live":
        w.set_unit(f"qdistro-tier3s-{TOKEN}.scope", "active")
    elif case == "scope-unknown":
        (w.F / f"units/qdistro-tier3s-{TOKEN}.scope.fail").write_text("")
    elif case == "container":
        c = w.F / "c/qdistro-tier3s-x"
        c.mkdir(parents=True)
        for f, v in (("exists", ""), ("label", TOKEN + "\n"), ("id", TOKEN * 2 + "\n"),
                     ("unit_label", "qdistro-tier3s-silo@x.service\n")):
            (c / f).write_text(v)
        w.set_unit("qdistro-tier3s-silo@x.service", "active")
    else:
        w.set("ps_fail")
    r = w.cleanup("--unit", "qdistro-tier3s-silo@unrelated.service")
    assert r.returncode == 1, r.stderr
    assert d.exists() and (w.launch_parent / TOKEN).exists()


# astra 5 / fable P3-1, P3-2: bounded calls, per-token locks

def test_a_wedged_podman_call_is_bounded_and_blocks_no_other_launch(w):
    import fcntl
    w.make_launch(TOKEN, "qdistro-tier3s-silo@a.service", "qdistro-tier3s-a", pids=(4001, 4002))
    w.make_launch(TOKEN2, "qdistro-tier3s-silo@b.service", "qdistro-tier3s-b", pids=(5001, 5002))
    w.set("hang_name", "qdistro-tier3s-a")
    t0 = time.time()
    pa = w.cleanup_bg("--unit", "qdistro-tier3s-silo@a.service", TIER3S_TEST_TMO="3")
    try:
        wait_for(lambda: (w.F / "hung.pids").exists(), "A's podman call to hang", pa)
        # B's teardown and a new record publication proceed while A hangs
        r = w.cleanup("--unit", "qdistro-tier3s-silo@b.service", TIER3S_TEST_TMO="3")
        assert r.returncode == 0, r.stderr
        assert w.launch_gone(TOKEN2)
        with open(w.ctl / ".lock", "w") as g:
            fcntl.flock(g, fcntl.LOCK_EX | fcntl.LOCK_NB)      # the global lock is free
        assert pa.poll() is None, "A's cleanup is still bounded by its timeout"
        out, err = pa.communicate(timeout=30)
    finally:
        if pa.poll() is None:
            pa.kill()
    assert pa.returncode == 1, err
    assert "podman query failed" in err and time.time() - t0 < 25
    assert (w.ctl / TOKEN / "state").exists() and (w.F / "c/qdistro-tier3s-a/running").exists()
    for pid in (w.F / "hung.pids").read_text().split():
        assert not Path(f"/proc/{pid}").exists(), f"the wedged podman {pid} was not killed"
    # A's lock was released with the helper: once podman answers, it tears down
    (w.F / "hang_name").unlink()
    r = w.cleanup("--unit", "qdistro-tier3s-silo@a.service")
    assert r.returncode == 0, r.stderr
    assert w.launch_gone(TOKEN)


def test_reap_stale_skips_a_token_another_teardown_holds(w):
    import fcntl
    w.make_launch(TOKEN, "qdistro-tier3s-silo@a.service", "qdistro-tier3s-a", pids=(4001, 4002))
    w.set_unit("qdistro-tier3s-silo@a.service", "failed")
    fd = os.open(w.ctl / TOKEN, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        t0 = time.time()
        r = w.cleanup("--reap-stale", TIER3S_TEST_TMO="5")
        assert time.time() - t0 < 3, "the reaper must not wait on a busy token"
    finally:
        os.close(fd)
    assert r.returncode == 0 and "busy" in r.stderr
    assert (w.ctl / TOKEN / "state").exists()
    assert not any(c.startswith(("podman stop", "podman rm")) for c in w.calls())


def test_reap_stale_deadline_preserves_what_it_did_not_reach(w):
    w.make_launch(TOKEN, "qdistro-tier3s-silo@a.service", "qdistro-tier3s-a", pids=(4001, 4002))
    w.set_unit("qdistro-tier3s-silo@a.service", "failed")
    w.set("hang_name", "qdistro-tier3s-a")
    w.make_launch(TOKEN2, "qdistro-tier3s-silo@b.service", "qdistro-tier3s-b", pids=(5001, 5002))
    w.set_unit("qdistro-tier3s-silo@b.service", "failed")
    r = w.cleanup("--reap-stale", "--deadline", "1", TIER3S_TEST_TMO="2")
    assert r.returncode == 1
    # A (first) hangs past the deadline; B is not started and stays intact
    assert f"{TOKEN2}: reap deadline reached" in r.stderr
    # nor is the label listing, or any orphan dir, started after it (astra A r2 #3)
    assert "labelled containers and orphan per-launch dirs not processed" in r.stderr
    assert not any(c.startswith("podman ps") for c in w.calls())
    assert (w.ctl / TOKEN / "state").exists() and (w.ctl / TOKEN2 / "state").exists()
    assert (w.F / "c/qdistro-tier3s-b/running").exists()


# fable P2-2: admin-chosen label bytes never steer the reaper

def test_reap_stale_label_bytes_cannot_inject_or_shift_fields(w):
    hexes = "55555555555555555555555555555555"
    w.make_launch(TOKEN, "qdistro-tier3s-silo@live.service", "qdistro-tier3s-live", pids=(4001, 4002))
    c = w.F / "c/qdistro-tier3s-evil"
    c.mkdir(parents=True)
    for f, v in (("exists", ""), ("running", ""), ("label", TOKEN2 + "\n"), ("id", TOKEN2 * 2 + "\n"),
                 ("unit_label", f"qdistro-tier3s-silo@x.service\n{hexes}|qdistro-tier3s-silo@y.service"
                                f"|qdistro-tier3s-live\n")):
        (c / f).write_text(v)
    for u in ("x", "y"):
        w.set_unit(f"qdistro-tier3s-silo@{u}.service", "inactive")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 1, r.stderr
    assert f"{TOKEN2}: labelled container qdistro-tier3s-evil has no valid qdistro_tier3s_unit label" in r.stderr
    assert (w.F / "c/qdistro-tier3s-live/running").exists() and c.exists()
    assert not any(c_.startswith(("podman stop", "podman rm", "systemctl stop")) for c_ in w.calls())


def test_reap_stale_bad_listing_json_is_an_error(w):
    w.set("ps_garbage")
    r = w.cleanup("--reap-stale")
    assert r.returncode == 1 and "nothing reaped by label" in r.stderr


# astra 4 / fable P2-1: Type=notify; READY=1 only once the launch runs

def test_spawn_sends_ready_once_recorded_running_and_hides_the_socket(w):
    w.set("run_block")
    p = w.start(NOTIFY_SOCKET="/run/systemd/notify")
    try:
        wait_for(lambda: any(c.startswith("systemd-notify") for c in w.calls()), "READY", p)
        assert w.state()["phase"] == "running"
        ready = [c for c in w.calls() if c.startswith("systemd-notify")]
        assert len(ready) == 1 and "--ready" in ready[0] and "NOTIFY_SOCKET=/run/systemd/notify" in ready[0]
        assert w.first("systemd-notify") > w.first("systemd-run")
        # the socket never reaches the probe or the scope (podman runs under env -i)
        assert (w.F / "env_seen").read_text().splitlines() == [
            "probe NOTIFY_SOCKET=unset", "systemd-run NOTIFY_SOCKET=unset"]
        w.set("release")
        p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0


@pytest.mark.parametrize("setup", ["deny", "probe", "image", "podman-fails"])
def test_a_refused_or_failed_launch_never_sends_ready(w, setup):
    {"deny": lambda: w.set("dbus_mode", "deny"), "probe": lambda: w.set("probe_rc", "1"),
     "image": lambda: w.set("image_rc", "1"), "podman-fails": lambda: w.set("run_rc", "125")}[setup]()
    r = w.spawn(NOTIFY_SOCKET="/run/systemd/notify")
    assert r.returncode != 0, r.stderr
    assert w.first("systemd-notify") is None


def test_a_short_workload_that_completed_sends_ready(w):
    r = w.spawn(NOTIFY_SOCKET="/run/systemd/notify")
    assert r.returncode == 0, r.stderr
    assert len([c for c in w.calls() if c.startswith("systemd-notify --ready")]) == 1
    assert w.launch_gone(TOKEN)


def test_a_failed_ready_tears_the_launch_down(w):
    w.set("run_block")
    w.set("notify_rc", "1")
    r = w.spawn(NOTIFY_SOCKET="/run/systemd/notify")
    assert r.returncode == 2 and "cannot send READY=1" in r.stderr
    assert w.launch_gone(TOKEN)


# --- astra A r2 #1: a state printed by a query that did not complete is unknown

def _pid_gone(pid, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(f"/proc/{pid}/stat") as fh:
                if fh.read().split(") ", 1)[1].startswith("Z"):
                    return True
        except (FileNotFoundError, IndexError):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("how", ["hang_after", "fail_after"])
@pytest.mark.parametrize("path", ["record", "label", "incomplete", "scope"])
def test_a_state_printed_before_a_timeout_or_error_is_no_evidence(w, path, how):
    unit = "qdistro-tier3s-silo@a.service"
    if path == "incomplete":
        _incomplete(w)
        probe = f"qdistro-tier3s-{TOKEN}.scope"
        w.set_unit(probe, "inactive")
    else:
        w.make_launch(TOKEN, unit, "qdistro-tier3s-a", pids=(4001, 4002))
        if path == "label":
            shutil.rmtree(w.ctl / TOKEN)
        if path == "scope":
            # the unit is genuinely dead, but the LIVE token scope's state
            # query prints "inactive" and then hangs/fails: rule 3 must not be
            # skipped (the scope is owned by another unit)
            w.set_unit(unit, "failed")
            (w.F / "units" / f"qdistro-tier3s-{TOKEN}.scope.bindsto").write_text(
                "qdistro-tier3s-silo@other.service\n")
            probe = f"qdistro-tier3s-{TOKEN}.scope"
            w.set_unit(probe, "inactive")       # what the broken query prints
        else:
            probe = unit
            w.set_unit(unit, "inactive")
    (w.F / "units" / f"{probe}.{how}").write_text("")
    r = w.cleanup("--reap-stale", TIER3S_TEST_TMO="1")
    assert r.returncode != 0, r.stderr
    assert not any(c.startswith(("podman stop", "podman rm", "systemctl stop")) for c in w.calls()), w.calls()
    if path in ("record", "scope", "incomplete"):
        assert (w.ctl / TOKEN).is_dir()
    if path != "incomplete":
        assert (w.F / "c/qdistro-tier3s-a/running").exists()
    for pid in (w.F / "hung.pids").read_text().split() if (w.F / "hung.pids").exists() else []:
        assert _pid_gone(pid), f"the hung state query {pid} survived"


# --- astra A r2 #2: every process of a call ends with it; nothing blocks on a pipe

def _token_lock_free(w, token=TOKEN):
    import fcntl
    fd = os.open(w.ctl / token, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def test_a_term_ignoring_helper_of_a_timed_out_call_is_killed(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("hang_name", "qdistro-tier3s-smoke")
    w.set("hang_desc")
    t0 = time.time()
    r = w.cleanup(TOKEN, TIER3S_TEST_TMO="1")
    assert r.returncode == 4 and "podman query failed" in r.stderr, r.stderr
    assert time.time() - t0 < 10
    desc = (w.F / "desc.pids").read_text().split()
    assert desc, "the fake did not start its TERM-ignoring helper"
    for pid in desc + (w.F / "hung.pids").read_text().split():
        assert _pid_gone(pid), f"{pid} of the timed-out call survived"
    assert (w.ctl / TOKEN / "state").exists() and _token_lock_free(w)


def test_a_helper_holding_the_output_open_blocks_nothing(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("inspect_leak")
    p = w.cleanup_bg(TOKEN)
    try:
        out, err = p.communicate(timeout=20)
    finally:
        for pid in ((w.F / "leak.pids").read_text().split() if (w.F / "leak.pids").exists() else []):
            try:
                os.kill(int(pid), 9)
            except ProcessLookupError:
                pass
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0, err
    assert (w.F / "leak.pids").exists() and w.launch_gone(TOKEN)


def test_a_term_to_the_cleanup_kills_its_call_in_flight(w):
    import signal
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("hang_name", "qdistro-tier3s-smoke")
    w.set("hang_desc")
    p = w.cleanup_bg(TOKEN, TIER3S_TEST_TMO="30")
    try:
        wait_for(lambda: (w.F / "desc.pids").exists() and (w.F / "hung.pids").exists(), "the hung call", p)
        t0 = time.time()
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=10)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 143 and time.time() - t0 < 5, err
    for pid in (w.F / "desc.pids").read_text().split() + (w.F / "hung.pids").read_text().split():
        assert _pid_gone(pid), f"{pid} of the cancelled call survived"
    assert (w.ctl / TOKEN / "state").exists() and _token_lock_free(w)
    assert not list(w.ctl.glob(".call-*")), "the work dir survived"


def test_admin_calls_run_in_their_own_scope_killed_after_the_call(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    r = w.cleanup(TOKEN)
    assert r.returncode == 0, r.stderr
    podman = [c for c in w.calls() if c.startswith("podman ")]
    scopes = (w.F / "callscopes").read_text().splitlines()
    assert len(scopes) == len(podman) >= 4
    for ln in scopes:
        unit = ln.split()[1]
        assert "--scope" in ln and "-p DefaultDependencies=no" in ln and "-p TimeoutStopSec=" in ln
        assert " -p RuntimeMaxSec=" in ln and "runuser -u" in ln
        kill = w.T / f"sys/fs/cgroup/system.slice/{unit}/cgroup.kill"
        assert kill.exists() and kill.read_text().strip() == "1", f"{unit} was not killed"
    assert len({ln.split()[1] for ln in scopes}) == len(scopes), "a call scope was reused"
    assert not list(w.ctl.glob(".call-*")), "the work dir survived"


def test_a_call_scope_that_does_not_empty_is_a_failed_query(w):
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("callscope_sticky")
    r = w.cleanup(TOKEN)
    assert r.returncode == 4 and "did not empty" in r.stderr and "podman query failed" in r.stderr, r.stderr
    assert (w.ctl / TOKEN / "state").exists()
    assert not any(c.startswith(("podman stop", "podman rm")) for c in w.calls())


def test_reap_stale_sweeps_the_work_dir_of_a_killed_cleanup(w):
    dead = 4194300; live = 4194301
    (w.T / f"proc/{live}").mkdir()        # the sweep checks $PROC, not /proc
    stale = w.ctl / f".call-{dead}-abcdef"
    stale.mkdir()
    (stale / "out").write_text("x")
    live_d = w.ctl / f".call-{live}-abcdef"
    live_d.mkdir()
    r = w.cleanup("--reap-stale")
    assert r.returncode == 0, r.stderr
    assert not stale.exists() and live_d.exists()


# --- astra A r2 #3 / fable A r2 P3-3: deadlines and waits are by the clock

def test_reap_stale_deadline_covers_the_orphan_dirs(w):
    toks = [f"{i:x}" * 32 for i in range(1, 7)]
    for t in toks:
        (w.launch_parent / t).mkdir()
    w.set("show_delay", "0.4")
    t0 = time.time()
    r = w.cleanup("--reap-stale", "--deadline", "1")
    took = time.time() - t0
    assert r.returncode == 1, r.stderr
    assert "orphan per-launch dir not processed" in r.stderr
    left = [t for t in toks if (w.launch_parent / t).exists()]
    assert 1 <= len(left) < 6, left
    assert took < 4, took


def test_a_query_is_cut_at_the_batch_deadline(w):
    w.make_launch(TOKEN, "qdistro-tier3s-silo@a.service", "qdistro-tier3s-a", pids=(4001, 4002))
    (w.F / "units" / "qdistro-tier3s-silo@a.service.hang_before").write_text("")
    t0 = time.time()
    r = w.cleanup("--reap-stale", "--deadline", "2")      # real bounds: 10 s query, 5 s kill
    took = time.time() - t0
    assert r.returncode == 1 and "cannot tell whether" in r.stderr, r.stderr
    assert took < 6, f"the 10 s query was not cut at the 2 s deadline ({took:.1f} s)"
    assert (w.ctl / TOKEN / "state").exists()


def test_the_unit_lock_wait_is_capped_by_the_deadline(w):
    import fcntl
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    fd = os.open(w.ctl / TOKEN, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        t0 = time.time()
        p = w.cleanup_bg("--unit", w.unit, "--deadline", "1")      # LOCK_WAIT is 120 s
        try:
            out, err = p.communicate(timeout=15)
        finally:
            if p.poll() is None:
                p.kill()
        took = time.time() - t0
    finally:
        os.close(fd)
    assert p.returncode == 1 and "another teardown holds it" in err, err
    assert took < 5, took


def test_the_bindsto_wait_is_by_the_clock(w):
    t = TOKEN
    (w.launch_parent / t).mkdir()
    rel = f"/system.slice/qdistro-tier3s-{t}.scope"
    (w.T / f"sys/fs/cgroup{rel}").mkdir(parents=True)
    (w.F / "units" / f"qdistro-tier3s-{t}.scope.cgroup").write_text(rel + "\n")
    (w.F / "units" / f"qdistro-tier3s-{t}.scope.bindsto").write_text("qdistro-tier3s-silo@a.service\n")
    w.set_unit(f"qdistro-tier3s-{t}.scope", "active")
    w.set_unit("qdistro-tier3s-silo@a.service", "failed")
    w.set("show_delay", "0.3")
    t0 = time.time()
    r = w.cleanup("--reap-stale")
    took = time.time() - t0
    assert r.returncode == 0, r.stderr
    assert f"systemctl stop qdistro-tier3s-{t}.scope" in w.calls()
    assert not (w.launch_parent / t).exists()
    assert took < 8, f"the 20 s (x4% in tests) wait ran by query count ({took:.1f} s)"


def test_the_start_poll_is_bounded_by_the_clock(w):
    w.set("run_block")
    w.set("inspect_created")
    w.set("inspect_delay", "0.5")
    t0 = time.time()
    p = w.start(TIER3S_TEST_POLL_S="2")
    try:
        out, err = p.communicate(timeout=20)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 2 and "did not reach running within 2 s" in err, err
    assert time.time() - t0 < 10
    assert w.first("systemd-notify") is None and w.launch_gone(TOKEN)


# --- astra A r2 #4: READY=1 comes from the unit's main PID (the spawn) itself

def test_ready_is_sent_by_the_spawn_process_itself(w):
    w.set("run_block")
    p = w.start(NOTIFY_SOCKET="/run/systemd/notify")
    try:
        wait_for(lambda: (w.F / "notify_ppid").exists(), "READY", p)
        ready = [c for c in w.calls() if c.startswith("systemd-notify")]
        assert len(ready) == 1 and "--pid" not in ready[0]
        # a direct child of the spawn: privileged systemd-notify then sends
        # with the spawn's PID, the one NotifyAccess=main accepts
        assert (w.F / "notify_ppid").read_text().strip() == str(p.pid)
        w.set("release")
        p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0


# --- fable A r2 P3-6: .new-<token> only ever exists under the global lock

def test_unpublished_record_is_removed_under_the_global_lock(w):
    w.set("mv_new_fail")
    r = w.spawn()
    assert r.returncode == 2 and "cannot write the control record" in r.stderr, r.stderr
    assert not list(w.ctl.glob(".new-*"))
    rms = [c for c in w.calls() if c.startswith("rm .new")]
    assert rms and rms[-1] == "rm .new locked", rms
