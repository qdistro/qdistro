#!/usr/bin/env bash
# Deterministic smoke: the v26 idle/DPMS live-apply surface.
#
# Executable replacement for tests/gui/20-idle-dpms.md (visual:none). Same
# mandatory assertions:
#   A.0 `qs ipc call qdwin capabilities` reports bound=true, version >= 26
#   A.1 ... and idleDpms=true (>= v26 bind AND ext_idle_notifier_v1 + wl_seat
#       bound by the qml-plugin — the CapabilityService state the Power tab's
#       live idle policy rides on)
#   B.1 with qdwin-bystander holding the shell role, `displaypower 0` then
#       `displaypower 1` log `qdwin: set_display_power on=0 (N output...)` then
#       `on=1` in the compositor journal
#   B.2 no `qdwin_shell_v1 ... protocol error` in that window
# qdshell is restored (and its re-bind proven) on every exit path by
# qdwin_apps_restore_shell. The timed idle trigger itself is covered by
# agent-idle-dpms-recovery-smoke.sh.
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

# Path A — capability gate
caps="" ver=""
for _ in $(seq 1 30); do
    caps=$(js_qs_ipc capabilities | grep -E 'bound=' | tail -1)
    ver=$(printf '%s' "$caps" | sed -nE 's/.*version=([0-9]+).*/\1/p')
    case "$caps" in *bound=true*) [ -n "$ver" ] && [ "$ver" -ge 26 ] && break ;; esac
    sleep 1
done
echo "capabilities: $caps"
case "$caps" in *bound=true*) ;; *) js_setup_fail "A.0: qdshell IPC never reported bound=true ($caps)" ;; esac
[ -n "$ver" ] && [ "$ver" -ge 26 ] || js_fail "A.0: shell bound at version=${ver:-?} (< 26)"
js_pass "A.0 bound=true version=$ver"
printf '%s' "$caps" | grep -q 'idleDpms=' || js_fail "A.1: capabilities lacks the idleDpms field ($caps)"
printf '%s' "$caps" | grep -q 'idleDpms=true' || js_fail "A.1: idleDpms is not true with a v$ver bind ($caps)"
js_pass "A.1 idleDpms=true"

# Path B — compositor functional proof with the bystander as shell
js_guest 'command -v qdwin-bystander >/dev/null && echo OK' | grep -qx OK \
    || js_setup_fail "qdwin-bystander not installed on the VM"
qdwin_apps_become_shell >/dev/null || { qdwin_apps_restore_shell >/dev/null 2>&1; js_setup_fail "could not take over the shell role with qdwin-bystander"; }
trap 'qdwin_apps_restore_shell >/dev/null 2>&1 || echo "WARN: qdshell restore failed" >&2' EXIT
qdwin_apps_session_up >/dev/null || js_setup_fail "bystander session not healthy"

CB=$(js_cursor); [ -n "$CB" ] || js_setup_fail "no journal cursor"
qdwin_apps_ctl displaypower 0 >/dev/null || js_setup_fail "FIFO write 'displaypower 0' failed"
sleep 0.5
qdwin_apps_ctl displaypower 1 >/dev/null || js_setup_fail "FIFO write 'displaypower 1' failed"
lines=$(js_wait "$CB" 'qdwin: set_display_power on=[01] \([0-9]+ output' 10 qdwin-compositor.service 2 \
        | grep -oE 'set_display_power on=[01] \([0-9]+ outputs?\)')
first=$(printf '%s\n' "$lines" | sed -n 1p); second=$(printf '%s\n' "$lines" | sed -n 2p)
case "$first" in "set_display_power on=0 ("[1-9]*) ;; *) js_fail "B.1: expected 'set_display_power on=0 (N>=1 output...)' first, got: '${first:-<none>}'" ;; esac
case "$second" in "set_display_power on=1 ("[1-9]*) ;; *) js_fail "B.1: expected 'set_display_power on=1 (N>=1 output...)' second, got: '${second:-<none>}'" ;; esac
js_pass "B.1 $first then $second"
js_no_protocol_errors "$CB"
js_guest "grep -c 'protocol error' $QDWIN_BYSTANDER_LOG 2>/dev/null || true" | grep -qx 0 \
    || js_fail "B.2: the bystander logged a protocol error: $(js_guest "grep 'protocol error' $QDWIN_BYSTANDER_LOG" | head -2)"
js_pass "B.2 no protocol errors (compositor journal + bystander log)"

echo "PASS: idle/DPMS capability live + compositor set_display_power round-trip"
