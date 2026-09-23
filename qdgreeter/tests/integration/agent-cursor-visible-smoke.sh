#!/bin/bash
# agent-cursor-visible-smoke — host-driven regression test for the
# qdgreeter login-screen mouse cursor.
#
# Guards the bug fixed in app.py::_ensure_eglfs_software_cursor: the
# greeter runs on eglfs/KMS, which by default draws the pointer on a DRM
# *hardware* cursor plane. The qdistro VM's virtual GPU does not scan that
# plane out, so the login screen had NO visible mouse cursor. The fix
# forces Qt's GL/software cursor (KMS "hwcursor": false) AND keeps Qt
# input enabled (eglfs only creates a cursor when it has an input device).
#
# This test proves BOTH halves on a real VM, the only place the bug shows:
#   1. With the pointer parked in an empty area, a cluster of bright
#      (cursor) pixels appears there that was NOT present before — i.e.
#      the cursor is actually rendered to the framebuffer (visible).
#   2. Moving the pointer to a second empty area moves the bright cluster
#      with it — i.e. it is the live pointer cursor (input-driven), not a
#      static decoration. This is what would regress if someone re-added
#      QT_QPA_EGLFS_DISABLE_INPUT.
#
# Runs on the HOST (needs virsh screenshot + QMP input injection); the
# greeter has no Wayland display so in-VM screenshot tools cannot see it.
#
# Usage:  VMNAME=qdistro-daily bash agent-cursor-visible-smoke.sh
# Exit:   0 pass, 1 assertion failed, 2 setup error.

set -u

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)        # qdgreeter/
WORKSPACE=$(cd "$ROOT/.." && pwd)                                # doc/qdistro2/
VIRSH=${VIRSH:-virsh -c qemu:///session}
VMNAME=${VMNAME:-$($VIRSH list --name --state-running 2>/dev/null | head -n1)}
VM_EXEC=${VM_EXEC:-$WORKSPACE/qdistro/scripts/vm/vm-exec}

fail()  { echo "FAIL: $*" >&2; exit 1; }
setup() { echo "SETUP-ERROR: $*" >&2; exit 2; }
pass()  { echo "PASS: $*"; }
note()  { echo "INFO: $*"; }

[ -n "$VMNAME" ]      || setup "no running VM (set VMNAME=...)"
[ -x "$VM_EXEC" ]     || setup "vm-exec helper not found at $VM_EXEC"
command -v magick >/dev/null 2>&1 || command -v convert >/dev/null 2>&1 \
    || setup "ImageMagick (magick/convert) required for pixel analysis"
IM=$(command -v magick || command -v convert)

vm_exec() { "$VM_EXEC" "$VMNAME" "$@"; }

# QMP absolute pointer move (0..32767 abs range). We ramp through a few
# intermediate points instead of a single teleport: eglfs only repaints
# the software cursor on pointer *motion*, so a lone jump from a stale
# position is not reliably reflected in the next frame, whereas a short
# stream of deltas always is. LAST_X/LAST_Y remember where we left it.
LAST_X=0; LAST_Y=0
qmp_abs() {  # qmp_abs <px_x> <px_y>
    local vx vy
    vx=$(( $1 * 32767 / SCREEN_W ))
    vy=$(( $2 * 32767 / SCREEN_H ))
    $VIRSH qemu-monitor-command "$VMNAME" \
        "{\"execute\":\"input-send-event\",\"arguments\":{\"events\":[{\"type\":\"abs\",\"data\":{\"axis\":\"x\",\"value\":$vx}},{\"type\":\"abs\",\"data\":{\"axis\":\"y\",\"value\":$vy}}]}}" \
        >/dev/null 2>&1 || fail "QMP input-send-event failed (is the VM QMP reachable?)"
}
qmp_move() {  # qmp_move <px_x> <px_y>
    local tx=$1 ty=$2 i steps=6 ix iy
    for i in $(seq 1 $steps); do
        ix=$(( LAST_X + (tx - LAST_X) * i / steps ))
        iy=$(( LAST_Y + (ty - LAST_Y) * i / steps ))
        qmp_abs "$ix" "$iy"
        sleep 0.12
    done
    LAST_X=$tx; LAST_Y=$ty
    sleep 0.6
}

# virsh writes PNG content (regardless of extension); normalise to a real
# PNG we can analyse, and expose its pixel dimensions in SHOT_W/SHOT_H.
SHOT_W=""; SHOT_H=""
shot() {  # shot <png-path>
    # Capture with a .ppm extension: virsh actually emits PNG bytes, and
    # ImageMagick sniffs the real format from the content (a no-magic
    # extension like .raw would be misread as headerless raw RGB).
    local raw="$1.cap.ppm"
    $VIRSH screenshot "$VMNAME" "$raw" >/dev/null 2>&1 || fail "virsh screenshot failed"
    "$IM" "$raw" "$1" >/dev/null 2>&1 || fail "image convert failed"
    read -r SHOT_W SHOT_H < <(file -b "$raw" \
        | sed -n 's/.* \([0-9][0-9]*\) x \([0-9][0-9]*\).*/\1 \2/p')
    rm -f "$raw"
}

# Count near-white pixels in a 70x70 box centred on (cx,cy). The cursor
# arrow is white (#ffffff) with a black outline; the empty greeter
# background is near-black, so a Gray>55% threshold isolates the cursor.
white_in_box() {  # white_in_box <png> <cx> <cy>
    local img=$1 cx=$2 cy=$3 x y
    x=$(( cx - 35 )); y=$(( cy - 35 ))
    "$IM" "$img" -crop "70x70+${x}+${y}" +repage -colorspace Gray \
        -threshold 55% -format "%[fx:int(mean*w*h)]" info: 2>/dev/null
}

# ---------------------------------------------------------------------------
# Bring up a fresh greeter on tty3 (idempotent; tolerates an already-up
# greeter or an admin desktop holding the VT).
# ---------------------------------------------------------------------------
# We verify on a freshly *cold-booted* greeter rather than restarting
# greetd at runtime. On qdistro's eglfs/KMS greeter the pointer device
# attaches reliably on cold boot, but a runtime `systemctl restart greetd`
# races the new eglfs init against the previous greeter's VT/DRM teardown
# and the pointer can fail to attach (no cursor). Cold boot is also the
# real production path, so it is what we want to guard. Set
# QDGREETER_REUSE_GREETER=1 to skip the reboot and test whatever greeter
# is already on tty3 (faster local iteration).
greeter_pid() { vm_exec 'pgrep -f qdgreeter.app 2>/dev/null | head -n1' | tr -d '[:space:]'; }

if [ "${QDGREETER_REUSE_GREETER:-0}" = "1" ]; then
    note "QDGREETER_REUSE_GREETER=1: using the greeter already on tty3"
    vm_exec 'chvt 3' >/dev/null 2>&1; sleep 1
else
    note "cold-booting $VMNAME for a clean greeter (eglfs pointer attaches reliably on boot)"
    vm_exec 'systemctl reboot' >/dev/null 2>&1 || true
    sleep 10
    # wait for the guest agent to come back
    back=0
    for _ in $(seq 1 60); do
        if vm_exec 'true' >/dev/null 2>&1; then back=1; break; fi
        sleep 2
    done
    [ "$back" = "1" ] || setup "VM did not come back after reboot"
fi

GREETER_PID=""
for _ in $(seq 1 40); do
    vm_exec 'chvt 3' >/dev/null 2>&1
    # The /usr/bin/qdgreeter wrapper exec's into `python3 -m qdgreeter.app`,
    # so match the module, not the wrapper path.
    GREETER_PID=$(greeter_pid)
    [ -n "$GREETER_PID" ] && break
    sleep 1
done
[ -n "$GREETER_PID" ] || setup "qdgreeter process not running on tty3 after boot"
sleep 1
pass "qdgreeter is running on tty3 (pid $GREETER_PID)"

# ---------------------------------------------------------------------------
# Screen geometry from a screenshot.
# ---------------------------------------------------------------------------
shot /tmp/qdg-cursor-probe-geom.png
SCREEN_W=$SHOT_W; SCREEN_H=$SHOT_H
[ -n "${SCREEN_W:-}" ] && [ -n "${SCREEN_H:-}" ] || setup "could not read screen dimensions"
note "screen is ${SCREEN_W}x${SCREEN_H}"

# Two probe points in the empty band BELOW the centred login card
# (the card lives roughly in the vertical middle; the bottom ~15% is
# blank on both sides), far apart horizontally.
P1X=$(( SCREEN_W * 18 / 100 )); P1Y=$(( SCREEN_H * 85 / 100 ))
P2X=$(( SCREEN_W * 82 / 100 )); P2Y=$(( SCREEN_H * 85 / 100 ))
PARKX=4; PARKY=4   # top-left corner, away from both probe points

# ---------------------------------------------------------------------------
# 1. Baseline: park the cursor in the corner; probe point P1 must be dark.
# ---------------------------------------------------------------------------
qmp_move "$PARKX" "$PARKY"
shot /tmp/qdg-cursor-probe-baseline.png
BASE_P1=$(white_in_box /tmp/qdg-cursor-probe-baseline.png "$P1X" "$P1Y")
note "baseline white pixels at P1($P1X,$P1Y) = ${BASE_P1:-?}"
[ "${BASE_P1:-0}" -lt 15 ] \
    || fail "probe region P1 is not empty at baseline (${BASE_P1} white px); pick a clearer spot"

# ---------------------------------------------------------------------------
# 2. Move the cursor onto P1: bright cursor pixels must now appear there.
# ---------------------------------------------------------------------------
qmp_move "$P1X" "$P1Y"
shot /tmp/qdg-cursor-probe-p1.png
CUR_P1=$(white_in_box /tmp/qdg-cursor-probe-p1.png "$P1X" "$P1Y")
note "white pixels at P1 with cursor parked there = ${CUR_P1:-?}"
[ "${CUR_P1:-0}" -ge 40 ] \
    || fail "no visible cursor at P1 (only ${CUR_P1} white px) — the eglfs cursor is invisible; \
hwcursor was not disabled or Qt input is disabled"
pass "mouse cursor is VISIBLE on the login screen (${CUR_P1} cursor px at P1)"

# ---------------------------------------------------------------------------
# 3. Move to P2: the bright cluster must follow the pointer (proves it is
#    the live, input-driven cursor — not a static artifact).
# ---------------------------------------------------------------------------
qmp_move "$P2X" "$P2Y"
shot /tmp/qdg-cursor-probe-p2.png
P1_AFTER=$(white_in_box /tmp/qdg-cursor-probe-p2.png "$P1X" "$P1Y")
P2_AFTER=$(white_in_box /tmp/qdg-cursor-probe-p2.png "$P2X" "$P2Y")
note "after moving to P2: white at P1=${P1_AFTER:-?}, white at P2=${P2_AFTER:-?}"
[ "${P2_AFTER:-0}" -ge 40 ] \
    || fail "cursor did not appear at P2 (${P2_AFTER} white px) — pointer input not driving the cursor"
[ "${P1_AFTER:-99}" -lt 15 ] \
    || fail "cursor cluster did not leave P1 (${P1_AFTER} white px still there) — not a movable cursor"
pass "mouse cursor MOVES with the pointer (P1 cleared to ${P1_AFTER}, P2 now ${P2_AFTER})"

echo "PASS: qdgreeter login screen shows a visible, movable mouse cursor"
exit 0
