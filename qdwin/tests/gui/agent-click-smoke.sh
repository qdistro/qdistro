#!/usr/bin/env bash
set -euo pipefail

# Agent/CI smoke test for the qdwin real mouse path.
#
# This intentionally does not depend on qdshell's historical ctrl socket.
# Current qdshell is Quickshell IPC based, while qdwin's click-to-focus path
# is below the shell and is observable through qdwin's journal focus logs.

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
QDWIN_REPO=${QDWIN_REPO:-$ROOT}
VMNAME=${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -n1)}

export QDWIN_REPO
export QDWIN_VIRSH=${QDWIN_VIRSH:-virsh -c qemu:///session}

# shellcheck source=qdwin-helpers.sh
source "$QDWIN_REPO/tests/gui/qdwin-helpers.sh"
qdwin_set_vm "$VMNAME"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

pass() {
    echo "PASS: $*"
}

vm_exec() {
    "$QDWIN_VM_EXEC" "$VMNAME" "$@"
}

journal_cursor() {
    vm_exec "journalctl _UID=1000 -n 1 --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'"
}

journal_after() {
    local cursor=$1
    vm_exec "journalctl _UID=1000 --after-cursor='$cursor' --no-pager 2>/dev/null"
}

detect_screen_size() {
    local tmp=/tmp/qdwin-agent-screen-$$.ppm
    $QDWIN_VIRSH screenshot "$VMNAME" "$tmp" >/dev/null
    local dims
    dims=$(file -b "$tmp" | sed -n 's/.* \([0-9][0-9]*\) x \([0-9][0-9]*\).*/\1 \2/p')
    if [ -z "$dims" ]; then
        dims=$(awk '
            /^#/ { next }
            NR == 1 { next }
            NF >= 2 { print $1, $2; exit }
        ' "$tmp")
    fi
    rm -f "$tmp"
    [ -n "$dims" ] || fail "could not read screenshot dimensions"
    echo "$dims"
}

wait_for_new_handles() {
    local cursor=$1 handles=
    for _ in $(seq 1 30); do
        handles=$(journal_after "$cursor" \
            | sed -n 's/.*qdwin: toplevel_added handle=\([0-9][0-9]*\) uid=1000 pid=[0-9][0-9]* app_id=qdistro-test-window.*/\1/p' \
            | head -n2 \
            | tr '\n' ' ')
        if [ "$(wc -w <<<"$handles")" -ge 2 ]; then
            echo "$handles"
            return 0
        fi
        sleep 0.2
    done
    return 1
}

# Read the actual placement (global content top-left) qdwin logged for a
# handle: "qdwin: mapped handle=H size=WxH pos=X,Y (...)". Echoes "X Y W H".
# This makes the smoke independent of qdwin's placement policy (center /
# smart / cascade) — it clicks where the window actually landed rather than
# re-deriving the placement math. Retries because the mapped line lands on
# the buffer commit, shortly after toplevel_added.
wait_for_geometry() {
    local handle=$1 cursor=$2 line=
    for _ in $(seq 1 30); do
        line=$(journal_after "$cursor" \
            | sed -n "s/.*qdwin: mapped handle=$handle size=\\([0-9]*\\)x\\([0-9]*\\) pos=\\(-\\?[0-9]*\\),\\(-\\?[0-9]*\\) .*/\\3 \\4 \\1 \\2/p" \
            | tail -n1)
        if [ -n "$line" ]; then
            echo "$line"
            return 0
        fi
        sleep 0.2
    done
    return 1
}

wait_for_focus() {
    local handle=$1 cursor=$2
    for _ in $(seq 1 20); do
        if journal_after "$cursor" | grep -q "qdwin: focus handle=$handle "; then
            return 0
        fi
        sleep 0.2
    done
    return 1
}

# Dump the deciding journal lines for a click-to-focus failure: the
# position-based pick (qdwin.c click-focus pick), the low-level button
# notify, and any focus transitions. Distinguishes a dropped QMP button
# event (no pick/notify line) from a downstream focus/emit bug (pick line
# present, no focus transition). See /tmp/opus-analysis-round1.md §1/§2.
dump_click_diag() {
    local cursor=$1
    echo "---- click-focus diagnostics (after cursor) ----" >&2
    journal_after "$cursor" \
        | grep -E 'click-focus pick|notify_button|qdwin: focus handle=|toplevel_added' \
        | tail -20 >&2 || true
    echo "------------------------------------------------" >&2
}

[ -n "$VMNAME" ] || fail "no running VM; set VMNAME or start qdistro-daily"
$QDWIN_VIRSH domstate "$VMNAME" >/dev/null || fail "VM '$VMNAME' not found"
vm_exec "test -S /run/user/1000/wayland-1" >/dev/null \
    || fail "admin qdwin Wayland socket is not up"
vm_exec "command -v qdistro-test-window" >/dev/null \
    || fail "qdistro-test-window is not installed in the VM"

read -r QDWIN_SCREEN_W QDWIN_SCREEN_H < <(detect_screen_size)
export QDWIN_SCREEN_W QDWIN_SCREEN_H
pass "screen ${QDWIN_SCREEN_W}x${QDWIN_SCREEN_H}"

run_id=$$
title_one="agent-click-one-$run_id"
title_two="agent-click-two-$run_id"

cleanup() {
    vm_exec "pkill -KILL -u admin -f '[q]distro-test-window --title agent-click-' 2>/dev/null || true" >/dev/null || true
}
trap cleanup EXIT
cleanup

cursor_start=$(journal_cursor)
# setsid -f: detach so the client survives vm_exec returning (a bare `&` gets
# SIGHUP'd when the agent command exits, so the toplevel never reaches qdwin).
vm_exec "setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 qdistro-test-window --title '$title_one' --width 520 --height 360 --color 0xff203060 >/tmp/$title_one.log 2>&1"
sleep 0.8
vm_exec "setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 qdistro-test-window --title '$title_two' --width 300 --height 180 --color 0xff604020 >/tmp/$title_two.log 2>&1"

read -r handle_one handle_two < <(wait_for_new_handles "$cursor_start") \
    || fail "test windows did not appear in qdwin journal"
pass "windows handle_one=$handle_one handle_two=$handle_two"

# Resolve the windows' ACTUAL placement from qdwin's journal rather than
# assuming a center/cascade layout. qdwin's live WM policy is "smart", which
# tiles windows to avoid overlap, so the second window is NOT stacked on the
# first — clicking an assumed center would land on the wrong window.
read -r one_x one_y one_w one_h < <(wait_for_geometry "$handle_one" "$cursor_start") \
    || fail "did not observe placement (mapped … pos=) for handle=$handle_one"
read -r two_x two_y two_w two_h < <(wait_for_geometry "$handle_two" "$cursor_start") \
    || fail "did not observe placement (mapped … pos=) for handle=$handle_two"
echo "geometry: handle_one=${one_w}x${one_h}@${one_x},${one_y} handle_two=${two_w}x${two_h}@${two_x},${two_y}"

# Click the center of the first/background window through the QMP mouse path.
cursor_click_one=$(journal_cursor)
qdwin_click "$(( one_x + one_w / 2 ))" "$(( one_y + one_h / 2 ))"
wait_for_focus "$handle_one" "$cursor_click_one" || {
    dump_click_diag "$cursor_click_one"
    fail "click on first/background window did not focus handle=$handle_one"
}
pass "background-window click focused handle=$handle_one"

# Click the center of the second window to prove focus can move back through
# the same QMP mouse path.
cursor_click_two=$(journal_cursor)
qdwin_click "$(( two_x + two_w / 2 ))" "$(( two_y + two_h / 2 ))"
wait_for_focus "$handle_two" "$cursor_click_two" || {
    dump_click_diag "$cursor_click_two"
    fail "click on second window did not focus handle=$handle_two"
}
pass "foreground-window click focused handle=$handle_two"

# The user-facing launcher icon is qdshell-owned UI, not the compositor's
# Ctrl+Space launcher keybinding. Keep the probe visible for agents, but do
# not fail the core click-to-focus CI smoke unless explicitly requested.
cursor_launcher=$(journal_cursor)
qdwin_click 16 16
sleep 0.5
if journal_after "$cursor_launcher" | grep -q "qdwin: launcher_requested"; then
    pass "launcher click emitted qdwin: launcher_requested"
elif [ "${QDWIN_REQUIRE_LAUNCHER_CLICK:-0}" = 1 ]; then
    fail "top-left launcher click did not emit qdwin: launcher_requested"
else
    echo "GAP: top-left launcher click did not emit qdwin: launcher_requested"
fi

# Close the launcher overlay the probe just opened. The smokes share one
# session VM; a leaked open launcher occludes the center of the screen and
# broke the shell-capture smoke's mutation assert downstream (its 480x320
# test window mapped exactly under the launcher panel — zero visible diff).
qdwin_send_key KEY_ESC
sleep 0.5

qdwin_screenshot "/tmp/qdwin-agent-click-smoke-$run_id.png" >/dev/null
pass "artifact /tmp/qdwin-agent-click-smoke-$run_id.png"
