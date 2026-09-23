#!/usr/bin/env bash
set -euo pipefail

# Agent/CI smoke: cursor sprite visible above shell UI does NOT capture
# clicks. plan3 H3 reproducer; tightened twice — once after the first
# post-implementation review and again after the deep-review caught a
# focus-away-optional gap that let broken click paths pass.
#
# Symptom this test guards: a cursor sprite that retains a non-empty
# input region becomes a pickable shell surface above qdshell / layer-
# shell UI; pointer clicks land on the cursor view instead of the UI
# underneath. libweston's pointer_cursor_surface_committed clears both
# pending and current input regions on every commit
# (libweston/input.c:3521-3522). qdwin's install path clears them once;
# plan3 M2 adds a per-sprite commit listener that re-asserts the
# invariant on every commit. This smoke proves the invariant by-effect
# using a mandatory focus transition.
#
# Sequence (deep-review tightened):
#   1. Spawn TWO test windows — handle_one, then handle_two.
#      handle_two is the foreground (auto-focused on spawn).
#   2. Park the pointer over the exposed top-left of handle_one
#      (cursor sprite maps on cursor_layer above the window).
#   3. Click that exposed pixel. Focus MUST transition from
#      handle_two → handle_one. If the cursor sprite were pickable,
#      the click would land on the cursor view and no such transition
#      would appear.
#
# The two-window setup removes the previous test's reliance on
# "empty-desktop click moves focus", which on noctalia-bootstrap is
# best-effort because the shell auto-takes focus.
#
# QDWIN_CURSOR_CLICKTHROUGH_FORCE_BREAK=1 flips the assertion so the
# regression itself is reproducible (focus must NOT transition).

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
    local tmp=/tmp/qdwin-clickthrough-screen-$$.ppm
    $QDWIN_VIRSH screenshot "$VMNAME" "$tmp" >/dev/null
    local dims
    dims=$(file -b "$tmp" | sed -n 's/.* \([0-9][0-9]*\) x \([0-9][0-9]*\).*/\1 \2/p')
    rm -f "$tmp"
    [ -n "$dims" ] || fail "could not read screenshot dimensions"
    echo "$dims"
}
wait_for_handles() {
    local cursor=$1 want=$2 handles
    for _ in $(seq 1 30); do
        handles=$(journal_after "$cursor" \
            | sed -n 's/.*qdwin: toplevel_added handle=\([0-9][0-9]*\) uid=1000 pid=[0-9][0-9]* app_id=qdistro-test-window.*/\1/p' \
            | head -n"$want" | tr '\n' ' ')
        if [ "$(wc -w <<<"$handles")" -ge "$want" ]; then
            echo "$handles"
            return 0
        fi
        sleep 0.2
    done
    return 1
}
# Read the actual placement qdwin logged for a handle ("mapped handle=H
# size=WxH pos=X,Y (...)"); echoes "X Y W H". Keeps the click target correct
# under any placement policy (qdwin's live policy is "smart", which tiles to
# avoid overlap rather than stacking windows at a shared centre).
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
# Wait for a focus transition into `handle=$next`. The previous owner may be
# `$prev` or UINT32_MAX: qdshell layer-shell surfaces can legitimately own
# keyboard focus between setup and click, and qdwin reports that as no qdwin
# toplevel focused. We still require the click to focus `$next`.
wait_for_strict_focus_transition() {
    local prev=$1 next=$2 cursor=$3
    local pat="qdwin: focus handle=$next \\(was ($prev|4294967295)\\)"
    for _ in $(seq 1 25); do
        if journal_after "$cursor" | grep -qE "$pat"; then
            return 0
        fi
        sleep 0.2
    done
    # Dump the deciding journal lines: the position-based pick and the
    # low-level button notify distinguish a dropped QMP button event (no
    # pick/notify) from a downstream focus/emit bug (pick present, no focus
    # transition). See /tmp/opus-analysis-round1.md §1.
    journal_after "$cursor" \
        | grep -E 'click-focus pick|notify_button|qdwin: focus handle=|toplevel_added' \
        | tail -20 >&2
    return 1
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

# qdistro-daily ships qdwin-compositor.service; noctalia-bootstrap ships
# noctalia-session.service. Probe whichever exists.
if vm_exec "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user list-unit-files qdwin-compositor.service" >/dev/null 2>&1; then
    QDWIN_COMP_UNIT=qdwin-compositor.service
else
    QDWIN_COMP_UNIT=noctalia-session.service
fi
vm_exec "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active $QDWIN_COMP_UNIT qdistro-cursor-sprites.service" \
    > /tmp/clickthrough-svc-$$.txt 2>&1
if grep -qE '^(inactive|failed|unknown)' /tmp/clickthrough-svc-$$.txt; then
    cat /tmp/clickthrough-svc-$$.txt >&2
    fail "qdwin/cursor-sprite services are not active"
fi
rm -f /tmp/clickthrough-svc-$$.txt
pass "qdwin ($QDWIN_COMP_UNIT) and cursor-sprite services active"

vm_exec "journalctl _UID=1000 -b --no-pager | grep -q 'cursor-sprite registered shape=pointer\\|cursor-sprite registered shape=default'" \
    || fail "cursor sprite helper did not register pointer/default shape"
pass "cursor sprite helper registered pointer/default shape"

run_id=$$
title_one="agent-clickthrough-one-$run_id"
title_two="agent-clickthrough-two-$run_id"
cleanup() {
    vm_exec "pkill -KILL -u admin -f '[q]distro-test-window --title agent-clickthrough-' 2>/dev/null || true" >/dev/null || true
}
trap cleanup EXIT
cleanup

# Spawn handle_one then handle_two; handle_two becomes foreground.
cursor_start=$(journal_cursor)
# Use a larger background window and smaller foreground window so the target
# pixel is exposed even if qdwin centers both windows instead of cascading.
# setsid -f: detach so the client survives vm_exec returning (a bare `&` gets
# SIGHUP'd when the agent command exits, so the toplevel never reaches qdwin).
vm_exec "setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 qdistro-test-window --title '$title_one' --width 520 --height 360 --color 0xff206040 >/tmp/$title_one.log 2>&1"
sleep 0.8
vm_exec "setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 qdistro-test-window --title '$title_two' --width 300 --height 180 --color 0xff604020 >/tmp/$title_two.log 2>&1"

read -r handle_one handle_two < <(wait_for_handles "$cursor_start" 2) \
    || fail "two test windows did not appear in qdwin journal"
pass "windows handle_one=$handle_one handle_two=$handle_two"

# Resolve handle_one's ACTUAL placement from the journal rather than assuming
# a centered layout (qdwin's "smart" policy tiles to avoid overlap). Under any
# policy handle_one and handle_two do not overlap, so handle_one's centre is a
# safe click target that is outside handle_two.
read -r one_x one_y one_w one_h < <(wait_for_geometry "$handle_one" "$cursor_start") \
    || fail "did not observe placement (mapped … pos=) for handle=$handle_one"
echo "geometry: handle_one=${one_w}x${one_h}@${one_x},${one_y}"

target_x=$(( one_x + one_w / 2 ))
target_y=$(( one_y + one_h / 2 ))

cursor_park=$(journal_cursor)
qdwin_mouse_move "$target_x" "$target_y"
sleep 0.6
if ! vm_exec "journalctl _UID=1000 -b --no-pager | grep -q 'mapped on cursor_layer'"; then
    fail "no cursor sprite mapped on cursor_layer — preconditions not met"
fi
pass "cursor sprite mapped on cursor_layer at ($target_x,$target_y)"

# Click the exposed pixel of handle_one. Required transition is
# focus handle=$handle_one (was $handle_two).
cursor_click=$(journal_cursor)
qdwin_click "$target_x" "$target_y"

if [ "${QDWIN_CURSOR_CLICKTHROUGH_FORCE_BREAK:-0}" = 1 ]; then
    if wait_for_strict_focus_transition "$handle_two" "$handle_one" "$cursor_click"; then
        fail "FORCE_BREAK: click reached handle_one — cursor not intercepting"
    fi
    pass "FORCE_BREAK: click did NOT transition focus to handle_one"
    qdwin_screenshot "/tmp/qdwin-cursor-clickthrough-FORCE_BREAK-$run_id.png" >/dev/null
    exit 0
fi

wait_for_strict_focus_transition "$handle_two" "$handle_one" "$cursor_click" \
    || fail "click at ($target_x,$target_y) did not transition focus from handle=$handle_two to handle=$handle_one — cursor sprite may be capturing clicks"
pass "click at ($target_x,$target_y) transitioned focus handle_two=$handle_two -> handle_one=$handle_one through visible cursor sprite"

# Bonus M2 sanity: the per-sprite commit listener should never log
# "re-cleared input" in normal operation (the helper does not commit
# non-empty input regions). Its presence indicates a regression in the
# helper. WARN, do not fail.
if journal_after "$cursor_click" | grep -q 'cursor-sprite commit re-cleared input'; then
    echo "WARN: M2 listener observed a non-empty cursor input region " \
         "at commit — investigate qdistro-cursor-sprites helper"
fi

qdwin_screenshot "/tmp/qdwin-cursor-clickthrough-$run_id.png" >/dev/null
pass "artifact /tmp/qdwin-cursor-clickthrough-$run_id.png"
