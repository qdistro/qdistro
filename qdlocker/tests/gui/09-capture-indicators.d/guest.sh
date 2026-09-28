#!/bin/bash
# 09-capture-indicators.d/guest.sh — the GUEST driver of
# qdlocker/tests/gui/09-capture-indicators.md. Runs as ROOT inside the VM, as
# ONE shell from Setup through Cleanup. Started by run.sh (host), never by hand.
#
# WHY THIS FILE EXISTS. The scenario used to be written as host-side bash that
# called vm-exec once per command, and every GUI run had the agent re-write it
# into a single claimed guest driver plus a host loop serving its screenshots.
# Each run hand-translated ~40 guest commands and 9-12 host steps, and each
# failing run lost the verdict to a different translation slip, never to the
# product:
#   - full-20260928T154720Z: the host loop served a FIXED step list
#     (s1 s2 s3 s4 s7 ...) while the guest skipped the conditional step 4, so
#     the host polled for `s4` forever while the guest sat on `s7`, then on
#     `s8a`; recorded as a "transport" ERROR after step 7.
#   - full-20260928T111118Z: busctl calls with the interface argument dropped
#     ("Invalid interface name: CreateSilo"), `systemctl stop` of a SYSTEM unit
#     through `runuser -u admin` (polkit "Access denied") -> 8.2/8.3 FAIL.
#   - full-20260926T153217Z: cleanup skipped the Stopping fixture kill, the
#     next attempt's CreateSilo hit "silo already exists".
# So the commands live HERE, once, and the host side (run.sh) serves whatever
# step this driver publishes, by name, with no list of its own to drift.
#
# Host-step names are <id>-<action>. run.sh knows these actions and nothing
# else (tests/integration/qci/qdlocker-gui09-runner.bats pins the contract):
#   quiet   capture $ART/<id>.png; #FD4663 must be ABSENT from the banner band
#   alarm   capture $ART/<id>.png; #FD4663 must be PRESENT in the banner band
#   rec     capture $ART/<id>.png; recorded only, no pixel assertion
#   drain   qdlocker_drain_lock_state (unlock through the real keyboard path)
#   heads   two-head check of Step 10 (virsh --screen 0/1 histograms)
#
# Output: one `ASSERT <id> <PASS|FAIL|SKIP> <detail>` line per assertion, then
# `VERDICT <PASS|FAIL|ERROR>`. Exit 0 PASS, 1 FAIL, 3 ERROR. An ASSERT ERROR
# (a probe or the harness failed, so that assertion was not decided) makes
# the verdict ERROR even when other assertions FAILed: the run is incomplete.

# The lane installs the library at /tmp/qci-gui-waiters.sh. The override is
# honoured ONLY under the host-only bats harness's explicit flag, so a stray
# QCI_GUI_WAITERS in a guest environment can never swap in a no-op
# qci_host_step (run.sh also strips both from the driver's environment).
WAITERS=/tmp/qci-gui-waiters.sh
if [ "${QDLOCKER_09_TEST_HARNESS:-}" = 1 ]; then
    WAITERS=${QCI_GUI_WAITERS:?test harness must set QCI_GUI_WAITERS}
fi
# shellcheck source=/dev/null
source "$WAITERS" || exit 2
: "${QDLOCKER_09_DIR:?run.sh sets the /tmp/qci/<slug> step directory}"
qci_claim_driver "$QDLOCKER_09_DIR/driver.lock" || exit 2
set -u

SILO=${QDLOCKER_09_SILO:-qdlocker09}
SILO_UID=${QDLOCKER_09_SILO_UID:-3909}
SCR=$QDLOCKER_09_DIR/scratch
mkdir -p "$SCR" && chmod 0711 "$SCR" || exit 2
# Files an ADMIN process writes (the capture outputs) go in an admin-owned
# directory: the root-created $SCR is 0700 under qga's umask, and pw-record
# then dies "Permission denied" -- which read as a product FAIL of Step 2
# (gui-20260928T182835Z-1160277, first run of this driver).
ASCR=$SCR/admin
install -d -m 0700 -o admin -g users "$ASCR" || exit 2
BROKEN=/tmp/qdlocker-09-brokenbin
DROPIN_DIR=/home/admin/.config/systemd/user/qdlocker.service.d
BREAK_DROPIN=$DROPIN_DIR/91-break-pwdump.conf
FIXTURE_PID=/tmp/qdlocker-09-stop-fixture.pid
FAILS=0
ERRORS=0
# This scenario's own session-manager stop (Step 8). The marker holds the
# InvocationID of the manager invocation THIS scenario stopped (an enabled
# unit keeps its last InvocationID while inactive; any later start replaces
# it). Setup repairs the manager ONLY when it is still inactive/failed with
# that same InvocationID -- nobody has run it since -- and reports any other
# stopped manager, never repairing it. See sm_read / sm_try_start.
SM_MARK=$SCR/sm-stopped-by-09
SM_UNIT=qdistro-session-manager.service

# ------------------------------------------------------------------ helpers
# admin's user session (qdlocker.service, PipeWire) needs XDG_RUNTIME_DIR.
U() { runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 "$@"; }
# The ctrl socket is SO_PEERCRED-gated to admin; root is refused.
ctrl() {
    printf '%s\n' "$1" \
        | runuser -u admin -- socat -t 1 - UNIX-CONNECT:/run/user/1000/qdlocker.sock 2>/dev/null \
        | tr -d '\r'
}
ind() { ctrl indicators; }
# All four arguments of a SessionManager1 call, every time: the object path,
# the interface AND the method (dropping the interface is the 09-28 slip).
sm() {
    runuser -u admin -- busctl --system call \
        org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
        org.qdistro.SessionManager1 "$@"
}
field() { printf '%s\n' "$2" | tr ' ' '\n' | sed -n "s/^$1=//p" | head -1; }

record() {
    printf 'ASSERT %s %s %s\n' "$1" "$2" "$3"
    [ "$2" != FAIL ] || FAILS=$((FAILS + 1))
    [ "$2" != ERROR ] || ERRORS=$((ERRORS + 1))
}
# assert_ind <id> <key> <want> — exact match on one key=value token.
assert_ind() {
    local id=$1 key=$2 want=$3 line got
    line=$(ind); got=$(field "$key" "$line")
    if [ "$got" = "$want" ]; then
        record "$id" PASS "$key=$got"
    else
        record "$id" FAIL "$key=${got:-<missing>} (want $want) :: $line"
    fi
}
# assert_ind_contains <id> <key> <substring>
assert_ind_contains() {
    local id=$1 key=$2 want=$3 line got
    line=$(ind); got=$(field "$key" "$line")
    case "$got" in
        *"$want"*) record "$id" PASS "$key=$got contains $want" ;;
        *) record "$id" FAIL "$key=${got:-<missing>} does not contain $want :: $line" ;;
    esac
}

# wait_status <secs>: the ctrl socket answers `status` (it only listens once the
# QML root window exists — qdlocker AGENTS.md pitfall 7).
wait_status() {
    local end=$((SECONDS + ${1:-15}))
    while [ "$SECONDS" -lt "$end" ]; do
        case "$(ctrl status)" in *locked=*) return 0 ;; esac
        sleep 0.2
    done
    return 1
}
wait_lock() {
    local end=$((SECONDS + ${1:-5}))
    while [ "$SECONDS" -lt "$end" ]; do
        case "$(ctrl status)" in *locked=True*) return 0 ;; esac
        sleep 0.2
    done
    return 1
}
lock_now() {
    ctrl lock >/dev/null
    wait_lock 5 || { record "$1" FAIL "qdlocker did not reach locked=True within 5s :: $(ctrl status)"; return 1; }
}
restart_locker() {
    U systemctl --user restart qdlocker.service
    wait_status 20 || { error "qdlocker ctrl socket did not answer status 20s after a restart"; }
}

host() { qci_host_step "$1"; }

# ---- this scenario's own recorders -------------------------------------
# Each recorder is started through start_recorder, which records
# "<pid> <starttime> <comm>" in the root-owned $SCR/rec-<tag>. Only a
# process matching all three is ever signalled: another admin application's
# pw-record/parec/gst-launch-1.0 is never killed (that would silently turn a
# real capture into Step 1's "quiet" baseline); Setup REPORTS it instead.
# proc_start <pid>: field 22 of /proc/<pid>/stat (unchanged across exec).
proc_start() {
    local st
    { read -r st <"/proc/$1/stat"; } 2>/dev/null || return 1
    set -f
    # shellcheck disable=SC2086
    set -- ${st##*") "}
    set +f
    [ -n "${20:-}" ] || return 1
    printf '%s' "${20}"
}
# start_recorder <tag> <comm> <log> <cmd...>: run cmd as admin, detached,
# stdio on <log> (opened by this root shell), and record its identity. The
# admin sh writes its own pid and then execs the recorder, so that pid IS
# the recorder's.
start_recorder() {
    local tag=$1 comm=$2 log=$3 pidf pid st
    shift 3
    pidf=$ASCR/$tag.pid
    rm -f "$pidf" "$SCR/rec-$tag"
    # shellcheck disable=SC2016
    U setsid sh -c 'echo $$ >"$0"; exec "$@"' "$pidf" "$@" >"$log" 2>&1 </dev/null &
    for _ in $(seq 1 50); do [ -s "$pidf" ] && break; sleep 0.1; done
    pid=$(cat "$pidf" 2>/dev/null) || return 1
    case $pid in ''|*[!0-9]*) return 1 ;; esac
    st=$(proc_start "$pid") || return 1
    printf '%s %s %s\n' "$pid" "$st" "$comm" >"$SCR/rec-$tag"
}
# tracked_alive <tag>: the recorded process is still that same process.
tracked_alive() {
    local pid st comm cur
    read -r pid st comm <"$SCR/rec-$1" 2>/dev/null || return 1
    cur=$(proc_start "$pid") || return 1
    [ "$cur" = "$st" ] && [ "$(cat "/proc/$pid/comm" 2>/dev/null)" = "$comm" ]
}
# stop_recorder <tag>: TERM (then KILL) the recorded process if, and only
# if, it is still the one this scenario started; then forget the record.
stop_recorder() {
    local pid st comm
    if tracked_alive "$1"; then
        read -r pid st comm <"$SCR/rec-$1"
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq 1 30); do tracked_alive "$1" || break; sleep 0.1; done
        ! tracked_alive "$1" || kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$SCR/rec-$1"
}
# foreign_captures: admin recorders this scenario did not start, one entry
# each. Setup refuses to run over them.
foreign_captures() {
    local name pid
    for name in pw-record parec gst-launch-1.0; do
        for pid in $(pgrep -u admin -x "$name" 2>/dev/null); do
            printf '%s(pid %s: %s) ' "$name" "$pid" \
                "$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | cut -c1-120)"
        done
    done
}

# ---- session manager provenance ------------------------------------------
# sm_read: load SM_LOAD / SM_ACTIVE / SM_INV from ONE systemctl query (a
# SYSTEM unit: root's systemctl, never through runuser -- polkit refuses).
# rc != 0 when the query failed. Note that `show` of an unknown unit still
# answers ActiveState=inactive, so LoadState=loaded is always required too.
sm_read() {
    local l out
    SM_LOAD="" SM_ACTIVE="" SM_INV=""
    out=$(systemctl show -p LoadState -p ActiveState -p InvocationID "$SM_UNIT" 2>/dev/null) || return 1
    while IFS= read -r l; do
        case $l in
            LoadState=*) SM_LOAD=${l#*=} ;;
            ActiveState=*) SM_ACTIVE=${l#*=} ;;
            InvocationID=*) SM_INV=${l#*=} ;;
        esac
    done <<<"$out"
    [ -n "$SM_LOAD" ]
}
# sm_stop_verified: stop the manager and write the marker ONLY for a stop
# this scenario provably performed: a successful query shows it loaded and
# ACTIVE with an InvocationID immediately before (a manager that already
# exited on its own is NOT stopped -- nothing to claim), the stop succeeds,
# and a successful query afterwards shows it loaded, inactive, with that
# SAME InvocationID. rc 1 and no marker otherwise; SM_PRE_INV is the
# pre-stop invocation for the report.
sm_stop_verified() {
    rm -f "$SM_MARK"
    SM_PRE_INV=""
    sm_read || return 1
    [ "$SM_LOAD" = loaded ] && [ "$SM_ACTIVE" = active ] && [ -n "$SM_INV" ] || return 1
    SM_PRE_INV=$SM_INV
    systemctl stop "$SM_UNIT" 2>/dev/null || { sm_read || true; return 1; }
    sm_read || return 1
    [ "$SM_LOAD" = loaded ] && [ "$SM_ACTIVE" = inactive ] && [ "$SM_INV" = "$SM_PRE_INV" ] || return 1
    printf '%s\n' "$SM_INV" >"$SM_MARK"
}
# sm_try_start: restart a manager this scenario stopped. The marker is
# removed only once the start succeeded AND the manager reads active. A
# failed start is still OUR pending recovery: the marker is rewritten with
# that failed attempt's InvocationID, so the next Setup can retry it (and
# only it).
sm_try_start() {
    if systemctl start "$SM_UNIT" 2>/dev/null && sm_read && [ "$SM_ACTIVE" = active ]; then
        rm -f "$SM_MARK"
        return 0
    fi
    if sm_read && [ "$SM_LOAD" = loaded ] && [ -n "$SM_INV" ]; then
        printf '%s\n' "$SM_INV" >"$SM_MARK"
    fi
    return 1
}
# sm_reclaim: Setup/cleanup side of the marker. The marker is discarded only
# on PROOF from a successful query: the manager is active (a recovery
# finished; the driver died before removing it), or it is loaded with a
# DIFFERENT InvocationID (someone ran it since: not ours). Inactive/failed
# with the marked InvocationID -> our own pending stop: retry the start.
# A failed query, or anything else unproven, KEEPS the marker (Setup then
# reports ERROR for this attempt; a later one can still reclaim).
sm_reclaim() {
    local mark=""
    [ -e "$SM_MARK" ] || return 0
    read -r mark <"$SM_MARK" 2>/dev/null || mark=""
    sm_read || return 0
    if [ "$SM_ACTIVE" = active ]; then
        rm -f "$SM_MARK"
    elif [ "$SM_LOAD" = loaded ] && [ -n "$SM_INV" ] && [ "$SM_INV" != "$mark" ]; then
        rm -f "$SM_MARK"
    elif [ "$SM_LOAD" = loaded ] && [ -n "$mark" ] && [ "$SM_INV" = "$mark" ] \
         && { [ "$SM_ACTIVE" = inactive ] || [ "$SM_ACTIVE" = failed ]; }; then
        sm_try_start || true
    fi
}

# reset_state: undo everything a run of this driver can leave behind that
# would change what the NEXT run observes. Idempotent; safe on a clean VM.
# Called by guest_cleanup AND at the top of Setup: a qci_host_step timeout
# stops the driver WITHOUT its EXIT trap (by design, so the frame's state
# stays inspectable), so a rerun must not assume the previous run cleaned
# up. Left alone, a timeout at s2-alarm leaves pw-record capturing (the next
# Step 1 reads capture_active=1, a false product FAIL), and a timeout at
# s8b-alarm leaves qdistro-session-manager stopped (the next Setup dies at
# ListSilos/CreateSilo). Echoes 1 if the pw-dump break drop-in was present.
reset_state() {
    local rec
    for rec in "$SCR"/rec-*; do
        [ -e "$rec" ] || continue
        stop_recorder "${rec##*/rec-}"
    done
    if [ -s "$FIXTURE_PID" ]; then
        local fp
        fp=$(cat "$FIXTURE_PID")
        if grep -Fq "/qdistro-silos/$SILO" "/proc/$fp/cgroup" 2>/dev/null; then
            kill -KILL "$fp" 2>/dev/null || true
        fi
    fi
    rm -f "$FIXTURE_PID" /tmp/qdlocker-09-stop-fixture.log
    local was_broken=0
    [ -e "$BREAK_DROPIN" ] && was_broken=1
    rm -f "$BREAK_DROPIN"
    rm -rf "$BROKEN"
    # The session manager: restarted only if it provably is THIS scenario's
    # own pending stop (sm_reclaim).
    sm_reclaim
    echo "$was_broken"
}

guest_cleanup() {
    local was_broken
    was_broken=$(reset_state)
    U systemctl --user daemon-reload || true
    # A driver that died with the pw-dump break in place must not leave the
    # locker running with it.
    [ "$was_broken" = 0 ] || U systemctl --user restart qdlocker.service || true
    sm SetSiloEgress ss "$SILO" none >/dev/null 2>&1 || true
    sm StopSilo si "$SILO" 0 >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do
        sm DeleteSilo s "$SILO" >/dev/null 2>&1 && break
        sleep 1
    done
}

error() {
    printf 'ERROR: %s\n' "$*"
    trap - EXIT
    guest_cleanup
    # The verdict is printed before the last host step, so a timeout on the
    # drain cannot lose it.
    echo "VERDICT ERROR ($ERRORS undecided assertion(s); $FAILS failed)"
    host cleanup-drain
    exit 3
}

# ---- probes ---------------------------------------------------------------
# A probe prints its value and returns 0 when it RAN and was parsed (the value
# may legitimately be empty/zero), 2 when the probe itself failed (non-zero
# exit, unparsable output), 3 when its tool is not installed. Only a
# successful empty result may SKIP a conditional step; a failure is ERROR.
# have <tool>: the tool is installed. Under the host-only bats harness (and
# only there) QDLOCKER_09_HIDE_TOOLS lists tools to treat as missing.
have() {
    if [ "${QDLOCKER_09_TEST_HARNESS:-}" = 1 ]; then
        case " ${QDLOCKER_09_HIDE_TOOLS:-} " in *" $1 "*) return 1 ;; esac
    fi
    command -v "$1" >/dev/null
}
compositor_journal() {
    runuser -l admin -c "journalctl --user -u qdwin-compositor.service --boot --no-pager" 2>"$SCR/journal.err"
}
# probe_default_sink: the node.name of the default Audio/Sink, from pw-dump
# (pipewire-tools, which the image ships -- pactl/parec never were: the old
# pactl probe "found no sink" on every VM because pactl did not exist, and
# Step 4 had never run). The default is the `default` metadata's
# default.audio.sink; with no default set, the first Audio/Sink by name.
# rc 0 + empty = pw-dump succeeded and there is no Audio/Sink; rc 2 = pw-dump
# failed or its output is not a pw-dump object list; rc 3 = no pw-dump.
probe_default_sink() {
    local out
    have pw-dump || return 3
    out=$(cd / && U python3 -I - 2>"$SCR/sink-probe.err" <<'PYEOF'
import json, subprocess, sys
r = subprocess.run(["pw-dump"], capture_output=True, text=True)
if r.returncode != 0:
    sys.stderr.write("pw-dump failed rc=%d: %s\n" % (r.returncode, r.stderr[-300:]))
    sys.exit(2)
try:
    objs = json.loads(r.stdout)
except ValueError as e:
    sys.stderr.write("pw-dump output is not JSON: %s\n" % e)
    sys.exit(2)
if not isinstance(objs, list) or not objs:
    sys.stderr.write("pw-dump returned no objects\n")
    sys.exit(2)
sinks, default = set(), None
for o in objs:
    if not isinstance(o, dict):
        continue
    t = str(o.get("type") or "")
    if "Interface:Node" in t:
        info = o.get("info") if isinstance(o.get("info"), dict) else {}
        props = info.get("props") if isinstance(info.get("props"), dict) else {}
        if str(props.get("media.class") or "") == "Audio/Sink" and props.get("node.name"):
            sinks.add(str(props["node.name"]))
    elif "Interface:Metadata" in t:
        mprops = o.get("props") if isinstance(o.get("props"), dict) else {}
        if str(mprops.get("metadata.name") or "") != "default":
            continue
        for m in o.get("metadata") or []:
            if isinstance(m, dict) and m.get("key") == "default.audio.sink" \
                    and isinstance(m.get("value"), dict):
                default = m["value"].get("name")
if sinks:
    print(default if default in sinks else sorted(sinks)[0])
PYEOF
) || return 2
    printf '%s' "${out//$'\r'/}"
}
# probe_camera: the node.name of a Video/Source node, via the installed parser.
probe_camera() {
    local out
    out=$(cd / && U python3 -I - 2>"$SCR/cam-probe.err" <<'PYEOF'
import subprocess, sys
from qdlocker import indicators as I
r = subprocess.run(I.CAPTURE_CMD, capture_output=True, text=True)
if r.returncode != 0:
    sys.stderr.write("capture command failed rc=%d: %s\n" % (r.returncode, r.stderr[-300:]))
    sys.exit(2)
ok, nodes = I.parse_pw_dump(r.stdout)
if not ok:
    sys.stderr.write("parse_pw_dump rejected the pw-dump output\n")
    sys.exit(2)
for n in nodes:
    p = n["props"]
    if str(p.get("media.class", "")) == "Video/Source":
        print(p.get("node.name", "")); break
PYEOF
) || return 2
    printf '%s' "${out//$'\r'/}"
}
# probe_node_id <node.name>: pw-cli's id for a node pw-dump just reported;
# not finding it is a probe failure, not an absence.
probe_node_id() {
    local out id
    out=$(U pw-cli ls Node 2>"$SCR/pw-cli.err") || return 2
    id=$(printf '%s\n' "$out" | awk -v n="node.name = \"$1\"" '/id /{id=$2} index($0,n){print id; exit}' | tr -d ',')
    case $id in ''|*[!0-9]*) return 2 ;; esac
    printf '%s' "$id"
}
count_pw_nodes() {
    local out
    out=$(U pw-cli ls Node 2>"$SCR/pw-cli.err") || return 2
    printf '%s\n' "$out" | grep -c 'weston\.pipewire' || true
}
# count_drm_outputs: DRM heads the CURRENT compositor invocation has enabled
# and not since disabled. libweston's DRM backend logs
# "Output <name> (crtc <n>) video modes:" when it enables an output
# (backend-drm/drm.c drm_output_enable) and "Disabling output <name>" when it
# disables one; the journal keeps both, and the lines of every earlier
# compositor invocation of this boot. The query is scoped to this
# invocation's journal entries by _SYSTEMD_INVOCATION_ID (the compositor is
# admin's USER unit, whose own entries carry that field in the user
# journal), NOT by a start timestamp: a whole-second `--since` also takes in
# an earlier invocation that crashed in the same second. Enable/disable
# lines are then replayed in order. (The first probe grepped for
# `output_created ... name=`, which qdwin never logs: it read 0 on every VM
# and Step 10 could never run.) A missing InvocationID, a failed query, or
# no enabled output at all is a probe failure.
count_drm_outputs() {
    local inv out n
    inv=$(runuser -l admin -c "systemctl --user show -p InvocationID --value qdwin-compositor.service" 2>"$SCR/journal.err") || return 2
    inv=${inv//$'\r'/}
    if ! [[ $inv =~ ^[0-9a-f]{32}$ ]]; then
        echo "no InvocationID for qdwin-compositor.service: '$inv'" >>"$SCR/journal.err"
        return 2
    fi
    out=$(runuser -l admin -c "journalctl --user _SYSTEMD_INVOCATION_ID=$inv --no-pager -o cat" 2>>"$SCR/journal.err") || return 2
    n=$(printf '%s\n' "$out" | awk '
        match($0, /Output [A-Za-z0-9-]+ \(crtc [0-9]+\) video modes:/) {
            split(substr($0, RSTART, RLENGTH), f, " "); on[f[2]] = 1
        }
        match($0, /Disabling output [A-Za-z0-9-]+/) {
            split(substr($0, RSTART, RLENGTH), f, " "); delete on[f[3]]
        }
        END { c = 0; for (k in on) c++; print c }')
    [ "$n" -ge 1 ] || return 2
    printf '%s' "$n"
}

# ------------------------------------------------------------------ Setup
echo "== Setup"
# qdlocker_session_healthy, guest side: introspection marker + GUI-lane prep.
f=/etc/qdistro/locker-ctrl-introspection
if [ ! -f "$f" ]; then
    install -d -m 0755 -o 0 -g 0 /etc/qdistro
    : > "$f"; chown 0:0 "$f"; chmod 0644 "$f"
fi
faillock --user admin --reset 2>/dev/null || true
install -d -m 0755 -o admin -g users "$DROPIN_DIR"
printf '[Service]\nEnvironment=QDLOCKER_IDLE_MS=86400000\n' > "$DROPIN_DIR/90-ci-gui.conf"
chown admin:users "$DROPIN_DIR/90-ci-gui.conf"
# Reclaim whatever an earlier attempt of THIS scenario left (see
# reset_state) BEFORE the locker restart below and before any silo call:
# its own recorders, the Stopping fixture, the Step 7 pw-dump break, and the
# session manager when its own Step 8 marker says it stopped it. Anything
# else is reported, not repaired.
reset_state >/dev/null
FOREIGN=$(foreign_captures)
[ -z "$FOREIGN" ] || error "foreign capture active before Step 1 (not started by this scenario; left running): $FOREIGN"
if ! sm_read; then
    error "cannot query qdistro-session-manager.service state at Setup$([ -e "$SM_MARK" ] && echo "; this scenario's marker is kept for the next attempt")"
fi
if [ "$SM_ACTIVE" != active ]; then
    if [ -e "$SM_MARK" ]; then
        error "qdistro-session-manager.service is '${SM_ACTIVE:-unknown}' at Setup: this scenario's own earlier stop could not be recovered (invocation ${SM_INV:-none}; marker kept for the next attempt)"
    fi
    error "qdistro-session-manager.service is '${SM_ACTIVE:-unknown}' at Setup and this scenario did not stop it (load ${SM_LOAD:-unknown}); not repairing it"
fi
U systemctl --user daemon-reload
restart_locker
for unit in qdwin-compositor.service qdlocker.service; do
    st=$(runuser -l admin -c "systemctl --user is-active $unit" 2>/dev/null | tr -d '\r\n')
    [ "$st" = active ] || error "session not up: $unit is '$st' (want active)"
done
case "$(ctrl status)" in
    *locked=*) ;;
    *) error "ctrl 'status' unavailable — introspection not enabled" ;;
esac
trap guest_cleanup EXIT
host setup-drain
case "$(ctrl status)" in
    *locked=False*) ;;
    *) error "lock state did not drain before Step 1 :: $(ctrl status)" ;;
esac

# Reclaim THIS scenario's own fixture silo if an earlier attempt left it
# (full-20260926T153217Z: TERM-ignoring fixture kept the cgroup busy, DeleteSilo
# hit EBUSY, the rerun's CreateSilo failed "silo already exists").
CG=/sys/fs/cgroup/qdistro-silos/$SILO
if sm ListSilos 2>/dev/null | grep -Fq "$SILO" || [ -d "$CG" ]; then
    echo "setup: reclaiming leftover fixture silo $SILO"
    [ -f "$CG/cgroup.procs" ] && xargs -r kill -KILL < "$CG/cgroup.procs" 2>/dev/null || true
    rm -f "$FIXTURE_PID" /tmp/qdlocker-09-stop-fixture.log
    sm StopSilo si "$SILO" 0 >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do
        sm DeleteSilo s "$SILO" >/dev/null 2>&1 && break
        sleep 1
    done
fi
sm CreateSilo si "$SILO" "$SILO_UID" || error "CreateSilo $SILO failed"
sm SetSiloEgress ss "$SILO" none || error "SetSiloEgress $SILO none failed"

# ------------------------------------------------------------------ Preflight A
echo "== Preflight A"
CJ=$(compositor_journal) || error "compositor journal query failed: $(tail -2 "$SCR/journal.err" | tr '\n' ' ')"
DRM_FAILS=$(printf '%s\n' "$CJ" \
    | grep -cE "atomic: couldn't commit new state: Invalid argument|repaint-flush failed: Invalid argument" || true)
if [ "${DRM_FAILS:-0}" -ge 5 ]; then
    error "VM graphics backend is failing DRM atomic commits ($DRM_FAILS); pixel checks would be false FAILs"
fi
echo "ok: DRM atomic-commit failures=$DRM_FAILS"

# ------------------------------------------------------------------ Preflight B
echo "== Preflight B"
# python3 -I ignores PYTHONPATH and user site-packages; `cd /` drops the cwd.
MODPATH=$(cd / && runuser -u admin -- python3 -I -c "import qdlocker.indicators as m; print(m.__file__)" 2>/dev/null | tr -d '\r')
echo "installed module: $MODPATH"
case "$MODPATH" in
    /usr/lib*/python3*/site-packages/qdlocker/indicators.py|/usr/lib/qdistro/*|/usr/local/lib*/python3*/*/qdlocker/indicators.py)
        record B.1 PASS "module resolves under an installed prefix: $MODPATH" ;;
    "") record B.1 FAIL "qdlocker.indicators does not import in isolated mode — the wheel does not ship it" ;;
    *) record B.1 FAIL "module resolves to $MODPATH, not an installed prefix" ;;
esac
if command -v pw-dump >/dev/null && command -v busctl >/dev/null; then
    record B.2 PASS "pw-dump=$(command -v pw-dump) busctl=$(command -v busctl)"
else
    record B.2 FAIL "pw-dump or busctl missing from the image"
fi
case "$(ind)" in
    *capture_observer=*) record B.3 PASS "indicators verb answers" ;;
    *) error "ctrl 'indicators' unavailable — the gate is unfalsifiable (BLOCKED)" ;;
esac
if [ "$FAILS" -gt 0 ]; then
    error "Preflight B failed; the steps below would not test the installed observer"
fi

# ------------------------------------------------------------------ Step 1
echo "== Step 1 — locked, nothing capturing"
lock_now 1.0
sleep 2            # the lock edge forces an immediate scan; one poll is 3s
assert_ind 1.1 capture_observer ok
assert_ind 1.1 capture_active 0
assert_ind 1.1 egress_observer ok
host s1-quiet      # 1.2: no #FD4663 in the banner band
assert_ind 1.3 capture_unverified microphone,camera,screencast,systemAudio,virtualInput,unattributed

# ------------------------------------------------------------------ Step 2
echo "== Step 2 — microphone capture starts while locked"
start_recorder mic pw-record "$SCR/mic.log" pw-record --target=@DEFAULT_SOURCE@ "$ASCR/mic.wav" \
    || error "could not start pw-record: $(tail -3 "$SCR/mic.log" | tr '\n' ' ')"
sleep 6            # two polls; deliberately NO lock cycle
# The capture must actually be running, or 2.1 would grade the harness, not
# the observer (a recorder that exited is ERROR, never a product FAIL).
tracked_alive mic \
    || error "pw-record is not running 6s after start: $(tail -3 "$SCR/mic.log" | tr '\n' ' ')"
assert_ind 2.1 capture_active 1
assert_ind_contains 2.1 capture_kinds microphone
assert_ind_contains 2.1 capture_detail mic
host s2-alarm      # 2.2: the banner turns alarming
LINE=$(ind)
ATTR=$(field capture_attributed "$LINE")
echo "capture_attributed=$ATTR"
if [ "$ATTR" = 0 ]; then
    assert_ind_contains 2.3 capture_detail client_unknown
elif [ "$ATTR" = 1 ]; then
    case "$LINE" in
        *client_unknown*) record 2.3 FAIL "capture_attributed=1 but the detail says client unknown :: $LINE" ;;
        *) record 2.3 PASS "capture_attributed=1 and the detail names a client" ;;
    esac
else
    record 2.3 FAIL "capture_attributed=${ATTR:-<missing>} :: $LINE"
fi

# ------------------------------------------------------------------ Step 3
echo "== Step 3 — the capture stops while locked"
stop_recorder mic
sleep 7
assert_ind 3.1 capture_active 0
assert_ind 3.1 capture_observer ok
host s3-quiet

# ------------------------------------------------------------------ Step 4
echo "== Step 4 — system-audio (sink-monitor) capture (CONDITIONAL)"
# The capture is a pw-record stream on the default sink with
# stream.capture.sink=true: a Stream/Input/Audio node carrying that
# property, which qdlocker.indicators.classify_node reports as systemAudio
# (and, without it, as microphone). Same tool family as Step 2's microphone.
if ! have pw-record; then
    record 4 ERROR "pw-record is not installed (command -v pw-record); system-audio capture undecided"
else
    SINK=$(probe_default_sink); rc=$?
    if [ "$rc" = 3 ]; then
        record 4 ERROR "pw-dump is not installed, so the default-sink probe cannot run; system-audio capture undecided"
    elif [ "$rc" != 0 ]; then
        record 4 ERROR "default-sink probe failed (rc=$rc): $(tail -2 "$SCR/sink-probe.err" | tr '\n' ' ')"
    elif [ -z "$SINK" ]; then
        record 4 SKIP "no Audio/Sink node (pw-dump succeeded); system-audio capture not exercised"
    else
        echo "default sink: $SINK"
        start_recorder sysaudio pw-record "$SCR/sysaudio.log" \
            pw-record --target "$SINK" -P '{ stream.capture.sink = true }' "$ASCR/sysaudio.wav" \
            || error "could not start the sink-monitor pw-record: $(tail -3 "$SCR/sysaudio.log" | tr '\n' ' ')"
        sleep 6
        tracked_alive sysaudio \
            || error "sink-monitor pw-record is not running 6s after start: $(tail -3 "$SCR/sysaudio.log" | tr '\n' ' ')"
        assert_ind 4.1 capture_active 1
        assert_ind_contains 4.1 capture_kinds systemAudio
        if field capture_kinds "$(ind)" | grep -q microphone; then
            record 4.1 FAIL "sink-monitor capture also reported as microphone :: $(ind)"
        else
            record 4.1 PASS "monitor capture not reported as microphone"
        fi
        host s4-alarm
        stop_recorder sysaudio
        sleep 7
        assert_ind 4.2 capture_active 0
    fi
fi

# ------------------------------------------------------------------ Step 5
echo "== Step 5 — camera (CONDITIONAL)"
CAMID=$(probe_camera); rc=$?
if [ "$rc" != 0 ]; then
    record 5 ERROR "camera probe failed (pw-dump / parse_pw_dump): $(tail -2 "$SCR/cam-probe.err" | tr '\n' ' ')"
elif [ -z "$CAMID" ]; then
    record 5 SKIP "no PipeWire Video/Source node in this VM (no camera, no v4l2loopback)"
elif ! have gst-launch-1.0; then
    # A camera IS present; only the driving tool is missing: undecided.
    record 5 ERROR "camera node $CAMID is present but gst-launch-1.0 is not installed; camera classification undecided"
elif ! CAMNODE=$(probe_node_id "$CAMID"); then
    record 5 ERROR "pw-dump reported camera node $CAMID but pw-cli could not resolve its id: $(tail -2 "$SCR/pw-cli.err" | tr '\n' ' ')"
else
    start_recorder cam gst-launch-1.0 "$SCR/cam.log" gst-launch-1.0 -q pipewiresrc path="$CAMNODE" ! fakesink \
        || record 5 ERROR "could not start gst-launch-1.0: $(tail -3 "$SCR/cam.log" | tr '\n' ' ')"
    sleep 6
    if ! tracked_alive cam; then
        # The dependency is present and the node resolved, so a pipeline that
        # did not stay up is not an absence.
        record 5 ERROR "gst-launch-1.0 exited immediately (node $CAMID id=$CAMNODE): $(tail -3 "$SCR/cam.log" | tr '\n' ' ')"
    else
        assert_ind 5.1 capture_active 1
        assert_ind_contains 5.1 capture_kinds camera
        host s5-alarm
    fi
    stop_recorder cam
    sleep 7
fi

# ------------------------------------------------------------------ Step 6
echo "== Step 6 — screencast via a qdwin view stream (CONDITIONAL, MANUAL)"
# Nothing in this driver creates a view stream (the scenario does not
# reimplement a Wayland client). The node set is sampled around a 5 s window;
# only a NEW weston.pipewire node lets the step assert, otherwise it SKIPs.
NODES_BEFORE=$(count_pw_nodes); rc1=$?
echo "RUNNER (manual): a qdwin view stream started now would be observed (nodes before: $NODES_BEFORE)"
sleep 5
NODES_AFTER=$(count_pw_nodes); rc2=$?
if [ "$rc1" != 0 ] || [ "$rc2" != 0 ]; then
    record 6 ERROR "pw-cli ls Node failed: $(tail -2 "$SCR/pw-cli.err" | tr '\n' ' ')"
elif [ "$NODES_AFTER" -le "$NODES_BEFORE" ]; then
    record 6 SKIP "no new weston.pipewire node appeared ($NODES_BEFORE -> $NODES_AFTER); no view stream was started"
else
    sleep 4
    assert_ind_contains 6.1 capture_kinds screencast
    host s6-alarm
fi

# ------------------------------------------------------------------ Step 7
echo "== Step 7 — observer failure must be visible"
# 7.3 must be proven by THIS step's hang: a journal cursor taken before the
# injection bounds the search (an earlier run's line in the same boot must
# not count). A cursor is "s=..;i=..;b=..;m=..;t=..;x=..".
C7=$(runuser -l admin -c "journalctl --user -u qdlocker.service -n1 --show-cursor --no-pager -o cat" 2>/dev/null \
    | sed -n 's/^-- cursor: //p' | tail -1)
install -d -m 0755 "$BROKEN"
printf '#!/bin/sh\nsleep 300\n' > "$BROKEN/pw-dump"
chmod 0755 "$BROKEN/pw-dump"
install -d -m 0755 "$DROPIN_DIR"
printf '[Service]\nEnvironment=PATH=%s:/usr/local/bin:/usr/bin:/bin\n' "$BROKEN" > "$BREAK_DROPIN"
chown -R admin:users "$DROPIN_DIR"
U systemctl --user daemon-reload
restart_locker
lock_now 7.0
sleep 6            # past the 2.5s scan timeout; the kill publishes immediately
assert_ind 7.1 capture_observer failed
assert_ind 7.1 capture_active 0
host s7-alarm      # 7.2: the banner is ALARMING
# 7.3: qdlocker.service is admin's USER unit; root's `journalctl --user` would
# read root's own journal and prove nothing.
if ! [[ $C7 =~ ^[A-Za-z0-9=\;_-]+$ ]]; then
    record 7.3 ERROR "no journal cursor for qdlocker.service before the injected hang ('$C7'); 7.3 undecided"
elif ! J7=$(runuser -l admin -c "journalctl --user -u qdlocker.service --after-cursor='$C7' --no-pager -o cat" 2>/dev/null); then
    record 7.3 ERROR "qdlocker.service journal query after the Step 7 cursor failed; 7.3 undecided"
elif printf '%s\n' "$J7" | grep -q "observer timed out; killing scan"; then
    record 7.3 PASS "journal (after the Step 7 cursor): observer timed out; killing scan"
else
    record 7.3 FAIL "the hard timeout did not fire (no 'observer timed out; killing scan' in qdlocker.service's journal after the Step 7 cursor)"
fi
# Recovery (7.4).
rm -f "$BREAK_DROPIN"
rm -rf "$BROKEN"
U systemctl --user daemon-reload
restart_locker
lock_now 7.4
sleep 3
assert_ind 7.4 capture_observer ok

# ------------------------------------------------------------------ Step 8
echo "== Step 8 — silo egress, Stopping, unreachable manager"
sm SetSiloEgress ss "$SILO" direct || record 8.0 FAIL "SetSiloEgress $SILO direct failed"
sm StartSilo s "$SILO" || record 8.0 FAIL "StartSilo $SILO failed"
sleep 4
assert_ind 8.1 egress_active 1
assert_ind_contains 8.1 egress_detail "$SILO"
host s8a-rec
# A process that ignores SIGTERM, in the silo's production cgroup, keeps
# StopSilo's 30 s grace window real and observable. Its stdio is a file: a
# background holder of the driver's stdout would keep vm-exec from returning.
setsid sh -c 'trap "" TERM; while :; do sleep 1; done' \
    >/tmp/qdlocker-09-stop-fixture.log 2>&1 </dev/null &
fixture_pid=$!
echo "$fixture_pid" > "/sys/fs/cgroup/qdistro-silos/$SILO/cgroup.procs" 2>/dev/null
echo "$fixture_pid" > "$FIXTURE_PID"
if grep -qx "$fixture_pid" "/sys/fs/cgroup/qdistro-silos/$SILO/cgroup.procs" 2>/dev/null; then
    echo "ok: Stopping-state workload $fixture_pid planted in $SILO"
else
    error "could not plant the Stopping-state workload in /sys/fs/cgroup/qdistro-silos/$SILO"
fi
setsid runuser -u admin -- busctl --system call \
    org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
    org.qdistro.SessionManager1 StopSilo si "$SILO" 30 >/dev/null 2>&1 </dev/null &
# Correlate: the TARGET silo is Stopping in the same window the indicator is
# sampled, read through the production parser (the installed module).
if (cd / && runuser -u admin -- python3 -I - "$SILO" <<'PYEOF'
import subprocess, sys, time
from qdlocker import indicators as I
silo = sys.argv[1]
deadline = time.monotonic() + 10
state = None
while time.monotonic() < deadline:
    out = subprocess.run(I.EGRESS_CMD, capture_output=True, text=True)
    ok, rows = I.parse_list_silos(out.stdout)
    row = [r for r in rows if r.get('name') == silo] if ok else []
    state = row[0].get('state') if row else None
    if state == 'Stopping':
        break
    time.sleep(0.2)
if state != 'Stopping':
    print('silo %s never became externally observable as Stopping; last state=%s' % (silo, state))
    sys.exit(1)
print('silo %s is Stopping' % silo)
PYEOF
); then
    record 8.2 PASS "silo $SILO observed in Stopping"
else
    record 8.2 FAIL "silo $SILO never observed in Stopping"
fi
sleep 4
assert_ind 8.2 egress_active 1              # still live: Stopping is not dark
assert_ind_contains 8.2 egress_detail "$SILO"
# Unreachable session manager must read UNVERIFIED, never "no egress".
# The marker (provenance for Setup) is written only by a VERIFIED stop of a
# manager that was active right before it.
SM_STOPPED=0
if sm_stop_verified; then
    SM_STOPPED=1
else
    record 8.3 ERROR "stop of qdistro-session-manager.service not verified (before: invocation ${SM_PRE_INV:-none}; after: load=${SM_LOAD:-?} active=${SM_ACTIVE:-?} invocation=${SM_INV:-none}); no marker written"
fi
sleep 5
assert_ind 8.3 egress_observer failed
assert_ind 8.3 egress_active 0
host s8b-alarm     # 8.3: the egress-unverified row is drawn in mError
# Recovery only restarts a manager THIS scenario verifiably stopped; the
# marker is cleared only once the restart is verified (sm_try_start). An
# unverified stop is not "repaired" here: the manager is left as found.
if [ "$SM_STOPPED" != 1 ]; then
    record 8.4 ERROR "recovery start skipped: this scenario did not verifiably stop the manager (see 8.3)"
elif ! sm_try_start; then
    record 8.4 FAIL "could not restart qdistro-session-manager.service (active=${SM_ACTIVE:-?} invocation=${SM_INV:-none})"
fi
sleep 4
assert_ind 8.4 egress_observer ok

# ------------------------------------------------------------------ Step 9
echo "== Step 9 — locked-state restart of qdlocker"
host s9-drain
lock_now 9.0
U systemctl --user restart qdlocker.service
sleep 5
wait_status 15 || true
case "$(ctrl status)" in
    *locked=True*) record 9.1 PASS "locker came back locked :: $(ctrl status)" ;;
    *) record 9.1 FAIL "locker did not come back locked :: $(ctrl status)" ;;
esac
assert_ind 9.1 capture_observer ok
host s9-rec

# ------------------------------------------------------------------ Step 10
echo "== Step 10 — second output (CONDITIONAL)"
OUTPUTS=$(count_drm_outputs); rc=$?
echo "qdwin enabled DRM outputs: ${OUTPUTS:-?}"
if [ "$rc" != 0 ]; then
    record 10 ERROR "could not count the compositor's DRM outputs (journal query failed, or no 'Output <name> (crtc N) video modes:' line): $(tail -2 "$SCR/journal.err" | tr '\n' ' ')"
elif [ "$OUTPUTS" -lt 2 ]; then
    record 10 SKIP "compositor enabled $OUTPUTS DRM output(s); needs a two-head VM"
else
    host s10-drain
    lock_now 10.0
    sleep 3
    host s10-heads   # 10.1 / 10.2 are decided host-side (run.sh)
fi

# ------------------------------------------------------------------ Cleanup
echo "== Cleanup"
trap - EXIT
guest_cleanup
host cleanup-drain
if [ "$ERRORS" -gt 0 ]; then
    echo "VERDICT ERROR ($ERRORS undecided assertion(s); $FAILS failed)"
    exit 3
fi
if [ "$FAILS" -gt 0 ]; then
    echo "VERDICT FAIL ($FAILS failed guest assertion(s))"
    exit 1
fi
echo "VERDICT PASS"
exit 0
