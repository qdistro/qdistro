#!/usr/bin/env bash
# Deterministic smoke: qdshell's Qdwin.closeWindow (reached through the
# supported Quickshell IPC `qs ipc call qdwin closeWindow H`) sends
# qdwin_shell_v1.request_close, qdwin dispatches it, the client exits, and the
# focus-recovery path runs.
#
# Executable replacement for tests/gui/17-qdshell-drives-close.md
# (visual:none). Same mandatory assertions:
#   pre `qs ipc call qdwin capabilities` reports bound=true
#   2.1 `qdwin: request_close handle=H` after the IPC call
#   2.2 `qdwin: toplevel_removed handle=H` (the client exited on xdg close)
#   2.3 `qdwin: seat_focus_changed seat=default handle=4294967295` (or a
#       surviving sibling) on the close (logged in the removal's dispatch)
# The scenario's 3.1 (bar empty-state screenshot) was soft and is not carried.
#
# Exit: 0 pass; 1 assertion failed; 2 setup/environment failure.
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
QDWIN_REPO=${QDWIN_REPO:-$ROOT}
VMNAME=${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -n1)}
export QDWIN_REPO VMNAME
export QDWIN_VIRSH=${QDWIN_VIRSH:-virsh -c qemu:///session}
# shellcheck source=qdwin-helpers.sh
source "$QDWIN_REPO/tests/gui/qdwin-helpers.sh"
qdwin_set_vm "$VMNAME"
# shellcheck source=../lib/journal-smoke.sh
source "$QDWIN_REPO/tests/lib/journal-smoke.sh"

trap js_kill_windows EXIT
js_require_session
caps=""
for _ in $(seq 1 20); do
    caps=$(js_qs_ipc capabilities)
    printf '%s' "$caps" | grep -q 'bound=true' && break
    sleep 1
done
printf '%s' "$caps" | grep -q 'bound=true' \
    || js_setup_fail "qs ipc bridge or qdwin binding not reachable (capabilities: $(printf '%s' "$caps" | tr '\n' ' '))"
js_kill_windows
sleep 1

C1=$(js_cursor); [ -n "$C1" ] || js_setup_fail "no journal cursor"
js_spawn_window qd17-target
read -r H P < <(js_window_handles "$C1" 15 1 | head -1)
[ -n "${H:-}" ] || js_setup_fail "the close target never mapped (no toplevel_added)"
sleep 1

C2=$(js_cursor)
js_qs_ipc closeWindow "$H" >/dev/null
js_wait "$C2" "qdwin: request_close handle=$H( |\$)" 5 >/dev/null \
    || js_fail "2.1: no 'qdwin: request_close handle=$H' after 'qs ipc call qdwin closeWindow $H'; got: $(js_after "$C2" | grep 'request_close' | tail -3)"
rc_line=$(js_after "$C2" | grep -oE "qdwin: request_close handle=$H( .*)?\$" | tail -1)
case "$rc_line" in
    *REFUSED*|*unknown*) js_fail "2.1: compositor did not dispatch the close: '$rc_line'" ;;
esac
js_pass "2.1 ${rc_line}"
js_wait "$C2" "qdwin: toplevel_removed handle=$H\$" 5 >/dev/null \
    || js_fail "2.2: no 'qdwin: toplevel_removed handle=$H' after the shell-driven close"
js_pass "2.2 toplevel_removed handle=$H"
js_guest "kill -0 $P 2>/dev/null && echo ALIVE" | grep -qx ALIVE \
    && js_fail "2.2: qdistro-test-window pid=$P is still running after the shell-driven close"
# The focus drop is logged in the same dispatch as the removal (qdwin emits
# `focus/seat_focus_changed` just BEFORE `toplevel_removed`), so read every
# seat_focus_changed after the request_close and require one that moved focus
# OFF the closed handle (4294967295, or a surviving sibling).
sfc=""
for _ in 1 2 3 4; do
    sfc=$(js_after "$C2" | sed -n "/qdwin: request_close handle=$H /,\$p" \
        | grep -oE 'qdwin: seat_focus_changed seat=default handle=[0-9]+$' | grep -v "handle=$H\$" | tail -1)
    [ -n "$sfc" ] && break
    sleep 0.5
done
[ -n "$sfc" ] || js_fail "2.3: no seat_focus_changed off handle=$H after the shell-driven close (focus recovery did not run)"
js_pass "2.3 ${sfc}"

echo "PASS: qdshell drives request_close end-to-end"
