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
# `VERDICT <PASS|FAIL|ERROR>`. Exit 0 PASS, 1 FAIL, 3 ERROR.

# QCI_GUI_WAITERS is a test seam (qdlocker-gui09-runner.bats); the lane
# installs the library at /tmp/qci-gui-waiters.sh.
# shellcheck source=/dev/null
source "${QCI_GUI_WAITERS:-/tmp/qci-gui-waiters.sh}" || exit 2
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
    pkill -u admin -x pw-record 2>/dev/null || true
    pkill -u admin -x parec 2>/dev/null || true
    pkill -u admin -x gst-launch-1.0 2>/dev/null || true
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
    # A SYSTEM unit: root's systemctl, never through runuser (polkit refuses).
    systemctl start qdistro-session-manager.service 2>/dev/null || true
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
    host cleanup-drain
    echo "VERDICT ERROR"
    exit 3
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
# Reclaim whatever an earlier attempt left (see reset_state) BEFORE the
# locker restart below and before any silo call: stale recorders, the
# Stopping fixture, the Step 7 pw-dump break, a stopped session manager.
reset_state >/dev/null
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
DRM_FAILS=$(runuser -l admin -c "journalctl --user -u qdwin-compositor.service --boot --no-pager" 2>/dev/null \
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
U setsid pw-record --target=@DEFAULT_SOURCE@ "$ASCR/mic.wav" >"$SCR/mic.log" 2>&1 </dev/null &
sleep 6            # two polls; deliberately NO lock cycle
# The capture must actually be running, or 2.1 would grade the harness, not
# the observer (a recorder that exited is ERROR, never a product FAIL).
pgrep -u admin -x pw-record >/dev/null \
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
pkill -u admin -x pw-record || true
sleep 7
assert_ind 3.1 capture_active 0
assert_ind 3.1 capture_observer ok
host s3-quiet

# ------------------------------------------------------------------ Step 4
echo "== Step 4 — system-audio (sink-monitor) capture (CONDITIONAL)"
MON=$(U pactl get-default-sink 2>/dev/null | tr -d '\r')
if [ -z "$MON" ]; then
    record 4 SKIP "no default sink (pactl get-default-sink empty); system-audio capture not exercised"
else
    U setsid parec -d "${MON}.monitor" -r "$ASCR/sysaudio.raw" >"$SCR/sysaudio.log" 2>&1 </dev/null &
    sleep 6
    pgrep -u admin -x parec >/dev/null \
        || error "parec is not running 6s after start: $(tail -3 "$SCR/sysaudio.log" | tr '\n' ' ')"
    assert_ind 4.1 capture_active 1
    assert_ind_contains 4.1 capture_kinds systemAudio
    if field capture_kinds "$(ind)" | grep -q microphone; then
        record 4.1 FAIL "sink-monitor capture also reported as microphone :: $(ind)"
    else
        record 4.1 PASS "monitor capture not reported as microphone"
    fi
    host s4-alarm
    pkill -u admin -x parec || true
    sleep 7
    assert_ind 4.2 capture_active 0
fi

# ------------------------------------------------------------------ Step 5
echo "== Step 5 — camera (CONDITIONAL)"
CAMID=$(cd / && U python3 -I - 2>"$SCR/cam-probe.err" <<'PYEOF' | tr -d '\r'
import subprocess
from qdlocker import indicators as I
ok, nodes = I.parse_pw_dump(subprocess.run(I.CAPTURE_CMD, capture_output=True, text=True).stdout)
for n in nodes:
    p = n["props"]
    if str(p.get("media.class", "")) == "Video/Source":
        print(p.get("node.name", "")); break
PYEOF
)
if [ -z "$CAMID" ]; then
    record 5 SKIP "no PipeWire Video/Source node in this VM (no camera, no v4l2loopback)"
elif ! command -v gst-launch-1.0 >/dev/null; then
    record 5 SKIP "gst-launch-1.0 not installed; cannot drive the camera node $CAMID"
else
    CAMNODE=$(U pw-cli ls Node | awk -v n="node.name = \"$CAMID\"" '/id /{id=$2} index($0,n){print id; exit}' | tr -d ',')
    U setsid gst-launch-1.0 -q pipewiresrc path="$CAMNODE" ! fakesink >"$SCR/cam.log" 2>&1 </dev/null &
    sleep 6
    if ! pgrep -u admin -x gst-launch-1.0 >/dev/null; then
        # The driver must actually be running, or a SKIP masquerades as a pass.
        record 5 SKIP "gst-launch-1.0 exited immediately (node $CAMID id=${CAMNODE:-?}; node id parse?): $(tail -3 "$SCR/cam.log" | tr '\n' ' ')"
    else
        assert_ind 5.1 capture_active 1
        assert_ind_contains 5.1 capture_kinds camera
        host s5-alarm
    fi
    pkill -u admin -x gst-launch-1.0 || true
    sleep 7
fi

# ------------------------------------------------------------------ Step 6
echo "== Step 6 — screencast via a qdwin view stream (CONDITIONAL, MANUAL)"
# Nothing in this driver creates a view stream (the scenario does not
# reimplement a Wayland client). The node set is sampled around a 5 s window;
# only a NEW weston.pipewire node lets the step assert, otherwise it SKIPs.
count_pw_nodes() { U pw-cli ls Node 2>/dev/null | grep -c 'weston\.pipewire' || true; }
NODES_BEFORE=$(count_pw_nodes)
echo "RUNNER (manual): a qdwin view stream started now would be observed (nodes before: $NODES_BEFORE)"
sleep 5
NODES_AFTER=$(count_pw_nodes)
if [ "${NODES_AFTER:-0}" -le "${NODES_BEFORE:-0}" ]; then
    record 6 SKIP "no new weston.pipewire node appeared ($NODES_BEFORE -> $NODES_AFTER); no view stream was started"
else
    sleep 4
    assert_ind_contains 6.1 capture_kinds screencast
    host s6-alarm
fi

# ------------------------------------------------------------------ Step 7
echo "== Step 7 — observer failure must be visible"
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
if runuser -l admin -c "journalctl --user -u qdlocker.service --boot --no-pager" 2>/dev/null \
        | grep -q "observer timed out; killing scan"; then
    record 7.3 PASS "journal: observer timed out; killing scan"
else
    record 7.3 FAIL "the hard timeout did not fire (no 'observer timed out; killing scan' in qdlocker.service's journal)"
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
systemctl stop qdistro-session-manager.service || record 8.3 FAIL "could not stop qdistro-session-manager.service"
sleep 5
assert_ind 8.3 egress_observer failed
assert_ind 8.3 egress_active 0
host s8b-alarm     # 8.3: the egress-unverified row is drawn in mError
systemctl start qdistro-session-manager.service || record 8.4 FAIL "could not start qdistro-session-manager.service"
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
OUTPUTS=$(runuser -l admin -c "journalctl --user -u qdwin-compositor.service --boot --no-pager" 2>/dev/null \
    | grep -oE "output_created[^\n]*name=[A-Za-z0-9-]+" | grep -oE "name=[A-Za-z0-9-]+" \
    | sort -u | wc -l)
echo "qdwin enabled outputs: $OUTPUTS"
if [ "${OUTPUTS:-0}" -lt 2 ]; then
    record 10 SKIP "compositor has ${OUTPUTS:-0} enabled output(s); needs a two-head VM"
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
if [ "$FAILS" -gt 0 ]; then
    echo "VERDICT FAIL ($FAILS failed guest assertion(s))"
    exit 1
fi
echo "VERDICT PASS"
exit 0
