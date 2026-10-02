#!/usr/bin/env bash
# Deterministic smoke: qdwin logs a `qdwin: focus handle=N (was M) seat=...`
# ground-truth line on EVERY keyboard-focus transition between toplevels:
# spawn, second spawn, close of the focused window, close of the last window.
#
# Executable replacement for tests/gui/13-focus-events-emitted.md (visual:none;
# it was content-skipped as legacy on every run because of one qdwin_ctrl
# "list" call, and it needed foot, which lean goldens do not carry). Same
# assertions, against the live compositor journal, with the baked
# qdistro-test-window as the client:
#   2.1 first spawn  -> `focus handle=H1 (was 4294967295)`
#   3.1 second spawn -> latest focus line `focus handle=H2 (was H1)`
#   4.1 kill H2      -> a new focus line, handle H1 or 4294967295
#   5.1 kill H1      -> focus ends at 4294967295 (a line `(was H1)` when 4.1
#                       transferred focus to H1)
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
js_kill_windows
sleep 1

FOCUS_RE='qdwin: focus handle=[0-9]+ \(was [0-9]+\)'
NONE=4294967295

C0=$(js_cursor); [ -n "$C0" ] || js_setup_fail "no journal cursor"
js_spawn_window qd-focus-1
read -r H1 P1 < <(js_window_handles "$C0" 15 1 | head -1)
[ -n "${H1:-}" ] || js_setup_fail "first qdistro-test-window never reached qdwin (no toplevel_added)"
js_wait "$C0" "qdwin: focus handle=$H1 \\(was $NONE\\)" 10 >/dev/null \
    || js_fail "2.1: no 'qdwin: focus handle=$H1 (was $NONE)' after the first spawn; got: $(js_after "$C0" | grep -E "$FOCUS_RE" | tail -3)"
js_pass "2.1 spawn of handle=$H1 logged focus handle=$H1 (was $NONE)"

C1=$(js_cursor)
js_spawn_window qd-focus-2 0xff405060
read -r H2 P2 < <(js_window_handles "$C1" 15 1 | head -1)
[ -n "${H2:-}" ] || js_setup_fail "second qdistro-test-window never reached qdwin"
js_wait "$C1" "qdwin: focus handle=$H2 \\(was $H1\\)" 10 >/dev/null || true
last=$(js_after "$C1" | grep -E "$FOCUS_RE" | tail -1)
case "$last" in
    *"focus handle=$H2 (was $H1)"*) js_pass "3.1 latest focus line: handle=$H2 (was $H1)" ;;
    *) js_fail "3.1: latest focus line after the second spawn is not 'handle=$H2 (was $H1)': '${last:-<none>}'" ;;
esac

C2=$(js_cursor)
js_guest "kill -9 $P2" >/dev/null
step4=$(js_wait "$C2" "qdwin: focus handle=($H1|$NONE) \\(was $H2\\)" 10 | grep -E "$FOCUS_RE" | tail -1)
[ -n "$step4" ] || js_fail "4.1: no new focus line (handle $H1 or $NONE, was $H2) after killing handle=$H2; got: $(js_after "$C2" | grep -E "$FOCUS_RE" | tail -3)"
js_pass "4.1 closing the focused window logged: ${step4##*qdwin: }"

C3=$(js_cursor)
js_guest "kill -9 $P1" >/dev/null
js_wait "$C3" "qdwin: toplevel_removed handle=$H1\$" 10 >/dev/null \
    || js_setup_fail "handle=$H1 was never removed after kill -9 $P1"
case "$step4" in
    *"focus handle=$H1 "*)
        js_wait "$C3" "qdwin: focus handle=$NONE \\(was $H1\\)" 10 >/dev/null \
            || js_fail "5.1: focus stayed on destroyed handle=$H1; no 'focus handle=$NONE (was $H1)'; got: $(js_after "$C3" | grep -E "$FOCUS_RE" | tail -3)"
        ;;
esac
sleep 1
final=$(js_after "$C0" | grep -E "$FOCUS_RE" | tail -1)
case "$final" in
    *"focus handle=$NONE "*) js_pass "5.1 focus ended at $NONE after the last window closed: ${final##*qdwin: }" ;;
    *) js_fail "5.1: final focus line is not handle=$NONE: '${final:-<none>}'" ;;
esac

echo "PASS: qdwin focus events emitted for spawn/spawn/close/close"
