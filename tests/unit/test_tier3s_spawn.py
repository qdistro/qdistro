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
import re
import shutil
import signal
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
            # a verdict must be alone on the call's output (A r3 P1/P3-4).
            # Written raw (a NUL included): a NUL-prefixed 'PMRC=1' is sol
            # r5 P1's truncated-read acceptance
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
            # inspect_nul: '<id> <token>' followed by a NUL and junk — the
            # truncated read would accept the prefix (sol r5 P1)
            if [ -e "$F/inspect_nul" ]; then
                printf '%s %s\0junk\n' "$(cat "$F/c/$name/id")" "$(cat "$F/c/$name/label")"
            else
                echo "$(cat "$F/c/$name/id") $(cat "$F/c/$name/label")"
            fi ;;
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
            state "$u"
            # units/<u>.nul_state: a (wrong) state followed by a NUL and junk —
            # read -d '' would accept the truncated prefix (sol r5 P1)
            if [ -e "$F/units/$u.nul_state" ]; then printf 'inactive\0junk\n'
            else echo "$s"; fi
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
# getent_hang_after: a complete-looking answer, THEN a wedge — killed at the
# bound, and what it printed is not a result (sol r5 P3-4)
if [ -e "$F/getent_hang_after" ]; then /usr/bin/getent "$@"; sleep 600; fi
exec /usr/bin/getent "$@"
'''

FAKE_DBUS = r'''#!/bin/bash
F=@F@
act=""; method=""; args=""
for a; do
    case "$a" in
        org.qdistro.*.*) method="$a" ;;
        string:*) act="${a#string:}"; args="$args $a" ;;
        uint64:*) args="$args $a" ;;
    esac
done
echo "dbus-send $act" >> "$F/calls"
# the full method+args line, for tests that assert registration arguments
echo "$method$args" >> "$F/dbus_full"
if [ "$method" = org.qdistro.AdminBroker1.RegisterLaunch ]; then
    # reg_fail: every attempt fails — a GUI launch must bound its retries and
    # refuse BEFORE podman. reg_flaky=<n>: the first n-1 attempts fail.
    if [ -e "$F/reg_fail" ] || [ -e "$F/reg_flaky" ]; then
        n=$(cat "$F/reg_n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$F/reg_n"
        if [ ! -e "$F/reg_flaky" ] || [ "$n" -lt "$(cat "$F/reg_flaky")" ]; then
            echo "Error org.qdistro.AdminBroker1.Error.Failed: injected" >&2; exit 1
        fi
    fi
    echo '   uint32 1'; exit 0
fi
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

# The GUI bridge halves (CONTRACT.md §5 step 12). qdistro-secctx-exec:
# refuses without the trusted-launcher env, forks the payload (the waypipe
# client) as its inner child, publishes "<inner pid> <token>" at
# $T$QDISTRO_LAUNCH_RECORD_PATH, and puts its own (the wrapper's) fake /proc
# entry in place — the spawn records the wrapper's starttime at once. Modes:
# secctx_no_record (the record never appears), secctx_bad_token (the record
# carries another launch's token), secctx_dead_pid (the record carries a
# dead pid).
FAKE_SECCTX = r'''#!/bin/bash
F=@F@; T=@T@
umask 022          # the spawn's bridge subshell is umask 0177; fake /proc dirs need +x
echo "secctx $*" >> "$F/calls"
[ -n "${QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER:-}" ] || { echo "secctx: untrusted launcher" >&2; exit 2; }
eng=""; app=""; inst=""
while [ $# -gt 0 ]; do
    case "$1" in
        --sandbox-engine) eng="$2"; shift 2 ;;
        --app-id) app="$2"; shift 2 ;;
        --instance-id) inst="$2"; shift 2 ;;
        --) shift; break ;;
        *) shift ;;
    esac
done
echo "secctx-id engine=$eng app=$app inst=$inst" >> "$F/calls"
unit="$(cat "$F/launch_unit")"
mkdir -p "$T/proc/$$"
echo "0::/system.slice/$unit" > "$T/proc/$$/cgroup"
echo "$$ (qdistro-secctx) S $PPID 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 7770 0 0" > "$T/proc/$$/stat"
trap 'rm -rf "$T/proc/$$"' EXIT
"$@" &
inner=$!
# secctx_dead_pid: the client dies between publishing the record and the
# registration check — killed and REAPED, so the record carries a real dead
# pid (and its stale /proc entry goes with it). The wrapper stays up a
# moment so the record is polled against a live wrapper.
if [ -e "$F/secctx_dead_pid" ]; then
    kill -9 "$inner" 2>/dev/null; wait "$inner" 2>/dev/null; rm -rf "$T/proc/$inner"
    linger=1
else linger=""
fi
rec="$T$QDISTRO_LAUNCH_RECORD_PATH"; tok="$QDISTRO_LAUNCH_RECORD_TOKEN"
[ ! -e "$F/secctx_no_record" ] || rec=/dev/null
[ ! -e "$F/secctx_bad_token" ] || tok=00000000000000000000000000000bad
printf '%s %s\n' "$inner" "$tok" > "$rec"
[ -z "$linger" ] || sleep 0.5
wait $inner
'''

# The waypipe client half: binds the -s socket (a refusal before podman must
# still see the client die), puts its own fake /proc entry into the launch
# unit's cgroup (waypipe_bad_cgroup: a foreign one) and stays until killed.
FAKE_WAYPIPE = r'''#!/bin/bash
F=@F@; T=@T@
umask 022          # the spawn's bridge subshell is umask 0177; fake /proc dirs need +x
echo "waypipe $*" >> "$F/calls"
sock=""; prev=""
for a in "$@"; do [ "$prev" = -s ] && sock="$a"; prev="$a"; done
unit="$(cat "$F/launch_unit")"
[ ! -e "$F/waypipe_bad_cgroup" ] || unit=other.service
mkdir -p "$T/proc/$$"
echo "0::/system.slice/$unit" > "$T/proc/$$/cgroup"
echo "$$ (waypipe) S $PPID 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 7771 0 0" > "$T/proc/$$/stat"
# waypipe_die: the client dies after its launch record validates but before
# it can bind — the socket wait must see the death and refuse at once, not
# run out its clock bound (sol B-i r1 P2-7). 1.5 s is long enough that the
# record/cgroup/starttime validation has already recorded the client, so the
# death is the socket wait's to see.
if [ -e "$F/waypipe_die" ]; then
    echo "fake waypipe: dying early, never binding" >&2
    sleep 1.5
    rm -rf "$T/proc/$$"   # dead is dead: /proc goes with it (the EXIT trap is not armed yet)
    exit 1
fi
short=""
if [ -n "$sock" ] && [ ! -e "$F/waypipe_no_sock" ]; then
    # pytest tmp paths exceed sun_path (108): bind a short alias and link it
    # (production's /run/qdistro-tier3s/<token>/link.sock is always short)
    bind="$sock"
    if [ "${#sock}" -gt 100 ]; then short="/tmp/t3s-wp-$$"; bind="$short"; fi
    python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1])' "$bind" \
        || { echo "fake waypipe: cannot bind $bind" >&2; exit 1; }
    [ -z "$short" ] || ln -sf "$short" "$sock"
fi
sleep 600 &
sp=$!
trap 'kill "$sp" 2>/dev/null; [ -z "$short" ] || rm -f "$short"; rm -rf "$T/proc/$$"' EXIT
wait "$sp"
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
        (lib / "workloads").mkdir(parents=True)
        for f in (T3S / "seccomp").glob("*.json"):
            shutil.copyfile(f, lib / "seccomp" / f.name)
        for f in (T3S / "workloads").glob("*.env"):
            shutil.copyfile(f, lib / "workloads" / f.name)
        shutil.copyfile(T3S / "containers.conf", lib / "containers.conf")
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
                           ("qdistro-secctx-exec", FAKE_SECCTX), ("waypipe", FAKE_WAYPIPE),
                           ("rm", FAKE_RM), ("mv", FAKE_MV)):
            write_exec(self.bin / name, fill(text))
        run = self.T / "run"
        for d, mode in (("qdistro-tier3s-ctl", 0o700), ("qdistro-tier3s", 0o755),
                        ("qdistro-tier3s-runsc", 0o755), (f"qdistro-tier3s-runsc/{UID}", 0o700),
                        ("qdistro-tier3s-rt", 0o755), (f"qdistro-tier3s-rt/{UID}", 0o700),
                        (f"user/{UID}", 0o700)):
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
        # the fake bridge halves put their /proc entries in this unit's cgroup
        self.set("launch_unit", self.unit)

    # -- GUI world pieces (CONTRACT.md §5 step 12) --
    def compositor(self):
        """An admin compositor socket at $XDG_RUNTIME_DIR/wayland-1."""
        import socket as _socket
        s = _socket.socket(_socket.AF_UNIX)
        s.bind(str(self.T / f"run/user/{UID}/wayland-1"))
        s.close()          # the bound socket file stays; the spawn only stat()s it

    def launch_records(self):
        """The secctx launch-record files under the admin runtime dir — the
        file ids are fresh per-launch randoms, never the launch token."""
        return sorted((self.T / f"run/user/{UID}").glob("qdistro-tier3s-launchrec-*.pid"))

    def dbus_full(self):
        f = self.F / "dbus_full"
        return f.read_text().splitlines() if f.exists() else []

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
        kv = dict(l.split("=", 1) for l in lines
                  if not l.startswith(("PODMAN_ARG=", "SCOPE_ARG=", "BRIDGE_ARG=")))
        kv["podman"] = [l.split("=", 1)[1] for l in lines if l.startswith("PODMAN_ARG=")]
        kv["scope"] = [l.split("=", 1)[1] for l in lines if l.startswith("SCOPE_ARG=")]
        kv["bridge"] = [l.split("=", 1)[1] for l in lines if l.startswith("BRIDGE_ARG=")]
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
              "silo_uid": str(UID), "silo_user": f"qt3s-{self.silo or 'smoke'}",
              "silo": self.silo or "smoke",
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
                 ["--name", "qdistro-tier3s-smoke"],
                 ["--label", f"qdistro_tier3s_token={TOKEN}"],
                 ["--label", f"qdistro_tier3s_unit={w.unit}"]):
        assert any(pa[i:i + 2] == pair for i in range(run, len(pa))), pair
    # Phase C2 model A: keep-id without a guest-1000 retarget — the guest uid
    # IS the silo's host uid (the test-mode silo is the caller)
    assert "--userns=keep-id" in pa and f"--user={UID}:{os.getgid()}" in pa
    assert not any(a == "--user" or a == "--user=1000:1000" for a in pa)
    assert "--cap-drop=ALL" in pa and "--read-only" in pa and "--rm" in pa
    assert "seccomp=/usr/lib/qdistro/tier3s/seccomp/headless-smoke.json" in pa
    assert p["SECCOMP"] == f"{w.T}/usr/lib/qdistro/tier3s/seccomp/headless-smoke.json"
    # tmpfs ownership through podman's U option, never a literal uid= (podman 6.0.2 rejects it)
    # (the guest runtime dir is the SILO's numeric uid; an unbound launch gets a
    # fresh tmpfs home)
    assert f"/run/user/{UID}:rw,U,mode=0700" in pa and "/home/admin:rw,U,mode=0700" in pa
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
    # the three owning-scope limits (CONTRACT §3): tasks, memory, cpu — set
    # by root at scope creation; the limit files stay root's under the
    # helper's selective delegation so admin can never raise them (s130)
    assert "TasksMax=1024" in props and "MemoryMax=2G" in props \
        and "MemorySwapMax=0" in props and "CPUQuota=200%" in props
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
    # C2 model A: the MOUNTED state dir is the silo-owned one, not the
    # resolver's admin-side path (which stays bookkeeping only)
    assert pa[i + 1] == f"{w.T}/home/qt3s-smoke/tier3s-state/smoke:/home/admin:rw"
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
    # every podman call runs dropped to a user (the silo account; the test
    # seam resolves it to the test user) under the fixed model-A env
    # (per-silo runtime dir + the root-owned CONTAINERS_CONF pinning cgroupfs;
    # spawn's $T-prefixed LIBDIR and the cleanup's installed path both end in
    # tier3s/containers.conf)
    for i, c in enumerate(calls):
        if c.startswith("podman") and "HELD" not in c:
            assert calls[i - 1].startswith("runuser -u "), calls[i - 1]
            assert "env -i " in calls[i - 1] and "XDG_RUNTIME_DIR=" in calls[i - 1]
            assert "CONTAINERS_CONF=" in calls[i - 1] \
                and "tier3s/containers.conf" in calls[i - 1], calls[i - 1]


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


def test_launcher_leaves_unit_stop_teardown_to_execstop(w):
    w.set("run_block")
    p = w.start()
    try:
        wait_for(lambda: (w.ctl / TOKEN / "state").exists() and w.state().get("phase") == "running",
                 "phase=running", p)
        w.set_unit(w.unit, "deactivating")
        w.set("release")
        _, err = p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0, err
    assert "unit stop owns teardown" in err
    assert (w.ctl / TOKEN / "state").exists(), "the launcher's EXIT path ran cleanup during a unit stop"
    r = w.cleanup("--unit", w.unit)
    assert r.returncode == 0 and w.launch_gone(TOKEN), r.stderr


def test_launcher_signal_leaves_teardown_to_execstop(w):
    w.set("run_block")
    p = w.start()
    try:
        wait_for(lambda: (w.ctl / TOKEN / "state").exists() and w.state().get("phase") == "running",
                 "phase=running", p)
        p.send_signal(signal.SIGTERM)
        w.set("release")
        _, err = p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 143, err
    assert "signal: unit stop owns teardown" in err
    assert (w.ctl / TOKEN / "state").exists(), "the signalled launcher ran cleanup in its cgroup"
    r = w.cleanup("--unit", w.unit)
    assert r.returncode == 0 and w.launch_gone(TOKEN), r.stderr


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
    assert r.returncode == 2 and "not in the silo's podman store" in r.stderr
    assert w.first("systemd-run") is None
    assert w.launch_gone(TOKEN)


def test_missing_state_root_is_created_by_the_spawn(w):
    """C2: silo uids are dynamic, so the per-uid runsc root can no longer be
    tmpfiles-provisioned — the spawn creates it (silo-owned 0700) when absent."""
    w.state_root.rmdir()
    r = w.spawn()
    assert r.returncode == 0, r.stderr
    assert w.state_root.is_dir() and stat.S_IMODE(w.state_root.stat().st_mode) == 0o700


@pytest.mark.parametrize("damage", ["mode", "symlink"])
def test_tampered_state_root_refuses_before_any_record(w, damage):
    """An existing-but-wrong runsc root is refused, never chmod/chowned into
    place (what someone else planted stays planted and the launch dies)."""
    if damage == "mode":
        w.state_root.chmod(0o755)
    else:
        w.state_root.rename(w.tmp / "real")
        w.state_root.symlink_to(w.tmp / "real")
    r = w.spawn()
    assert r.returncode == 2 and "not a silo-owned 0700" in r.stderr
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
    assert f"CONTAINERS_CONF={w.T}/usr/lib/qdistro/tier3s/containers.conf" in ru[0]
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
    # lock fd (a long-lived podman child would keep the lock); each call
    # carries the silo podman env (runtime dir + pinned CONTAINERS_CONF)
    assert all(calls[i - 1].startswith("runuser -u") for i, c in enumerate(calls)
               if c.startswith("podman") and "HELD" not in c)
    assert all("CONTAINERS_CONF=" in calls[i - 1]
               and f"{w.T}/usr/lib/qdistro/tier3s/containers.conf" in calls[i - 1]
               for i, c in enumerate(calls)
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


def test_a_complete_nss_line_printed_before_a_stall_is_not_a_lookup(w):
    """sol r5 P3-4: a provider that prints a full passwd line and then
    wedges is killed at the bound — the substitution's status is timeout's,
    and a killed lookup is a failure, not a result to parse."""
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("getent_hang_after")
    r = w.cleanup(TOKEN, TIER3S_TEST_TMO="3")
    assert r.returncode == 4 and "NSS lookup failed or timed out" in r.stderr
    assert (w.ctl / TOKEN / "state").exists() and (w.launch_parent / TOKEN).exists()
    assert not any(c.startswith("podman") for c in w.calls())


def test_spawn_a_stalled_nss_answer_is_not_a_lookup(w):
    """sol r5 P3-4 in the spawn: a passwd line printed before the wedge is
    not a result — the launch refuses, before the probe, gate or podman."""
    w.set("getent_hang_after")
    r = w.spawn()
    assert r.returncode == 2 and "NSS lookup failed or timed out" in r.stderr
    assert not any(c.startswith(("dbus-send", "podman", "systemd-run")) for c in w.calls())


def test_helper_a_stalled_nss_answer_is_not_a_lookup(w):
    """sol r5 P3-4 in the scope helper: same status discipline."""
    w.set("getent_hang_after")
    r = run_helper(w, ["enter", TOKEN, str(UID), "--", "podman", "version"])
    assert r.returncode == 2 and "NSS lookup failed or timed out" in r.stderr, r.stderr
    assert not any(c.startswith(("chown", "runuser")) for c in w.calls())


def test_cleanup_an_exists_answer_with_a_nul_prefix_is_a_failed_query(w):
    """sol r5 P1: `read -d ''` stops at the first NUL — a call printing
    'PMRC=1\\0' then exiting 0 leaves 'PMRC=1\\0PMRC=0\\n' in the output
    file. The truncated prefix must never be the verdict: the query failed,
    record/container/scope survive and no stop/rm ran."""
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    (w.F / "exists_stdout").write_bytes(b"PMRC=1\x00")
    r = w.cleanup(TOKEN)
    assert r.returncode == 4 and "NOT treating it as absent" in r.stderr
    assert "torn down" not in r.stderr
    assert (w.ctl / TOKEN / "state").exists() and (w.launch_parent / TOKEN).exists()
    assert (w.F / "c/qdistro-tier3s-smoke/exists").exists()
    calls = w.calls()
    assert [c for c in calls if c.startswith("podman")] == \
        ["podman container exists qdistro-tier3s-smoke"]
    assert not any(c.startswith("systemctl stop") for c in calls)


def test_cleanup_a_nul_poisoned_unit_state_is_unknown(w):
    """sol r5 P1 audit: prop answers get the same whole-file discipline — a
    state line followed by a NUL and junk is a failed query (unknown), never
    the truncated 'inactive'. The scope is live; the launch must survive."""
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    (w.F / "units" / f"qdistro-tier3s-{TOKEN}.scope.nul_state").write_text("")
    r = w.cleanup(TOKEN)
    assert r.returncode == 6 and "cannot tell whether" in r.stderr
    assert "torn down" not in r.stderr
    assert (w.ctl / TOKEN / "state").exists() and (w.launch_parent / TOKEN).exists()
    assert not any(c.startswith("podman") for c in w.calls())
    assert (w.F / "c/qdistro-tier3s-smoke/exists").exists()


def test_cleanup_an_inspect_answer_with_extra_bytes_is_a_failed_query(w):
    """sol r5 P1 audit: the inspect line must also be the call's whole
    output — '<id> <token>' followed by a NUL and junk is re-queried like
    any failed inspect, and a still-present container means preserve."""
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke")
    w.set("inspect_nul")
    r = w.cleanup(TOKEN)
    assert r.returncode == 4 and "inspect" in r.stderr
    assert (w.ctl / TOKEN / "state").exists() and "torn down" not in r.stderr
    assert not any(c.startswith(("podman stop", "podman rm")) for c in w.calls())
    assert (w.F / "c/qdistro-tier3s-smoke/exists").exists()


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
    # the context stages every recipe input: all Containerfile.*, the repo
    # helper, the shared GUI entrypoint, the workload scripts, the pin
    assert sorted((tmp_path / "seen.ctx").read_text().split()) == sorted(
        [f.name for f in T3S.glob("Containerfile.*")]
        + ["SNAPSHOT", "configure-snapshot-repos.sh", "qdistro-tier3s-entrypoint",
           "headless-smoke.sh"])
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


def test_cache_manifest_emits_the_shared_snapshot_key_once(tmp_path):
    """sol B-i r1 P1-2: IMAGE_SNAPSHOT is workload-invariant — the manifest
    carries it exactly once, outside the per-workload keys; the guest setup's
    `m IMAGE_SNAPSHOT` reads every matching line and compares to the single
    snapshot.conf pin, so one line per workload breaks it."""
    src = (T3S / "cache-image-archive.sh").read_text()
    m = re.search(r"awk '\n(.*?)' \"\\\$d/build\.log\"", src, re.S)
    assert m, "manifest awk program not found in cache-image-archive.sh"
    prog = m.group(1).replace("\\$", "$")   # the heredoc escapes guest-side $
    log = tmp_path / "build.log"
    blocks = []
    for w, iid in (("foot", "id1"), ("headless-smoke", "id2"), ("weston-terminal", "id3")):
        blocks.append(f"IMAGE=qdistro/tier3s-{w}:latest\nIMAGE_ID={iid}\n"
                      f"IMAGE_DIGEST=sha256:d{w[0]}\nIMAGE_SNAPSHOT=20261001\n"
                      f"IMAGE_ARCHIVE=/d/tier3s-{w}.oci.tar\nIMAGE_ARCHIVE_SHA256=s{w[0]}\n")
    log.write_text("".join(blocks))
    r = subprocess.run(["awk", prog, str(log)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = r.stdout.splitlines()
    assert out.count("IMAGE_SNAPSHOT=20261001") == 1, out
    for w, iid in (("foot", "id1"), ("headless-smoke", "id2"), ("weston-terminal", "id3")):
        W = w.upper().replace("-", "_")
        assert f"IMAGE_{W}=qdistro/tier3s-{w}:latest" in out
        assert f"IMAGE_ID_{W}={iid}" in out and f"IMAGE_DIGEST_{W}=sha256:d{w[0]}" in out
        assert not any(l.startswith(f"IMAGE_SNAPSHOT_{W}=") for l in out)


def test_gui_images_use_the_bridge_entrypoint_and_the_installer_ships_it():
    """ΔB3 (sol B-i r1 P2-5): each GUI image's ENTRYPOINT IS the in-image
    bridge half — the spawn passes only the app argv (the plan test asserts
    that shape). The installer ships the entrypoint and the build context
    the CONTRACT's installed-paths table promises."""
    for name in ("foot", "weston-terminal"):
        cf = (T3S / f"Containerfile.{name}").read_text()
        assert 'ENTRYPOINT ["/usr/lib/qdistro/tier3s-entrypoint"]' in cf
        assert not re.search(r"^CMD ", cf, re.M), cf
        assert "COPY qdistro-tier3s-entrypoint /usr/lib/qdistro/tier3s-entrypoint" in cf
    ep = (T3S / "qdistro-tier3s-entrypoint").read_text()
    # C2 model A: XDG_RUNTIME_DIR is the spawn's per-silo env, with a uid-
    # derived fallback — never a fixed /run/user/1000
    for v in ('XDG_RUNTIME_DIR="/run/user/$(id -u)"', "LIBGL_ALWAYS_SOFTWARE=1",
              "QT_QUICK_BACKEND=software", "GDK_BACKEND=wayland",
              "QT_QPA_PLATFORM=wayland"):
        assert v in ep, v
    assert "XDG_RUNTIME_DIR=/run/user/1000" not in ep
    assert 'exec waypipe -s "$SOCK" -o --no-gpu server -- "$@"' in ep
    inst = (REPO / "scripts/install/install-session-manager.sh").read_text()
    for f in ("qdistro-tier3s-entrypoint", "Containerfile.*",
              "make-tier3s-image.sh", "headless-smoke.sh", "configure-snapshot-repos.sh"):
        assert f in inst, f


def test_seccomp_terminal_profiles_deny_link():
    """sol B-i r1 P2-6 / the CONTRACT seccomp table: `link` stays DENY for
    the terminal workloads — the only observed caller (fontconfig's cache
    lock) tolerated EPERM in Phase S; nothing else is proven to need it."""
    for name in ("weston-terminal", "foot"):
        d = json.loads((T3S / f"seccomp/{name}.json").read_text())
        assert d["tier3sDecisions"]["link"]["decision"] == "DENY"
        allowed = {n for g in d["syscalls"] if g["action"] == "SCMP_ACT_ALLOW"
                   for n in g["names"]}
        assert "link" not in allowed


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


def test_reap_stale_ages_call_dirs_against_pid_reuse(w):
    """A live pid keeps a .call-<pid>-* dir only while the dir is at least as
    new as THAT incarnation's start: an older dir is a dead run's leftover
    (pid reuse, fable A r3 P3-5 tightened in r5)."""
    live = 4194301
    d = w.T / f"proc/{live}"; d.mkdir()
    # field 20 starttime = 50000 ticks; CLK_TCK=100 -> start epoch btime + 500
    (d / "stat").write_text(
        f"{live} (cleanup) S 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 50000 0 0\n")
    (w.T / "proc/stat").write_text("cpu 0\nbtime 1000000\n")
    old = w.ctl / f".call-{live}-abcdef"; old.mkdir(); (old / "out").write_text("x")
    fresh = w.ctl / f".call-{live}-ghijkl"; fresh.mkdir()
    os.utime(old, (1000400, 1000400))     # older than the live pid's start
    os.utime(fresh, (1000600, 1000600))   # newer: could be this incarnation's
    r = w.cleanup("--reap-stale")
    assert r.returncode == 0, r.stderr
    assert not old.exists() and fresh.exists()


def test_cleanup_preserves_the_record_while_a_recorded_bridge_pid_lives(w):
    """A bridge process outside the launch scope, still live at its recorded
    starttime, is a teardown failure: the record is preserved (Phase B-i)."""
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke",
                  record={"bridge_wrapper_pid": "4194302",
                          "bridge_wrapper_starttime": "7770"})
    d = w.T / "proc/4194302"; d.mkdir()
    (d / "stat").write_text(
        "4194302 (waypipe) S 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 7770 0 0\n")
    r = w.cleanup(TOKEN)
    assert r.returncode == 7 and "bridge_wrapper 4194302 is alive" in r.stderr, r.stderr
    assert (w.ctl / TOKEN / "state").exists()


def test_cleanup_removes_a_recorded_launch_record(w):
    """The RegisterLaunch record under the admin XDG_RUNTIME_DIR is swept with
    the launch (CONTRACT.md B-i cleanup)."""
    lr = w.T / f"run/user/{UID}/launchrec.pid"; lr.parent.mkdir(exist_ok=True)
    lr.write_text("1 x")
    w.make_launch(TOKEN, w.unit, "qdistro-tier3s-smoke",
                  record={"launch_record": f"/run/user/{UID}/launchrec.pid"})
    r = w.cleanup(TOKEN)
    assert r.returncode == 0, r.stderr
    assert not lr.exists()


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


# ================= Phase B-i: the GUI waypipe bridge =========================
# CONTRACT.md §5 step 12: a workload whose declaration says GUI=1 gets a
# host-side waypipe client (runuser -> env -> qdistro-secctx-exec -> waypipe
# client) in the launch unit's cgroup, a launch record, RegisterLaunch before
# podman, the bridge dir mounted at /run/qdistro/link and host-uds=open.

GUI_ARGV = ["foot", "--", "foot"]
WT_ARGV = ["weston-terminal", "--", "weston-terminal"]


def bridge_call(w):
    for i, c in enumerate(w.calls()):
        if "qdistro-secctx-exec" in c:
            return i, c
    return None, None


def test_gui_plan_adds_bridge_flag_mount_and_entrypoint(w):
    p = w.plan(argv=GUI_ARGV)
    pa = p["podman"]
    assert p["GUI"] == "1"
    assert "--runtime-flag=host-uds=open" in pa
    i = pa.index("-v")
    assert pa[i + 1] == f"{w.launch_parent}/{TOKEN}:/run/qdistro/link:ro"
    # ΔB3: the image's ENTRYPOINT wraps the argv — the spawn passes ONLY the
    # app argv after the image name, never the entrypoint as a command arg
    assert pa[-2:] == ["localhost/qdistro/tier3s-foot:latest", "foot"]
    assert "qdistro-tier3s-entrypoint" not in pa
    ba = p["bridge"]
    assert ba[:4] == ["runuser", "-u", ME, "--"] and ba[4] == "env" and ba[5] == "-i"
    se = ba.index("qdistro-secctx-exec")
    assert ba[se:se + 6] == ["qdistro-secctx-exec", "--sandbox-engine", "qdistro.tier3s",
                             "--app-id", f"qdistro.tier3s.{w.silo}", "--instance-id"]
    assert ba[se + 6] == TOKEN and ba[se + 7] == "--"
    assert ba[-8:] == ["waypipe", "-s", f"{w.launch_parent}/{TOKEN}/link.sock",
                       "-o", "--no-gpu", "--title-prefix", "[3s:smoke] ", "client"]
    # the launch record env the secctx wrapper publishes to: the FILE id and
    # the verified NONCE are two independent fresh randoms — neither is the
    # launch token, which podman labels and the container name expose
    lrp = next(a.split("=", 1)[1] for a in ba if a.startswith("QDISTRO_LAUNCH_RECORD_PATH="))
    lrt = next(a.split("=", 1)[1] for a in ba if a.startswith("QDISTRO_LAUNCH_RECORD_TOKEN="))
    m = re.fullmatch(rf"/run/user/{UID}/qdistro-tier3s-launchrec-([0-9a-f]{{32}})\.pid", lrp)
    assert m, lrp
    assert re.fullmatch(r"[0-9a-f]{32}", lrt), lrt
    assert m.group(1) != TOKEN and lrt != TOKEN and m.group(1) != lrt


def test_headless_plan_has_no_bridge(w):
    p = w.plan()
    pa = p["podman"]
    assert p["GUI"] == "0" and p["bridge"] == []
    assert "--runtime-flag=host-uds=open" not in pa
    assert not any(a.startswith(f"{w.launch_parent}") or "/run/qdistro/link" in a for a in pa)
    assert pa[-2:] == ["localhost/qdistro/tier3s-headless-smoke:latest", "qdistro-tier3s-smoke"]


def test_gui_launch_registers_the_bridge_before_podman(w):
    w.compositor()
    w.set("run_block")
    p = w.start(argv=GUI_ARGV)
    try:
        wait_for(lambda: w.first("systemd-run") is not None, "podman launch", p)
        wait_for(lambda: (w.F / "run_argv").exists(), "podman run argv", p)
        calls = w.calls()
        bi, bc = bridge_call(w)
        assert bi is not None, calls
        assert "runuser -u" in bc and "-- waypipe" in bc and "client" in bc
        reg = w.first("dbus-send tier3s")
        assert reg is not None and bi < reg < w.first("systemd-run"), calls
        full = [l for l in w.dbus_full() if "RegisterLaunch" in l]
        assert len(full) == 1, full
        f = full[0]
        assert f"string:{w.silo}" in f and "string:qdistro.tier3s " in f \
            and f"string:qdistro.tier3s.{w.silo}" in f and f"string:{TOKEN}" in f \
            and "string:tier3s" in f and "uint64:7771" in f, f
        st = w.state()
        m = re.fullmatch(rf"/run/user/{UID}/qdistro-tier3s-launchrec-([0-9a-f]{{32}})\.pid",
                         st["launch_record"])
        assert st["gui"] == "1" and m and m.group(1) != TOKEN, st["launch_record"]
        assert re.fullmatch(r"[0-9]+", st["bridge_client_pid"])
        assert re.fullmatch(r"[0-9]+", st["bridge_client_starttime"])
        assert re.fullmatch(r"[0-9]+", st["bridge_wrapper_pid"])
        # the registered pid IS the waypipe client pid
        assert f"uint64:{st['bridge_client_pid']}" in f
        # the launch record is consumed once registration succeeded
        assert w.launch_records() == []
        assert (w.T / f"proc/{st['bridge_client_pid']}").exists()
        # identity: the secctx tag and the title prefix reached the client
        ident = [c for c in calls if c.startswith("secctx-id")]
        assert ident and f"app=qdistro.tier3s.{w.silo}" in ident[0] \
            and f"inst={TOKEN}" in ident[0] and "engine=qdistro.tier3s" in ident[0], ident
        assert "[3s:smoke]" in bc
        podman_run = [c for c in calls if c.startswith("podman") and " run " in c]
        assert podman_run and "--runtime-flag=host-uds=open" in podman_run[0], podman_run
        ra = (w.F / "run_argv").read_text()
        assert f"{w.launch_parent}/{TOKEN}:/run/qdistro/link:ro" in ra
        # ΔB3: only the app argv after the image — the image ENTRYPOINT wraps it
        assert "qdistro-tier3s-entrypoint" not in ra
        assert ra.rstrip().endswith("localhost/qdistro/tier3s-foot:latest\nfoot")
        w.set("release")
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, err
        assert w.launch_gone(TOKEN)
        assert not (w.T / "proc" / st["bridge_client_pid"]).exists(), "bridge client left behind"
        assert not (w.T / "proc" / st["bridge_wrapper_pid"]).exists(), "bridge wrapper left behind"
        assert w.launch_records() == []
    finally:
        if p.poll() is None:
            p.kill()


def test_gui_launch_without_compositor_refuses(w):
    r = w.spawn(argv=GUI_ARGV)
    assert r.returncode == 2 and "no admin compositor socket" in r.stderr, r.stderr
    assert w.first("dbus-send") is None and w.first("systemd-run") is None
    assert bridge_call(w)[0] is None


def test_gui_launch_registers_after_bounded_retries(w):
    w.compositor()
    w.set("reg_flaky", "3")          # attempts 1 and 2 fail, 3rd registers
    w.set("run_block")
    p = w.start(argv=GUI_ARGV)
    try:
        wait_for(lambda: w.first("systemd-run") is not None, "podman launch", p)
        regs = [l for l in w.dbus_full() if "RegisterLaunch" in l]
        assert len(regs) == 3, regs
        w.set("release")
        p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0


def test_gui_launch_refuses_when_registration_keeps_failing(w):
    w.compositor()
    w.set("reg_fail")
    r = w.spawn(argv=GUI_ARGV)
    assert r.returncode == 2 and "RegisterLaunch failed" in r.stderr, r.stderr
    # a post-client refusal goes through bridge_refuse: the client's log
    # tail is attached even when it is empty (sol B-i r2)
    assert "client log tail" in r.stderr, r.stderr
    regs = [l for l in w.dbus_full() if "RegisterLaunch" in l]
    assert len(regs) == 5, regs                     # bounded: exactly five tries
    assert w.first("systemd-run") is None           # refused BEFORE podman run
    assert w.launch_gone(TOKEN)
    # the bridge client+wrapper were started, recorded, then torn down
    assert bridge_call(w)[0] is not None
    left = [p for p in (w.T / "proc").iterdir() if p.name != "self"]
    assert left == [], f"bridge leftovers: {[p.name for p in left]}"
    assert w.launch_records() == []


def test_gui_launch_refuses_a_record_with_another_token(w):
    w.compositor()
    w.set("secctx_bad_token")
    r = w.spawn(argv=GUI_ARGV)
    assert r.returncode == 2 and "did not publish a live pid" in r.stderr, r.stderr
    assert w.first("systemd-run") is None
    assert w.launch_gone(TOKEN)
    left = [p for p in (w.T / "proc").iterdir() if p.name != "self"]
    assert left == [], f"bridge leftovers: {[p.name for p in left]}"


def test_gui_launch_refuses_a_record_with_a_dead_pid(w):
    w.compositor()
    w.set("secctx_dead_pid")
    r = w.spawn(argv=GUI_ARGV)
    assert r.returncode == 2 and "did not publish a live pid" in r.stderr, r.stderr
    assert w.first("systemd-run") is None and w.launch_gone(TOKEN)


def test_gui_launch_refuses_a_record_never_published(w):
    w.compositor()
    w.set("secctx_no_record")
    r = w.spawn(argv=GUI_ARGV)
    assert r.returncode == 2 and "did not publish a live pid" in r.stderr, r.stderr
    assert w.first("systemd-run") is None
    left = [p for p in (w.T / "proc").iterdir() if p.name != "self"]
    assert left == [], f"bridge leftovers: {[p.name for p in left]}"


def test_gui_launch_refuses_a_client_outside_the_launch_unit(w):
    w.compositor()
    w.set("waypipe_bad_cgroup")
    r = w.spawn(argv=GUI_ARGV)
    assert r.returncode == 2 and "is not in" in r.stderr and "cgroup" in r.stderr, r.stderr
    assert w.first("systemd-run") is None and w.launch_gone(TOKEN)


def test_gui_launch_refuses_when_the_socket_never_binds(w):
    w.compositor()
    w.set("waypipe_no_sock")
    r = w.spawn(argv=GUI_ARGV)
    assert r.returncode == 2 and "did not bind" in r.stderr, r.stderr
    assert w.first("systemd-run") is None and w.launch_gone(TOKEN)


def test_gui_launch_refuses_fast_when_the_client_dies_before_the_socket(w):
    """A dead client never binds: the wait must see the death and refuse at
    once — before podman/systemd-run — and the refusal carries the client's
    own log tail, not a bare 'no socket' (sol B-i r1 P2-7)."""
    w.compositor()
    w.set("waypipe_die")
    t0 = time.monotonic()
    r = w.spawn(argv=GUI_ARGV)
    elapsed = time.monotonic() - t0
    assert r.returncode == 2, r.stderr
    # the client dies ~1.5 s in; a wait that missed the death runs the whole
    # 5 s test bound before refusing
    assert elapsed < 4, f"waited out the clock bound on a dead client ({elapsed:.1f}s)"
    assert w.first("systemd-run") is None and w.launch_gone(TOKEN)
    # whichever refusal raced in, it names the client's captured output
    assert "client log tail" in r.stderr, r.stderr
    assert "dying early" in r.stderr, r.stderr


def test_headless_launch_never_touches_the_bridge(w):
    w.compositor()
    w.set("run_block")
    p = w.start()
    try:
        # run_argv exists only once the podman inside the scope has logged it
        # (systemd-run in the calls log alone is not that readiness signal)
        wait_for(lambda: (w.F / "run_argv").exists(), "podman launch", p)
        assert bridge_call(w)[0] is None
        assert w.first("waypipe") is None
        assert not [l for l in w.dbus_full() if "RegisterLaunch" in l]
        ra = (w.F / "run_argv").read_text()
        assert "host-uds" not in ra and "/run/qdistro/link" not in ra
        st = w.state()
        assert "gui" not in st and "bridge_client_pid" not in st
        w.set("release")
        p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0


@pytest.mark.parametrize("content", [
    "GUI=2\n",                       # a value other than 0|1
    "GUI=1\nGUI=0\n",                # duplicate assignment
    "# only a comment\n",            # no GUI declaration at all
    "GUI=1; rm -rf /\n",             # parsed, never sourced: trailing junk is malformed
    "GUI =1\n",                      # whitespace around the key
])
def test_malformed_workload_declaration_refuses(w, content):
    w.compositor()
    f = w.T / "usr/lib/qdistro/tier3s/workloads/foot.env"
    f.write_text(content)
    r = w.spawn(argv=GUI_ARGV)
    assert r.returncode == 2 and "declaration" in r.stderr, r.stderr
    assert w.first("dbus-send") is None and w.first("systemd-run") is None
    assert bridge_call(w)[0] is None


def test_absent_workload_declaration_takes_the_headless_path(w):
    """ΔB1: an ABSENT <workload>.env means GUI=0 — the Phase A headless
    path, unchanged: no compositor check, no bridge argv, no mount, no
    host-uds flag, and the app argv runs the image directly (the plan's
    GUI=0 records the same resolution the spawn acts on)."""
    w.compositor()
    (w.T / "usr/lib/qdistro/tier3s/workloads/foot.env").unlink()
    p = w.plan(argv=GUI_ARGV)
    assert p["GUI"] == "0" and p["bridge"] == []
    assert "--runtime-flag=host-uds=open" not in p["podman"]
    assert not any("/run/qdistro/link" in a for a in p["podman"])
    assert p["podman"][-2:] == ["localhost/qdistro/tier3s-foot:latest", "foot"]
    # ... and the spawn really walks it: a launch with no declaration, no
    # compositor and no bridge still reaches podman
    w.set("run_block")
    p = w.start(argv=GUI_ARGV)
    try:
        wait_for(lambda: w.first("systemd-run") is not None, "podman launch", p)
        assert bridge_call(w)[0] is None and w.first("waypipe") is None
        w.set("release")
        p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0


def test_a_symlinked_workload_declaration_refuses(w):
    w.compositor()
    f = w.T / "usr/lib/qdistro/tier3s/workloads/foot.env"
    target = w.T / "elsewhere.env"; target.write_text("GUI=1\n")
    f.unlink(); f.symlink_to(target)
    r = w.spawn(argv=GUI_ARGV)
    assert r.returncode == 2 and "declaration" in r.stderr, r.stderr
    assert w.first("systemd-run") is None


def test_workload_declarations_are_parsed_not_sourced(w):
    """A declaration is data: shell syntax in it must never execute."""
    w.compositor()
    f = w.T / "usr/lib/qdistro/tier3s/workloads/foot.env"
    f.write_text('GUI=1\n$(touch "$T/proof-sourced")\n')
    r = w.spawn(argv=GUI_ARGV)
    # the second line is malformed -> refusal; the marker proves nothing ran
    assert r.returncode == 2
    assert not list(w.T.glob("proof*")) and not (w.F / "proof-sourced").exists()


def test_gui_bridge_is_torn_down_when_the_launch_fails_after_it(w):
    """The bridge is up before podman; a podman-run failure still kills it."""
    w.compositor()
    w.set("run_rc", "3")
    p = w.start(argv=GUI_ARGV)
    try:
        wait_for(lambda: w.first("systemd-run") is not None, "podman launch", p)
        st = w.state()
        bpid = st["bridge_client_pid"]
        out, err = p.communicate(timeout=30)
        assert p.returncode != 0
        assert not (w.T / "proc" / bpid).exists(), "bridge client survived a failed launch"
        assert w.launch_gone(TOKEN)
    finally:
        if p.poll() is None:
            p.kill()
