#!/bin/bash
# Mechanical qdlocker occlusion smoke test.
#
# This is the executable companion to 07-lock-occludes-desktop.md. It
# avoids subjective visual judgment by placing a full-screen magenta
# sentinel toplevel behind the locker, then asserting no magenta pixels
# remain visible after qdlocker reports locked=True.

# NOT pipefail: qdlocker_count_color_in_crop validates the histogram with
# `printf | grep -q`, and grep -q exits on the first match; with a large
# histogram printf then dies of SIGPIPE and pipefail turns that into a bogus
# "no histogram" setup failure (rc=2) on a perfectly good frame.
set -eu

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
      pkill -KILL -u admin -f ^qdistro-test-window 2>/dev/null || true
    ' >/dev/null 2>&1 || true
}
trap cleanup EXIT

qdlocker_drain_lock_state

# Query actual output resolution from a baseline screenshot rather than
# hardcoding 1920x1080.  Crop geometry must match the real framebuffer
# or asserts silently pass/fail at the wrong coordinates.
qdwin_screenshot "$pre" >/dev/null
# here-string, not < <(...): the helper prints "W H" with NO trailing newline,
# so `read` hit EOF, returned 1 and set -e killed the smoke right here.
read -r SW SH <<<"$(qdlocker_screenshot_dimensions "$pre")"
if [ -z "$SW" ] || [ -z "$SH" ] || [ "$SW" -lt 640 ] || [ "$SH" -lt 480 ]; then
    echo "FAIL: unexpected screenshot dimensions ${SW}x${SH}" >&2
    exit 2
fi

"$QDWIN_VM_EXEC" "$VMNAME" "
  pkill -KILL -u admin -f ^qdistro-test-window 2>/dev/null || true
  pkill -u admin -x foot 2>/dev/null || true
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
    qdistro-test-window --title qdlocker-sentinel \
      --width ${SW} --height ${SH} --color 0xffff00ff \
      >/tmp/qdlocker-sentinel.log 2>&1 &
"
sleep 1.5

qdwin_screenshot "$pre" >/dev/null
qdlocker_assert_color_present_in_crop "$pre" '#ff00ff' "${SW}x${SH}+0+0" whole-screen

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

# Edge bands: top 96px, left 160px, top-left corner, and full screen.
edge_h=$(( SH / 11 > 96 ? SH / 11 : 96 ))
edge_w=$(( SW / 12 > 160 ? SW / 12 : 160 ))
qdlocker_assert_color_absent_in_crop "$locked" '#ff00ff' "${SW}x${edge_h}+0+0" top-edge
qdlocker_assert_color_absent_in_crop "$locked" '#ff00ff' "${edge_w}x${SH}+0+0" left-edge
qdlocker_assert_color_absent_in_crop "$locked" '#ff00ff' "${edge_w}x${edge_h}+0+0" top-left-corner
qdlocker_assert_color_absent_in_crop "$locked" '#ff00ff' "${SW}x${SH}+0+0" whole-screen

echo "PASS: qdlocker fully occluded sentinel desktop"
echo "pre=$pre"
echo "locked=$locked"
