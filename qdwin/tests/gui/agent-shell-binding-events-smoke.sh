#!/usr/bin/env bash
# Deterministic smoke: qdshell binds qdwin_shell_v1 (>= v14) and the protocol
# event stream flows both ways (toplevel_added / seat_focus_changed /
# toplevel_removed reach the shell; the shell's Alt+Tab commit drives focus).
#
# Executable replacement for tests/gui/16-qdshell-binding-protocol-events.md
# (visual:none). Same REQUIRED assertions, from the compositor + qdshell
# journals (each read cursor-scoped and unit-scoped):
#   1.1 `qdwin: bind accepted for uid=1000` after a qdshell restart
#   1.2 qdshell's `Qdwin qdwin_shell_v1 bound vN` with N >= 14
#   2.1 a spawned qdistro-test-window logs `toplevel_added handle=H ...`
#   2.2 `qdwin: seat_focus_changed seat=default handle=H` (emitted only to a
#       bound v14+ shell — the proof the binding exercises the emit branch)
#   4.1 closing it logs `qdwin: toplevel_removed handle=H`
#   4.2 then `qdwin: seat_focus_changed seat=default handle=4294967295`
#   5.1 Alt+Tab over two windows logs `qdwin: switcher_next dir=`
#   5.2 and a `qdwin: focus handle=` line (the shell's onSwitcherCommit called
#       set_keyboard_focus and qdwin honoured it)
# The scenario's 3.1 (bar title OCR) was SOFT/non-blocking and is not carried.
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

NONE=4294967295
trap js_kill_windows EXIT
js_require_session
js_guest 'test -f /usr/share/qdistro/qml/Qdistro/Qdwin/libqdistro-qdwin.so && echo PLUGIN-OK' | grep -qx PLUGIN-OK \
    || js_setup_fail "qdshell QML plugin libqdistro-qdwin.so not installed"
js_kill_windows
sleep 1

# 1.x — cursor BEFORE the restart (the bind lands ~1s after it).
C1=$(js_cursor); [ -n "$C1" ] || js_setup_fail "no journal cursor"
js_guest 'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdshell.service' >/dev/null
js_wait "$C1" 'qdwin: bind accepted for uid=1000' 20 >/dev/null \
    || js_fail "1.1: no 'qdwin: bind accepted for uid=1000' after the qdshell restart"
js_pass "1.1 qdwin: bind accepted for uid=1000"
bound=$(js_wait "$C1" 'Qdwin +qdwin_shell_v1 bound v[0-9]+' 20 qdshell.service | grep -oE 'qdwin_shell_v1 bound v[0-9]+' | tail -1)
ver=${bound##*bound v}
[ -n "$bound" ] || js_fail "1.2: qdshell never logged 'Qdwin qdwin_shell_v1 bound vN' after the restart"
[ "$ver" -ge 14 ] 2>/dev/null || js_fail "1.2: qdshell bound qdwin_shell_v1 at v$ver (< 14)"
js_pass "1.2 qdshell $bound (>= v14)"
# Let the restarted shell settle (layer surfaces mapped) before spawning.
for _ in $(seq 1 20); do
    js_qs_ipc capabilities | grep -q 'bound=true' && break
    sleep 1
done

# 2.x
C2=$(js_cursor)
js_spawn_window qd16-step2
read -r H P < <(js_window_handles "$C2" 15 1 | head -1)
[ -n "${H:-}" ] || js_fail "2.1: no 'qdwin: toplevel_added handle=N uid=1000 pid=N app_id=qdistro-test-window'"
js_pass "2.1 toplevel_added handle=$H pid=$P"
js_wait "$C2" "qdwin: seat_focus_changed seat=default handle=$H\$" 10 >/dev/null \
    || js_fail "2.2: no 'qdwin: seat_focus_changed seat=default handle=$H' (the bound-v14+ emit branch)"
js_pass "2.2 seat_focus_changed seat=default handle=$H"

# 4.x — close by the PID from the toplevel_added line, never pkill -f.
C4=$(js_cursor)
js_guest "kill -TERM $P 2>/dev/null; sleep 0.5; kill -KILL $P 2>/dev/null; true" >/dev/null
js_wait "$C4" "qdwin: toplevel_removed handle=$H\$" 10 >/dev/null \
    || js_fail "4.1: no 'qdwin: toplevel_removed handle=$H' within 10s of killing pid=$P"
js_pass "4.1 toplevel_removed handle=$H"
js_wait "$C4" "qdwin: seat_focus_changed seat=default handle=$NONE\$" 10 >/dev/null \
    || js_fail "4.2: no 'qdwin: seat_focus_changed seat=default handle=$NONE' after the last window closed"
js_pass "4.2 seat_focus_changed seat=default handle=$NONE"

# 5.x
C5a=$(js_cursor)
js_spawn_window qd16-step5a
sleep 1
js_spawn_window qd16-step5b 0xff405060
js_window_handles "$C5a" 15 2 >/dev/null || js_setup_fail "fewer than 2 qdistro-test-window toplevels reached qdwin"
sleep 1
C5=$(js_cursor)
qdwin_chord alt -- tab
js_wait "$C5" 'qdwin: switcher_next dir=' 5 >/dev/null \
    || js_fail "5.1: Alt+Tab produced no 'qdwin: switcher_next dir='"
js_pass "5.1 switcher_next"
js_wait "$C5" 'qdwin: focus handle=' 5 >/dev/null \
    || js_fail "5.2: no 'qdwin: focus handle=' after the Alt+Tab commit (shell onSwitcherCommit did not drive focus)"
js_pass "5.2 focus moved after the switcher commit"

echo "PASS: qdshell <-> qdwin_shell_v1 binding observes and drives the protocol event stream"
