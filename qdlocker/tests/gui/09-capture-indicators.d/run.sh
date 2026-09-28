#!/bin/bash
# 09-capture-indicators.d/run.sh <vmname> — HOST runner of
# qdlocker/tests/gui/09-capture-indicators.md.
#
# Starts guest.sh as the ONE claimed guest driver (Setup..Cleanup) and, while
# it runs, serves EVERY host step it publishes, by the name it publishes: no
# list of expected steps lives here. A fixed list is what broke
# full-20260928T154720Z: the host loop waited for `s4` while the guest had
# skipped the conditional Step 4 and sat on `s7`, and every later step
# deadlocked. The action is the suffix of the step name (see guest.sh header):
#   <id>-quiet|alarm|rec  capture $ART/<id>.png (+ banner-band colour check)
#   <id>-drain            qdlocker_drain_lock_state
#   <id>-heads            Step 10 two-head histogram checks
#
# Needs QCI_GUI_ARTIFACT_DIR. Writes there: driver.log (the guest driver's
# output, delivered when it exits), host.log, host-checks.tsv, <id>.png frames,
# summary.txt. Prints the summary and exits 0 PASS, 1 FAIL, 3 ERROR.
#
# Run it in the FOREGROUND of one long-running command and let it finish
# (typically 4-6 min): it owns the guest driver's vm-exec. Do not start a
# second copy while one runs — the guest claim refuses a second driver.
#
# Test seams (tests/integration/qci/qdlocker-gui09-runner.bats):
# QDLOCKER_09_VMEXEC, QDLOCKER_09_GUEST_DIR, QDLOCKER_09_GUEST_SCRIPT,
# QDLOCKER_09_HOOKS (a file sourced after the default actions are defined).

set -u
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
VMNAME=${1:-${VMNAME:-}}
[ -n "$VMNAME" ] || { echo "usage: run.sh <vmname>" >&2; exit 3; }
ART=${QCI_GUI_ARTIFACT_DIR:?run.sh needs QCI_GUI_ARTIFACT_DIR}
mkdir -p "$ART" || exit 3
SLUG=${QDLOCKER_09_SLUG:-qdlocker_tests_gui_09-capture-indicators.md}
GDIR=${QDLOCKER_09_GUEST_DIR:-/tmp/qci/$SLUG}
GUEST_SCRIPT=${QDLOCKER_09_GUEST_SCRIPT:-$HERE/guest.sh}
DRIVER_TIMEOUT=${QDLOCKER_09_DRIVER_TIMEOUT:-1900}
ERR_COLOR='#FD4663'   # shim/Color.qml mError — the banner border while alarming

# shellcheck source=../qdlocker-helpers.sh
source "$HERE/../qdlocker-helpers.sh" || { echo "ERROR: cannot source qdlocker-helpers.sh" >&2; exit 3; }
qdwin_set_vm "$VMNAME"
VMEXEC=${QDLOCKER_09_VMEXEC:-$QDWIN_VM_EXEC}

CHECKS=$ART/host-checks.tsv
HLOG=$ART/host.log
: >"$CHECKS"
: >"$HLOG"

check() {   # check <id> <what> <PASS|FAIL|ERROR|WARN> <detail>
    printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >>"$CHECKS"
    printf 'HOSTCHECK %s %s %s %s\n' "$1" "$2" "$3" "$4" >>"$HLOG"
}

# vm-exec with stdout and stderr on separate FILES (never a pipe: a
# descendant holding a pipe keeps the reader blocked after vm-exec exits;
# never merged: vm-exec's own stderr lines are not guest output).
VX_OUT=""
vx() {
    local o e rc=0
    o=$(mktemp "$ART/.vx-out.XXXXXX") || return 99
    e=$(mktemp "$ART/.vx-err.XXXXXX") || { rm -f "$o"; return 99; }
    "$VMEXEC" "$VMNAME" "$1" >"$o" 2>"$e" || rc=$?
    VX_OUT=$(head -c 65536 "$o")
    [ "$rc" -eq 0 ] || { printf 'vm-exec rc=%s for: %s\n' "$rc" "$1"; cat "$e"; } >>"$HLOG"
    rm -f "$o" "$e"
    return "$rc"
}

read_waiting() {   # prints a well-formed token, or nothing
    vx "cat $GDIR/waiting 2>/dev/null || true" || return 0
    local t=${VX_OUT//[$'\r\n']/}
    [[ $t =~ ^[A-Za-z0-9_-]+\.[0-9]+\.[0-9]+$ ]] && printf '%s' "$t"
    return 0
}

send_go() {
    for _ in 1 2 3; do   # vm-exec exit 75 = refused to launch; retryable
        vx "mkdir $GDIR/$1.go" && return 0
        sleep 2
    done
    return 1
}

# ------------------------------------------------------------ default actions
act_capture() {   # act_capture <id> <quiet|alarm|rec>
    local id=$1 mode=$2 out="$ART/$1.png" sw sh bh crop n
    if ! qdwin_screenshot "$out" >/dev/null 2>>"$HLOG" || [ ! -s "$out" ]; then
        check "$id" capture ERROR "qdwin_screenshot produced no frame (see host.log)"
        return
    fi
    [ ! -f "$out.meta" ] || check "$id" capture WARN "retained (stale) frame: $(cat "$out.meta")"
    read -r sw sh < <(qdlocker_screenshot_dimensions "$out")
    if [ -z "${sw:-}" ] || [ -z "${sh:-}" ]; then
        check "$id" capture ERROR "cannot read the frame's screen dimensions"
        return
    fi
    # The banner is anchored to the top of the lock surface: a generous top band.
    bh=$(( sh / 4 > 220 ? sh / 4 : 220 ))
    crop="${sw}x${bh}+0+0"
    n=$(qdlocker_count_color_in_crop "$out" "$ERR_COLOR" "$crop" 2>>"$HLOG") \
        || { check "$id" banner ERROR "colour count failed on $out"; return; }
    case $mode in
        quiet) if [ "$n" -eq 0 ]; then check "$id" banner-quiet PASS "no $ERR_COLOR in $crop"
               else check "$id" banner-quiet FAIL "$n px of $ERR_COLOR in $crop"; fi ;;
        alarm) if [ "$n" -gt 0 ]; then check "$id" banner-alarm PASS "$n px of $ERR_COLOR in $crop"
               else check "$id" banner-alarm FAIL "no $ERR_COLOR in $crop"; fi ;;
        rec)   check "$id" capture PASS "recorded ($n px of $ERR_COLOR in $crop, not asserted)" ;;
    esac
}

act_drain() {   # act_drain <id>
    if qdlocker_drain_lock_state >>"$HLOG" 2>&1; then
        check "$1" drain PASS "lock state drained"
    else
        check "$1" drain ERROR "qdlocker_drain_lock_state failed (see host.log)"
    fi
}

# histogram <image>: ImageMagick's colour histogram, one colour per line.
# Fails (rc != 0) when the decode fails or yields no colour at all: an
# unreadable frame must never be judged "uniformly black".
histogram() {
    local out
    out=$(magick "$1" -format %c histogram:info:- 2>>"$HLOG") || return 1
    out=$(printf '%s\n' "$out" | grep .) || return 1
    printf '%s\n' "$out"
}

act_heads() {   # act_heads <id> — Step 10.1/10.2 on a two-head VM
    local id=$1 p="$ART/$1-primary.ppm" s="$ART/$1-secondary.ppm" hp hs colors nonblack
    if ! $QDWIN_VIRSH screenshot "$VMNAME" "$s" --screen 1 >>"$HLOG" 2>&1 \
       || ! $QDWIN_VIRSH screenshot "$VMNAME" "$p" --screen 0 >>"$HLOG" 2>&1; then
        check "$id" heads ERROR "virsh screenshot --screen 0/1 failed"
        return
    fi
    # 10.2 (weak, observed): the primary is not one flat colour.
    if ! hp=$(histogram "$p"); then
        check "$id" primary-painted ERROR "could not decode $p (empty or failed histogram)"
    else
        colors=$(printf '%s\n' "$hp" | wc -l)
        if [ "$colors" -gt 1 ]; then check "$id" primary-painted PASS "$colors colours"
        else check "$id" primary-painted FAIL "primary output is uniformly one colour — no lock UI"; fi
    fi
    # 10.1: the secondary is uniformly black — judged only on a real decode.
    if ! hs=$(histogram "$s"); then
        check "$id" secondary-black ERROR "could not decode $s (empty or failed histogram); 10.1 not decided"
        return
    fi
    nonblack=$(printf '%s\n' "$hs" | grep -v -e '#000000' -e 'srgb(0,0,0)' | head -5)
    if [ -z "$nonblack" ]; then check "$id" secondary-black PASS "uniformly black"
    else check "$id" secondary-black FAIL "not uniformly black: $(printf '%s' "$nonblack" | tr '\n' ';')"; fi
}

serve() {   # serve <token>
    local tok=$1 name id action
    name=${tok%%.*}
    id=${name%-*}
    action=${name##*-}
    printf '[run.sh] host step %s (token %s)\n' "$name" "$tok" >>"$HLOG"
    case $action in
        quiet|alarm|rec) act_capture "$id" "$action" ;;
        drain) act_drain "$id" ;;
        heads) act_heads "$id" ;;
        *) check "$name" action ERROR "unknown host-step action '$action'" ;;
    esac
    send_go "$tok" || check "$name" go ERROR "could not create $GDIR/$tok.go"
}

# shellcheck source=/dev/null
[ -z "${QDLOCKER_09_HOOKS:-}" ] || source "$QDLOCKER_09_HOOKS"

# ------------------------------------------------------------ run
# A `waiting` left by an earlier, dead driver is never served (the new driver
# also clears it when it claims).
STALE=$(read_waiting)
B64=$(base64 -w0 "$GUEST_SCRIPT") || exit 3
timeout -k 30 "$DRIVER_TIMEOUT" "$VMEXEC" "$VMNAME" \
    "mkdir -p $GDIR && echo $B64 | base64 -d >$GDIR/driver.sh && env -u QCI_GUI_WAITERS -u QDLOCKER_09_TEST_HARNESS QDLOCKER_09_DIR=$GDIR bash $GDIR/driver.sh" \
    >"$ART/driver.log" 2>&1 </dev/null &
DRV=$!
# vm-exec on TERM/INT/HUP stops the pinned guest tree; forward, never SIGKILL.
trap 'kill -TERM "$DRV" 2>/dev/null' TERM INT HUP

handled=""
while kill -0 "$DRV" 2>/dev/null; do
    tok=$(read_waiting)
    if [ -n "$tok" ] && [ "$tok" != "$handled" ] && [ "$tok" != "$STALE" ]; then
        serve "$tok"
        handled=$tok
    fi
    sleep 1
done
drc=0
wait "$DRV" || drc=$?
trap - TERM INT HUP

# ------------------------------------------------------------ summary
{
    echo "# 09-capture-indicators — run.sh summary"
    echo "guest driver exit: $drc (0 PASS, 1 FAIL, 3 ERROR; 2 = claim/library; 124/137 = timeout)"
    echo "## guest assertions (driver.log)"
    grep -E '^(ASSERT|VERDICT|ERROR)' "$ART/driver.log" || echo "(none — see driver.log)"
    echo "## host checks (host-checks.tsv)"
    if [ -s "$CHECKS" ]; then tr '\t' ' ' <"$CHECKS"; else echo "(none)"; fi
    echo "## frames to open and grade"
    ls -1 "$ART"/*.png 2>/dev/null || echo "(none)"
} >"$ART/summary.txt"

# ERROR takes precedence: a run that also hit a harness/transport/probe error
# did not complete, so its FAILs are reported but the class is ERROR (a
# FAIL-then-timeout is not a clean product failure). FAIL needs a completed
# run: guest verdict FAIL (exit 1) or PASS with a failed host check.
result=PASS
n_fail=$(( $(grep -c '^ASSERT [^ ]* FAIL' "$ART/driver.log") + $(grep -c $'\tFAIL\t' "$CHECKS") ))
if grep -q '^ASSERT [^ ]* ERROR' "$ART/driver.log" || grep -q $'\tERROR\t' "$CHECKS" \
   || { [ "$drc" -ne 0 ] && [ "$drc" -ne 1 ]; } \
   || ! grep -qE '^VERDICT (PASS|FAIL)' "$ART/driver.log"; then
    result=ERROR
elif [ "$n_fail" -gt 0 ]; then
    result=FAIL
fi
[ "$result" != ERROR ] || [ "$n_fail" -eq 0 ] || echo "note: $n_fail FAIL row(s) above, in a run that did not complete" >>"$ART/summary.txt"
echo "RESULT $result" >>"$ART/summary.txt"
cat "$ART/summary.txt"
case $result in
    PASS) exit 0 ;;
    FAIL) exit 1 ;;
    *) exit 3 ;;
esac
