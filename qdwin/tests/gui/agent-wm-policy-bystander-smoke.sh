#!/usr/bin/env bash
# Deterministic smoke: the v25 window-manager-policy surface is ENACTED by the
# compositor on a real client (set_wm_policy, request_tile left/none/right,
# request_fullscreen on/off, register_hotkey), driven by qdwin-bystander as the
# shell so the proof does not depend on qdshell's init order.
#
# Executable replacement for tests/gui/21-wm-policy-bystander.md (visual:none,
# "all asserts are deterministic journal reads"). Same mandatory assertions,
# with the output size read from the DRM connector instead of assuming
# 1920x1080 (the scenario's own note: assert half/full of the real output):
#   2.1 `qdwin: set_wm_policy focus=1 ffm_delay=250 raise_click=1 raise_hover=0 placement=2 snap=1 dist=24`
#   2.2 `qdwin: tile handle=H edge=left outer=(W/2)xH' at (0,0)`
#   2.3 `qdwin: tile handle=H restored ...`
#   2.4 `qdwin: tile handle=H edge=right outer=(W/2)xH' at (W/2,0)`
#   2.5 `qdwin: set_fullscreen handle=H fs=1 outer=WxH' at (0,0)` then `fs=0 restored=`
#   2.6 `qdwin: register_hotkey id=7101 mods=0x2 key=62`
#   2.7 no protocol error (compositor journal + bystander log)
# (H' = the output height; the bystander shell maps no panel, so the work area
# is the full output.)
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
# shellcheck source=../apps/qdwin-apps-helpers.sh
source "$QDWIN_REPO/tests/apps/qdwin-apps-helpers.sh"
qdwin_apps_set_vm "$VMNAME"
# shellcheck source=../lib/journal-smoke.sh
source "$QDWIN_REPO/tests/lib/journal-smoke.sh"

js_require_session
js_guest 'command -v qdwin-bystander >/dev/null && echo OK' | grep -qx OK \
    || js_setup_fail "qdwin-bystander not installed on the VM"

# Output size: the active mode of the connected DRM connector.
read -r OW OH < <(js_guest 'for c in /sys/class/drm/card*-*; do [ "$(cat $c/status 2>/dev/null)" = connected ] && { head -1 $c/modes; break; }; done' \
    | grep -oE '^[0-9]+x[0-9]+' | tail -1 | tr x ' ')
[ -n "${OW:-}" ] && [ -n "${OH:-}" ] || js_setup_fail "could not read the connected DRM connector's mode"
HW=$((OW / 2))
echo "output ${OW}x${OH}"

js_kill_windows
qdwin_apps_become_shell >/dev/null || { qdwin_apps_restore_shell >/dev/null 2>&1; js_setup_fail "could not take over the shell role with qdwin-bystander"; }
trap 'js_kill_windows; qdwin_apps_restore_shell >/dev/null 2>&1 || echo "WARN: qdshell restore failed" >&2' EXIT
qdwin_apps_session_up >/dev/null || js_setup_fail "bystander session not healthy"

qdwin_apps_launch qd21-target "qdistro-test-window --title qd21-target --width 400 --height 260 --color 0xff304050" >/dev/null
H=""
for _ in $(seq 1 30); do
    H=$(js_guest "grep -E 'qdwin-bystander: toplevel_added handle=[0-9]+ .*app_id=\"qdistro-test-window\"' $QDWIN_BYSTANDER_LOG 2>/dev/null | tail -1 | sed -nE 's/.*handle=([0-9]+).*/\\1/p'" | grep -E '^[0-9]+$' | tail -1)
    [ -n "$H" ] && break
    sleep 0.3
done
[ -n "$H" ] || js_setup_fail "1.0: the bystander never saw the test window"
echo "target handle=$H"

C=$(js_cursor); [ -n "$C" ] || js_setup_fail "no journal cursor"
for cmd in "wmpolicy 1 250 1 0 2 1 24" "tile $H left" "tile $H none" "tile $H right" \
           "fullscreen $H 1" "fullscreen $H 0" "hotkey 7101 2 62"; do
    qdwin_apps_ctl $cmd >/dev/null || js_setup_fail "FIFO write '$cmd' failed"
    sleep 0.5
done
js_wait "$C" 'qdwin: register_hotkey id=7101' 5 >/dev/null || true
LOGS=$(js_after "$C")

need() {  # need <label> <ERE>
    printf '%s\n' "$LOGS" | grep -qE -- "$2" \
        || js_fail "$1: missing /$2/ in the compositor journal; got: $(printf '%s\n' "$LOGS" | grep -E 'qdwin: (set_wm_policy|tile|set_fullscreen|register_hotkey)' | tr '\n' '|')"
    js_pass "$1 $(printf '%s\n' "$LOGS" | grep -oE -- "$2" | head -1)"
}
need 2.1 'qdwin: set_wm_policy focus=1 ffm_delay=250 raise_click=1 raise_hover=0 placement=2 snap=1 dist=24'
need 2.2 "qdwin: tile handle=$H edge=left outer=${HW}x${OH} at \\(0,0\\)"
need 2.3 "qdwin: tile handle=$H restored [0-9]+x[0-9]+@\\(-?[0-9]+,-?[0-9]+\\)"
need 2.4 "qdwin: tile handle=$H edge=right outer=${HW}x${OH} at \\(${HW},0\\)"
need 2.5a "qdwin: set_fullscreen handle=$H fs=1 outer=${OW}x${OH} at \\(0,0\\)"
need 2.5b "qdwin: set_fullscreen handle=$H fs=0 restored="
need 2.6 'qdwin: register_hotkey id=7101 mods=0x2 key=62'
# Order: left -> restored -> right -> fs=1 -> fs=0 (the sequence was serial).
order=$(printf '%s\n' "$LOGS" | grep -oE "tile handle=$H (edge=left|restored|edge=right)|set_fullscreen handle=$H fs=[01]" | sed -E 's/.*(edge=left|restored|edge=right|fs=[01])/\1/' | tr '\n' ' ')
case "$order" in
    "edge=left restored edge=right fs=1 fs=0 "*) js_pass "sequence order: $order" ;;
    *) js_fail "the tile/fullscreen sequence was not enacted in order: '$order'" ;;
esac
js_no_protocol_errors "$C"
js_guest "grep -c 'protocol error' $QDWIN_BYSTANDER_LOG 2>/dev/null || true" | grep -qx 0 \
    || js_fail "2.7: the bystander logged a protocol error"
js_pass "2.7 no protocol errors"

echo "PASS: v25 wm-policy/tile/fullscreen/hotkey enacted by the compositor"
