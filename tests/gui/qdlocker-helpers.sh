#!/bin/bash
# qdlocker-helpers.sh — host-side helpers for driving qdlocker on a
# qdistro VM. Sources qdwin's helpers (keyboard injection via QMP,
# screenshot via virsh, qdshell ctrl-socket access) and adds locker-
# specific accessors.
#
# Layout matches qdwin/tests/gui/qdwin-helpers.sh:1-35. Tests under
# tests/gui/ source this file once at top.
#
# Usage:
#     source qdlocker-helpers.sh
#     qdwin_set_vm demo-260515-1200
#     qdlocker_ctrl status
#     qdlocker_wait_for_lock

: "${QDLOCKER_REPO:=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)}"
: "${QDWIN_REPO:=${QDLOCKER_REPO}/../qdwin}"

if [ -f "${QDWIN_REPO}/tests/gui/qdwin-helpers.sh" ]; then
    # shellcheck disable=SC1091
    source "${QDWIN_REPO}/tests/gui/qdwin-helpers.sh"
else
    echo "qdlocker-helpers: cannot find qdwin-helpers.sh at" \
         "${QDWIN_REPO}/tests/gui/qdwin-helpers.sh" >&2
    echo "  set QDWIN_REPO to the qdwin checkout, or check sibling repo layout" >&2
    return 1
fi

# ---------------------------------------------------------------- ctrl
# Locker introspection via /run/user/<uid>/qdlocker.sock inside the VM.
# Uses qdwin's vm-exec helper to socat into the guest.

qdlocker_ctrl() {
    qdwin_require_vm || return $?
    local cmd="$1"
    "$QDWIN_VM_EXEC" "$VMNAME" \
        "printf '%s\n' '${cmd}' | socat -t 1 - UNIX-CONNECT:/run/user/1000/qdlocker.sock"
}

# ---------------------------------------------------------------- poll

qdlocker_wait_for_lock() {
    local timeout="${1:-5}"
    local end=$(( SECONDS + timeout ))
    while [ $SECONDS -lt $end ]; do
        case "$(qdlocker_ctrl status 2>/dev/null)" in
            *locked=True*) return 0 ;;
        esac
        sleep 0.2
    done
    echo "qdlocker_wait_for_lock: timeout after ${timeout}s" >&2
    return 1
}

qdlocker_wait_for_unlock() {
    local timeout="${1:-5}"
    local end=$(( SECONDS + timeout ))
    while [ $SECONDS -lt $end ]; do
        case "$(qdlocker_ctrl unlock-result 2>/dev/null)" in
            *last=success*) return 0 ;;
        esac
        sleep 0.2
    done
    echo "qdlocker_wait_for_unlock: timeout after ${timeout}s" >&2
    return 1
}

# Convenience: assert the locker prompt has N characters (after typing).
qdlocker_assert_prompt_len() {
    local want="$1"
    local got
    got=$(qdlocker_ctrl status | sed -n 's/.*prompt-len=\([0-9]*\).*/\1/p')
    if [ "$got" != "$want" ]; then
        echo "qdlocker_assert_prompt_len: want=$want got=$got" >&2
        return 1
    fi
}

# ---------------------------------------------------------------- health
#
# Composite check: qdwin session healthy AND qdlocker user-unit active.
# Both must pass before any scenario steps run.

qdlocker_session_healthy() {
    qdwin_require_vm || return $?
    if ! qdwin_session_healthy >/dev/null 2>&1; then
        echo "qdlocker_session_healthy: qdwin session not up" >&2
        return 1
    fi
    # `runuser -l admin -c` runs a login shell — same env (XDG_RUNTIME_DIR,
    # DBUS_SESSION_BUS_ADDRESS) as an interactive admin login. Bare
    # `runuser -u admin -- systemctl --user` doesn't get these and
    # the user manager lookup fails opaquely.
    local state
    state=$("$QDWIN_VM_EXEC" "$VMNAME" \
        'runuser -l admin -c "systemctl --user is-active qdlocker.service"' 2>/dev/null \
        | tr -d '\r\n')
    case "$state" in
        active) ;;
        *)
            echo "qdlocker_session_healthy: qdlocker.service is '$state' (want active)" >&2
            return 1
            ;;
    esac
    # Ctrl-socket must respond — proves the QML root window mounted
    # AND the listener fd is up. This is the most authoritative
    # smoke check; everything else is necessary-but-not-sufficient.
    if ! qdlocker_ctrl status >/dev/null 2>&1; then
        echo "qdlocker_session_healthy: ctrl-socket not responding" >&2
        return 1
    fi
}
