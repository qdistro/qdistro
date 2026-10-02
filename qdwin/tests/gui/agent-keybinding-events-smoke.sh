#!/usr/bin/env bash
# Deterministic smoke: every compositor keybinding logs a `qdwin: <event>` line
# independent of shell binding state (the silent-drop bug class).
#
# Executable replacement for tests/gui/15-keybinding-events.md (visual:none).
# Same required assertions, keys injected through QMP (qdwin_chord — real
# key-down/key-up ordering, which weston's modifier bindings need):
#   1.1 Ctrl+Space -> `qdwin: launcher_requested`
#   2.1 Alt+Tab over two windows -> `qdwin: switcher_next dir=1` AND
#       `qdwin: switcher_commit cause=...`
#   3.1 Ctrl+Alt+L -> exactly one of `qdwin: lock_requested` /
#       `qdwin: lock key pressed; ...` (whichever branch is wired)
#   C.1 the session is left UNLOCKED: qdlocker's own introspection reads
#       `locked=False` after cleanup (or the image has no qdlocker at all).
# (4.1, the registered-hotkey press, was optional in the scenario and needs a
# registering test shell; the register_hotkey path is covered by
# agent-wm-policy-bystander-smoke.sh.)
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
QDLOCKER_REPO=${QDLOCKER_REPO:-$QDWIN_WORKSPACE/qdlocker}
# shellcheck source=/dev/null
source "$QDLOCKER_REPO/tests/gui/qdlocker-helpers.sh" \
    || js_setup_fail "cannot source qdlocker-helpers.sh from $QDLOCKER_REPO"

HAVE_LOCKER=1
if js_guest 'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show qdlocker.service -p LoadState --value' \
        | grep -qx not-found; then
    HAVE_LOCKER=0
fi

cleanup_ok=1
cleanup() {
    local rc=$?
    js_kill_windows
    if [ "$HAVE_LOCKER" = 1 ]; then
        qdlocker_drain_lock_state >/dev/null 2>&1 || true
        local st=""
        for _ in $(seq 1 20); do
            st=$(qdlocker_ctrl status 2>/dev/null || true)
            case "$st" in *locked=*) break ;; esac
            sleep 0.5
        done
        case "$st" in
            *locked=False*) echo "C.1 ok: session left UNLOCKED (locked=False)" ;;
            *locked=True*) echo "FAIL: C.1: the session is STILL LOCKED after cleanup" >&2; cleanup_ok=0 ;;
            *) echo "FAIL: C.1: qdlocker is installed but its ctrl socket did not answer; unlocked state UNVERIFIED" >&2; cleanup_ok=0 ;;
        esac
    else
        echo "C.1 n/a: no qdlocker.service on this image"
    fi
    if [ "$cleanup_ok" != 1 ] && [ "$rc" = 0 ]; then
        exit 1
    fi
    exit "$rc"
}
trap cleanup EXIT

js_require_session
if [ "$HAVE_LOCKER" = 1 ]; then
    qdlocker_enable_introspection >/dev/null || js_setup_fail "could not enable qdlocker ctrl introspection"
    # A previous lock routes every key to the locker as overlay_key role=2 and
    # suppresses keybindings: drain it first (not a keybinding failure).
    qdlocker_drain_lock_state || js_setup_fail "could not drain a stale qdlocker lock"
fi
js_kill_windows
qdwin_release_modifiers
sleep 1

# 1.1 Ctrl+Space -> launcher_requested
C1=$(js_cursor); [ -n "$C1" ] || js_setup_fail "no journal cursor"
qdwin_chord ctrl -- spc
sleep 0.5
qdwin_send_key KEY_ESC
js_wait "$C1" 'qdwin: launcher_requested' 5 >/dev/null \
    || js_fail "1.1: Ctrl+Space produced no 'qdwin: launcher_requested'; got: $(js_after "$C1" | grep 'qdwin:' | tail -5)"
js_pass "1.1 Ctrl+Space -> launcher_requested"

# 2.1 Alt+Tab with two windows -> switcher_next dir=1 + switcher_commit cause=
C0=$(js_cursor)
js_spawn_window qd15-switch-1
sleep 1
js_spawn_window qd15-switch-2 0xff405060
js_window_handles "$C0" 15 2 >/dev/null || js_setup_fail "the two switcher windows never reached qdwin"
sleep 1
C2=$(js_cursor)
qdwin_chord alt -- tab
js_wait "$C2" 'qdwin: switcher_next dir=1' 5 >/dev/null \
    || js_fail "2.1: Alt+Tab produced no 'qdwin: switcher_next dir=1'; got: $(js_after "$C2" | grep 'qdwin:' | tail -5)"
commit=$(js_wait "$C2" 'qdwin: switcher_commit cause=[a-z-]+' 5 | grep -oE 'switcher_commit cause=[a-z-]+' | tail -1)
[ -n "$commit" ] || js_fail "2.1: Alt release produced no 'qdwin: switcher_commit cause=...'"
js_pass "2.1 Alt+Tab -> switcher_next dir=1 + $commit"
js_kill_windows
sleep 1

# 3.1 Ctrl+Alt+L -> exactly one lock-branch line
C3=$(js_cursor)
qdwin_chord ctrl alt -- l
js_wait "$C3" 'qdwin: lock_requested|qdwin: lock key pressed' 5 >/dev/null || true
sleep 0.6
n=$(js_count "$C3" 'qdwin: lock_requested|qdwin: lock key pressed')
line=$(js_after "$C3" | grep -oE 'qdwin: (lock_requested|lock key pressed).*' | tail -1)
[ "${n:-0}" -eq 1 ] || js_fail "3.1: expected exactly one lock keybinding line after Ctrl+Alt+L, got ${n:-0}: ${line:-<none>}"
js_pass "3.1 Ctrl+Alt+L -> ${line}"

echo "PASS: compositor keybindings emit journal events (C.1 verified on exit)"
