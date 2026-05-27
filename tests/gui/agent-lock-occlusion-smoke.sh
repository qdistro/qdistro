#!/bin/bash
# Mechanical qdlocker occlusion smoke test.
#
# This is the executable companion to 07-lock-occludes-desktop.md. It
# avoids subjective visual judgment by placing a full-screen magenta
# sentinel toplevel behind the locker, then asserting no magenta pixels
# remain visible after qdlocker reports locked=True.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/qdlocker-helpers.sh"

qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdlocker_session_healthy

run_id="${VMNAME:-vm}-$$"
pre="/tmp/qdlocker-occlusion-${run_id}-pre.png"
locked="/tmp/qdlocker-occlusion-${run_id}-locked.png"

cleanup() {
    qdlocker_drain_lock_state >/dev/null 2>&1 || true
    "$QDWIN_VM_EXEC" "$VMNAME" '
      pkill -u admin -x qdistro-test-window 2>/dev/null || true
    ' >/dev/null 2>&1 || true
}
trap cleanup EXIT

qdlocker_drain_lock_state

"$QDWIN_VM_EXEC" "$VMNAME" '
  pkill -u admin -x qdistro-test-window 2>/dev/null || true
  pkill -u admin -x foot 2>/dev/null || true
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
    qdistro-test-window --title qdlocker-sentinel \
      --width 1920 --height 1080 --color 0xffff00ff \
      >/tmp/qdlocker-sentinel.log 2>&1 &
'
sleep 1.5

qdwin_screenshot "$pre" >/dev/null
qdlocker_assert_color_present_in_crop "$pre" '#ff00ff' '1920x1080+0+0' whole-screen

qdlocker_ctrl lock >/dev/null
qdlocker_wait_for_lock 5
sleep 0.5
qdwin_screenshot "$locked" >/dev/null

status="$(qdlocker_ctrl status)"
case "$status" in
    *locked=True*) ;;
    *)
        echo "FAIL: qdlocker status after lock: $status" >&2
        exit 1
        ;;
esac

qdlocker_assert_color_absent_in_crop "$locked" '#ff00ff' '1920x96+0+0' top-edge
qdlocker_assert_color_absent_in_crop "$locked" '#ff00ff' '160x1080+0+0' left-edge
qdlocker_assert_color_absent_in_crop "$locked" '#ff00ff' '160x160+0+0' top-left-corner
qdlocker_assert_color_absent_in_crop "$locked" '#ff00ff' '1920x1080+0+0' whole-screen

echo "PASS: qdlocker fully occluded sentinel desktop"
echo "pre=$pre"
echo "locked=$locked"
