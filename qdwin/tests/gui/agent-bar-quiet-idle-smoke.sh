#!/usr/bin/env bash
# Deterministic smoke: the qdshell bar does not remap-storm while idle.
#
# Executable replacement for tests/gui/14-bar-content-quiet-when-idle.md
# (visual:none). Same assertions against the
# compositor journal:
#   1.1 <= 2 `qdshell-bar-content-<output>` lines over 10 s of idle;
#   2.1 <= 2 such lines over 5 s of idle after a window open/close cycle.
# Before todo/qdshell-bar-remap-storm.md, `qdwin: layer-shell mapped` fired
# on every bar commit (~600 lines / 10 s).
# Added precondition (the scenario's "N is 0 -> the bar might be hidden"
# caveat, made mechanical): the bar-content layer surface IS mapped in this
# boot, so a zero count cannot pass vacuously.
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

IDLE_S=${QDWIN_BAR_IDLE_S:-10}
MAX_LINES=${QDWIN_BAR_IDLE_MAX:-2}

trap js_kill_windows EXIT
js_require_session
js_kill_windows

# The bar-content namespace of the live session (output name included).
NS=$(js_guest "journalctl -b _UID=1000 _SYSTEMD_USER_UNIT=qdwin-compositor.service --no-pager -o cat 2>/dev/null \
  | grep -oE 'layer-shell mapped ns=qdshell-bar-content-[A-Za-z0-9_-]+' | sort -u" | sed -n 's/.*ns=//p')
# The DRM output's bar (the scenario's qdshell-bar-content-Virtual-1); the
# pipewire-N outputs are virtual capture/remote outputs.
NS=$(printf '%s\n' "$NS" | grep -m1 -- '-Virtual-' || printf '%s\n' "$NS" | head -1)
[ -n "$NS" ] || js_setup_fail "no qdshell-bar-content layer surface was ever mapped in this compositor session (bar hidden or qdshell not up); an idle count of 0 would be vacuous"
js_pass "precondition: bar-content layer surface '$NS' is mapped"

sleep 2   # let the session settle
C1=$(js_cursor); [ -n "$C1" ] || js_setup_fail "no journal cursor"
sleep "$IDLE_S"
N1=$(js_count "$C1" "$NS")
echo "bar-content lines in ${IDLE_S}s idle: ${N1:-?}"
[ -n "$N1" ] || js_setup_fail "could not count journal lines"
[ "$N1" -le "$MAX_LINES" ] || js_fail "1.1: $N1 '$NS' lines in ${IDLE_S}s of idle (max $MAX_LINES): remap storm; sample: $(js_after "$C1" | grep -F "$NS" | head -3)"
js_pass "1.1 bar quiet while idle ($N1 <= $MAX_LINES lines in ${IDLE_S}s)"

C0=$(js_cursor)
js_spawn_window qd14-cycle
js_window_handles "$C0" 15 1 >/dev/null || js_setup_fail "cycle window never reached qdwin"
sleep 1
js_kill_windows
sleep 2
C2=$(js_cursor)
sleep 5
N2=$(js_count "$C2" "$NS")
echo "bar-content lines in 5s idle after a window cycle: ${N2:-?}"
[ -n "$N2" ] || js_setup_fail "could not count journal lines"
[ "$N2" -le "$MAX_LINES" ] || js_fail "2.1: $N2 '$NS' lines in 5s after a window cycle (max $MAX_LINES): the bar did not re-settle"
js_pass "2.1 bar re-settled after a window cycle ($N2 <= $MAX_LINES lines in 5s)"

echo "PASS: qdshell bar-content quiet when idle"
