#!/usr/bin/env bash
# Deterministic live negative for D7: qdwin_shell_v1.show_popup must refuse a
# fabricated grab serial with `invalid_grab`, map nothing and take no grab.
# Host-side the contract is pinned by qdwin/test_popup_grab_hardening.py;
# this drives the real compositor.
#
# Who can even send show_popup: only the bound shell. So the lane is two
# halves:
#   A  with qdshell holding the role, a same-uid client's qdwin_shell_v1 bind
#      is refused ("shell role already claimed") — a forger never reaches
#      show_popup while the session is up;
#   B  with qdshell stopped, qdwin-probe takes the role (the position a
#      compromised or impersonating shell would be in) and sends show_popup on
#      a real parent with a committed surface and a forged serial: literal 0
#      (the grab_serial of any device that never produced input) and a large
#      fabricated value. Each must be refused with invalid_grab, logged as
#      `show_popup refused: stale/invalid serial=N`, and leave no
#      `show_popup parent=` line; a later press on the desktop must reach the
#      default grab's click-focus pick (no popup grab swallowed it).
# The session is restored (qdshell restarted, test windows killed) on exit.
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

AS_ADMIN='runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1'
user_systemctl() {
    js_guest "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user $*"
}
restore() {
    js_kill_windows
    user_systemctl reset-failed qdshell.service >/dev/null 2>&1 || true
    user_systemctl start qdshell.service >/dev/null 2>&1 || true
    local _
    for _ in $(seq 1 30); do
        js_qs_ipc capabilities | grep -q 'bound=true' && return 0
        sleep 1
    done
    echo "SETUP: qdshell did not rebind within 30s after the smoke; later scenarios on this VM will fail setup" >&2
}
trap restore EXIT

js_require_session
js_guest 'test -x /usr/bin/qdwin-probe && echo PROBE-OK' | grep -qx PROBE-OK \
    || js_setup_fail "/usr/bin/qdwin-probe missing from the golden (qdwin install regressed)"
# An older probe ignores unknown flags and exits 0 hello-only, which would
# read as "no refusal" (a product FAIL) — require the forged-serial mode.
js_guest 'grep -qa -- --show-popup-serial /usr/bin/qdwin-probe && echo MODE-OK' | grep -qx MODE-OK \
    || js_setup_fail "/usr/bin/qdwin-probe lacks --show-popup-serial (stale golden; rebuild from this tree)"
caps=""
for _ in $(seq 1 20); do
    caps=$(js_qs_ipc capabilities)
    printf '%s' "$caps" | grep -q 'bound=true' && break
    sleep 1
done
printf '%s' "$caps" | grep -q 'bound=true' \
    || js_setup_fail "qdshell is not bound to qdwin_shell_v1 (capabilities: $(printf '%s' "$caps" | tr '\n' ' '))"
js_kill_windows

# --- A: the live shell holds the role --------------------------------------
out=$(js_guest "$AS_ADMIN qdwin-probe --timeout=5 2>&1; echo rc=\$?")
rc=$(printf '%s\n' "$out" | sed -n 's/^rc=//p' | tail -1)
[ "$rc" = 1 ] && printf '%s' "$out" | grep -q 'shell role already claimed' \
    || js_fail "A: a same-uid client bound qdwin_shell_v1 while qdshell holds the role (rc=$rc): $(printf '%s' "$out" | tr '\n' ' ')"
js_pass "A same-uid bind refused while qdshell holds the role"

# --- B: forged serials from a client holding the role ----------------------
C1=$(js_cursor); [ -n "$C1" ] || js_setup_fail "no journal cursor"
js_spawn_window qd-forged-parent
read -r H _P < <(js_window_handles "$C1" 15 1 | head -1)
[ -n "${H:-}" ] || js_setup_fail "the parent window never mapped (no toplevel_added)"
user_systemctl stop qdshell.service >/dev/null \
    || js_setup_fail "could not stop qdshell.service"

for serial in 0 2147483632; do
    C2=$(js_cursor)
    out="" rc=""
    # The role frees once the dead shell's client is reaped; retry the bind.
    for _ in $(seq 1 10); do
        out=$(js_guest "$AS_ADMIN qdwin-probe --show-popup-serial=$serial --parent-app-id=qdistro-test-window --timeout=5 2>&1; echo rc=\$?")
        rc=$(printf '%s\n' "$out" | sed -n 's/^rc=//p' | tail -1)
        [ "$rc" = 1 ] || break
        sleep 1
    done
    case "$rc" in
        0) ;;
        4) js_fail "B: show_popup with forged serial=$serial was ACCEPTED: $(printf '%s' "$out" | tr '\n' ' ')" ;;
        5) js_fail "B: show_popup with forged serial=$serial failed with a different error than invalid_grab: $(printf '%s' "$out" | tr '\n' ' ')" ;;
        *) js_setup_fail "B: qdwin-probe could not take the shell role / find the parent (rc=$rc): $(printf '%s' "$out" | tr '\n' ' ')" ;;
    esac
    js_wait "$C2" "qdwin: show_popup refused: stale/invalid serial=$serial\$" 5 >/dev/null \
        || js_fail "B: no 'show_popup refused: stale/invalid serial=$serial' in the compositor journal"
    n=$(js_count "$C2" 'qdwin: show_popup parent=')
    [ "${n:-0}" -eq 0 ] \
        || js_fail "B: a popup was placed for forged serial=$serial ($n 'show_popup parent=' line(s))"
    js_pass "B serial=$serial refused with invalid_grab, nothing placed"
done

# No grab left behind: with qdshell (and its layer surfaces) gone, a press on
# the desktop goes through the default grab's click-focus pick; an installed
# popup grab would swallow it as an outside-click dismiss instead.
C3=$(js_cursor)
qdwin_click 1200 700
js_wait "$C3" 'qdwin: click-focus pick pos=' 5 >/dev/null \
    || js_fail "B: a desktop press after the refusals never reached click-focus (a grab is still installed?)"
n=$(js_count "$C3" 'popup dismissed by outside click')
[ "${n:-0}" -eq 0 ] \
    || js_fail "B: the desktop press was consumed by a popup grab ($n dismiss line(s))"
js_pass "B no popup grab installed: the next press reached the default grab"

echo "PASS: forged show_popup serials are refused live"
