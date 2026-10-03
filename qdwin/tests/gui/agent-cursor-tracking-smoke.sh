#!/usr/bin/env bash
# Deterministic smoke: QMP pointer motion across qdshell surfaces makes qdwin
# (re)install a VISIBLE cursor sprite (nonzero alpha) on the cursor plane at
# each pointer-enter transition (bar, wallpaper).
#
# Executable replacement for tests/integration/qdwin-noctalia/
# 04-cursor-tracking.md (visual:none; its load-bearing asserts were already
# compositor-journal reads — virsh/shell capture cannot see the hardware cursor
# plane). Same assertions, using the scenario's own journal predicates from
# noctalia-helpers.sh (noct_wait_cursor_layer_nonzero_alpha /
# cursor_layer_nonzero_alpha_after: `mapped on cursor_layer ... nonzero_alpha>0`,
# cursor-scoped):
#   pre the default sprite was `cursor-sprite registered shape=default` at boot
#   1.1 bar -> wallpaper: the wallpaper's `cursor-shape install shape=default:
#       mapped on cursor_layer` line with nonzero alpha
#   2.1 wallpaper -> bar clock area: a nonzero-alpha cursor_layer remap
#   3.1 a sweep across the bar: a nonzero-alpha cursor_layer remap
#   3.2 qdshell still healthy; 3.3 no protocol errors since the step-1 cursor
# Coordinates are the scenario's, authored for the 1280x800 GUI profile; the
# smoke refuses to run (SETUP) on another output size rather than move
# off-screen and fail for the wrong reason.
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
NOCT_HELPERS=${NOCT_HELPERS:-$QDWIN_WORKSPACE/tests/integration/qdwin-noctalia/noctalia-helpers.sh}
# shellcheck source=/dev/null
source "$NOCT_HELPERS" || js_setup_fail "cannot source $NOCT_HELPERS"

compositor_journal_cursor() {
    js_guest "runuser -l admin -c \"journalctl --user -u qdwin-compositor.service -n0 --show-cursor 2>/dev/null\" | sed -n 's/^-- cursor: //p'" \
        | grep -E '^s=' | tail -1
}

js_require_session
noct_session_healthy || js_setup_fail "qdshell (noctalia) session not healthy"
read -r OW OH < <(js_guest 'for c in /sys/class/drm/card*-*; do [ "$(cat $c/status 2>/dev/null)" = connected ] && { head -1 $c/modes; break; }; done' \
    | grep -oE '^[0-9]+x[0-9]+' | tail -1 | tr x ' ')
[ "${OW:-}x${OH:-}" = "${QDWIN_SCREEN_W}x${QDWIN_SCREEN_H}" ] \
    || js_setup_fail "output is ${OW:-?}x${OH:-?}, the scenario coordinates are authored for ${QDWIN_SCREEN_W}x${QDWIN_SCREEN_H}"

reg=$(js_guest "runuser -l admin -c \"journalctl --user -u qdwin-compositor.service -b --no-pager\" | grep -c 'cursor-sprite registered shape=default'" | grep -E '^[0-9]+$' | tail -1)
[ "${reg:-0}" -ge 1 ] || js_fail "pre: the default cursor sprite was never registered at boot"
js_pass "pre: default cursor sprite registered at boot ($reg)"

CERR=$(js_cursor)

# Step 1 — enter the bar (precondition remap lands BEFORE the step cursor),
# then the wallpaper, whose enter must install the default shape.
CUR_PRE1=$(compositor_journal_cursor); [ -n "$CUR_PRE1" ] || js_setup_fail "no compositor journal cursor"
qdwin_mouse_move 640 15
noct_wait_cursor_layer_nonzero_alpha "$CUR_PRE1" \
    || js_fail "1.0: entering the bar produced no visible cursor remap (step-1 precondition)"
CUR_STEP1=$(compositor_journal_cursor)
qdwin_mouse_move 1000 600
STEP1_SHAPE='cursor-shape install shape=default: mapped on cursor_layer'
noct_wait_cursor_layer_nonzero_alpha "$CUR_STEP1" "" "$STEP1_SHAPE" \
    || js_fail "1.1: no visible wallpaper-enter default-shape install after the move"
[ "$(cursor_layer_nonzero_alpha_after "$CUR_STEP1" "$STEP1_SHAPE")" -ge 1 ] \
    || js_fail "1.1: no visible wallpaper-enter default-shape install after the move"
js_pass "1.1 wallpaper enter installed a visible default cursor"

# Step 2 — onto the bar clock area
CUR_STEP2=$(compositor_journal_cursor)
qdwin_mouse_move 1130 15
noct_wait_cursor_layer_nonzero_alpha "$CUR_STEP2" || js_fail "2.1: cursor not remapped on cursor_layer after the bar hover"
[ "$(cursor_layer_nonzero_alpha_after "$CUR_STEP2")" -ge 1 ] || js_fail "2.1: cursor not mapped on cursor_layer after the bar hover"
js_pass "2.1 bar hover keeps a visible sprite"

# Step 3 — sweep across the bar
CUR_STEP3=$(compositor_journal_cursor)
for x in 100 300 500 700 900 1100 1260; do
    qdwin_mouse_move "$x" 15
    sleep 0.2
done
noct_wait_cursor_layer_nonzero_alpha "$CUR_STEP3" || js_fail "3.1: no visible cursor remap during the bar sweep"
[ "$(cursor_layer_nonzero_alpha_after "$CUR_STEP3")" -ge 1 ] || js_fail "3.1: no visible cursor remap during the bar sweep"
js_pass "3.1 sweep remapped a visible sprite"
noct_session_healthy || js_fail "3.2: qdshell not healthy after the sweep"
js_pass "3.2 qdshell healthy"
js_no_protocol_errors "$CERR"
js_pass "3.3 no protocol errors"

echo "PASS: cursor sprite (re)installed visibly on each pointer-enter transition"
