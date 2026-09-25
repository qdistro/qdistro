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
: "${QDWIN_VM_EXEC:=${QDLOCKER_REPO}/../scripts/vm/vm-exec}"   # monorepo root

# qdwin session unit names. Default to the production/deploy names
# (qdistro/deploy/). VMs spun via install-qdwin-session-for-vm.sh now ship
# the session under these SAME deploy names (the legacy noctalia-* aliases
# were retired 2026-06-16), so the defaults validate the real contract on
# every lane. The env-var indirection is kept only as an escape hatch for
# explicitly nonstandard experiments / future unit renames — the normal
# VM path needs no override.
: "${QDWIN_COMPOSITOR_UNIT:=qdwin-compositor.service}"
: "${QDWIN_SHELL_UNIT:=qdshell.service}"

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
    # The ctrl socket is uid-gated to the session owner (admin/1000) via
    # SO_PEERCRED and fails closed for any other peer. vm-exec runs as root, so
    # the socat MUST run as admin or the connection is refused ("reset by
    # peer"). base64 the inner script to dodge vm-exec's embedded-quote
    # handling (mirrors qdwin-helpers.sh qdwin_ctrl, which runs as admin too).
    local inner b64
    inner="printf '%s\n' '${cmd}' | socat -t 1 - UNIX-CONNECT:/run/user/1000/qdlocker.sock"
    b64=$(printf '%s' "$inner" | base64 -w0)
    "$QDWIN_VM_EXEC" "$VMNAME" "echo $b64 | base64 -d | runuser -u admin -- bash"
}

# NOTE on `systemctl --user` for admin from a root vm-exec: it needs the user
# session context (XDG_RUNTIME_DIR), or it fails "DBUS_SESSION_BUS_ADDRESS not
# defined" and dropins/restarts silently don't apply. Scenarios pin it inline as
# `runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user ...`
# (see 03-idle-lock-trigger.md / 04-lid-close-lock.md).

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

# Type the password once, char by char, via QMP key events. Per-key sleeps are
# 0.08s (bumped from 0.05) so the guest's QMP input queue drains between events —
# the previous timing occasionally dropped a char, producing a short prompt and a
# spurious PAM reject. Verification/retry lives in the public wrapper below.
_qdlocker_type_password_chars_once() {
    local password="$1"
    local i ch
    for ((i = 0; i < ${#password}; i++)); do
        ch="${password:i:1}"
        case "$ch" in
            [a-z0-9]) ;;
            [A-Z])
                qdwin_qmp_key shift down; sleep 0.04
                qdwin_qmp_key "${ch,,}" down; sleep 0.08
                qdwin_qmp_key "${ch,,}" up;   sleep 0.08
                qdwin_qmp_key shift up; sleep 0.04
                continue
                ;;
            _)
                qdwin_qmp_key shift down; sleep 0.04
                qdwin_qmp_key minus down; sleep 0.08
                qdwin_qmp_key minus up;   sleep 0.08
                qdwin_qmp_key shift up; sleep 0.04
                continue
                ;;
            *)
                echo "qdlocker_unlock_with_password: unsupported char '$ch'" >&2
                return 2
                ;;
        esac
        qdwin_qmp_key "$ch" down; sleep 0.08
        qdwin_qmp_key "$ch" up;   sleep 0.08
    done
}

# Type the password and VERIFY all chars landed via the locker's prompt-len
# introspection, retrying (clear + retype) up to 3 times. This converts the
# QMP keystroke-injection flake (a dropped char → wrong password → PAM reject)
# into a deterministic outcome. The final prompt-len is still authoritative:
# the caller's unlock assertion remains hard, so this hides a transient dropped
# key but NOT a real routing/PAM regression.
qdlocker_type_password_chars() {
    local password="${1:-Pa_ssw0rd45}"
    local want=${#password}
    local attempt
    for attempt in 1 2 3; do
        _qdlocker_type_password_chars_once "$password" || return $?
        sleep 0.2
        if qdlocker_assert_prompt_len "$want" 2>/dev/null; then
            return 0
        fi
        echo "qdlocker_type_password_chars: attempt $attempt prompt-len mismatch (want $want); clearing + retrying" >&2
        # Clear the field: send a generous number of backspaces, then confirm the
        # field is actually empty before retyping — otherwise residual chars make
        # the retype overshoot and the next attempt fails the same way.
        local b
        for ((b = 0; b < want + 4; b++)); do
            qdwin_qmp_key backspace down; sleep 0.03
            qdwin_qmp_key backspace up;   sleep 0.03
        done
        sleep 0.2
        qdlocker_assert_prompt_len 0 2>/dev/null \
            || echo "qdlocker_type_password_chars: field not empty after clear (attempt $attempt)" >&2
    done
    echo "qdlocker_type_password_chars: failed to reach prompt-len=$want after 3 attempts" >&2
    return 1
}

qdlocker_unlock_with_password() {
    qdlocker_type_password_chars "${1:-Pa_ssw0rd45}" || return $?
    qdwin_send_key KEY_ENTER
    qdlocker_wait_for_unlock 5
}

qdlocker_drain_lock_state() {
    case "$(qdlocker_ctrl status 2>/dev/null)" in
        *locked=True*)
            if qdlocker_unlock_with_password "${1:-Pa_ssw0rd45}"; then
                return 0
            fi
            echo "qdlocker_drain_lock_state: password unlock failed; restarting qdwin session" >&2
            "$QDWIN_VM_EXEC" "$VMNAME" \
                "runuser -l admin -c \"systemctl --user restart $QDWIN_COMPOSITOR_UNIT\"; sleep 3; runuser -l admin -c \"systemctl --user restart $QDWIN_SHELL_UNIT qdlocker.service\"; sleep 3" \
                >/dev/null
            case "$(qdlocker_ctrl status 2>/dev/null)" in
                *locked=False*) return 0 ;;
                *)
                    echo "qdlocker_drain_lock_state: still locked after session restart" >&2
                    return 1
                    ;;
            esac
            ;;
    esac
}

# ---------------------------------------------------------------- pixels
#
# Sentinel-color assertions for lock-screen occlusion tests. These use
# ImageMagick on the host screenshot, not agent vision, so "a thin strip
# of desktop is visible" turns into a deterministic failure.

# The SCREEN dimensions of a capture: the raw frame's, from its `.raw` sidecar
# (view-geometry.sh pads every harness frame with a unique black margin), or
# the decoded size of a legacy frame without one. Output format unchanged:
# "W H" with no trailing newline.
qdlocker_screenshot_dimensions() {
    local image="$1" dims
    if ! command -v magick >/dev/null 2>&1; then
        echo "qdlocker_screenshot_dimensions: ImageMagick 'magick' not found" >&2
        return 2
    fi
    dims=$(qci_view_raw_dims "$image") || return 1
    printf '%s' "$dims"
}

qdlocker_count_color_in_crop() {
    local image="$1" color="$2" crop="$3"
    local hex
    hex=$(printf "%s" "$color" | tr '[:lower:]' '[:upper:]' | sed 's/^#//')
    if ! command -v magick >/dev/null 2>&1; then
        echo "qdlocker_count_color_in_crop: ImageMagick 'magick' not found" >&2
        return 2
    fi
    magick "$image" -alpha off -crop "$crop" \
        -format %c histogram:info:- \
        | awk -v hex="$hex" '
            toupper($0) ~ ("#" hex) {
                gsub(":", "", $1);
                sum += $1;
            }
            END { print sum + 0 }
        '
}

qdlocker_assert_color_absent_in_crop() {
    local image="$1" color="$2" crop="$3" label="${4:-$crop}"
    local count
    count=$(qdlocker_count_color_in_crop "$image" "$color" "$crop") || return $?
    if [ "$count" -ne 0 ]; then
        echo "qdlocker_assert_color_absent_in_crop: $label has $count pixels of $color in $image" >&2
        return 1
    fi
}

qdlocker_assert_color_present_in_crop() {
    local image="$1" color="$2" crop="$3" label="${4:-$crop}"
    local count
    count=$(qdlocker_count_color_in_crop "$image" "$color" "$crop") || return $?
    if [ "$count" -eq 0 ]; then
        echo "qdlocker_assert_color_present_in_crop: $label has no $color pixels in $image" >&2
        return 1
    fi
}

# ---------------------------------------------------------------- health
#
# Composite check: qdwin session healthy AND qdlocker user-unit active.
# Both must pass before any scenario steps run.

# Finding 02: in production the ctrl socket serves only `lock`; the
# introspection commands (status, unlock-result, prompt-text) that the GUI
# scenarios assert on are authorized only by the ROOT-OWNED marker
# /etc/qdistro/locker-ctrl-introspection (a user-controlled env var would let a
# same-uid process re-enable the side channel). Install the marker as root and
# restart the unit. Idempotent — only restarts when the marker was just created.
# Fails loudly (non-zero) if it cannot install/restart, so the health check
# below does not silently run scenarios without introspection.
qdlocker_enable_introspection() {
    qdwin_require_vm || return $?
    # Send the block as a base64 envelope: vm-exec's qga/JSON encoding mishandles
    # multi-line args with nested quotes + backslashes (the status probe below).
    local script b64
    script=$(cat <<'SCRIPT'
set -e
f=/etc/qdistro/locker-ctrl-introspection
if [ ! -f "$f" ]; then
    install -d -m 0755 -o 0 -g 0 /etc/qdistro
    : > "$f"; chown 0:0 "$f"; chmod 0644 "$f"
    runuser -l admin -c "systemctl --user restart qdlocker.service"
    sleep 4
fi
# Verify it took effect: the locker must now answer `status`. Connect as admin —
# the ctrl socket is uid-gated to the session owner and refuses the root context.
reply=$(runuser -u admin -- bash -c "printf 'status\n' | socat -t 1 - UNIX-CONNECT:/run/user/1000/qdlocker.sock")
case "$reply" in
    *locked=*) exit 0 ;;
    *) echo "introspection not active (status: $reply)" >&2; exit 1 ;;
esac
SCRIPT
)
    b64=$(printf '%s' "$script" | base64 -w0)
    "$QDWIN_VM_EXEC" "$VMNAME" "echo $b64 | base64 -d | bash"
}

qdlocker_prepare_gui_lane() {
    qdwin_require_vm || return $?
    local script b64
    script=$(cat <<'SCRIPT'
set -e
# GUI scenarios exercise password auth repeatedly across preserved/replayed VMs.
# Reset stale faillock tallies so one failed/flaky run does not poison the next
# scenario, then keep the production 5-minute idle lock from firing in long
# focus/input suites. Individual idle tests can still override this explicitly.
faillock --user admin --reset 2>/dev/null || true
install -d -m 0755 -o admin -g users /home/admin/.config/systemd/user/qdlocker.service.d
cat >/home/admin/.config/systemd/user/qdlocker.service.d/90-ci-gui.conf <<'EOF'
[Service]
Environment=QDLOCKER_IDLE_MS=86400000
EOF
chown admin:users /home/admin/.config/systemd/user/qdlocker.service.d/90-ci-gui.conf
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service
sleep 3
SCRIPT
)
    b64=$(printf '%s' "$script" | base64 -w0)
    "$QDWIN_VM_EXEC" "$VMNAME" "echo $b64 | base64 -d | bash"
}

qdlocker_session_healthy() {
    qdwin_require_vm || return $?
    # The GUI lane drives introspection commands; production gates them off.
    # Fail loudly if introspection could not be enabled — otherwise later
    # scenarios that parse `locked=`/`prompt-len=` would silently false-green.
    if ! qdlocker_enable_introspection; then
        echo "qdlocker_session_healthy: could not enable ctrl introspection" >&2
        return 1
    fi
    if ! qdlocker_prepare_gui_lane; then
        echo "qdlocker_session_healthy: could not prepare GUI lane state" >&2
        return 1
    fi
    local compositor_state
    compositor_state=$("$QDWIN_VM_EXEC" "$VMNAME" \
        "runuser -l admin -c \"systemctl --user is-active $QDWIN_COMPOSITOR_UNIT\"" 2>/dev/null \
        | tr -d '\r\n')
    case "$compositor_state" in
        active) ;;
        *)
            echo "qdlocker_session_healthy: $QDWIN_COMPOSITOR_UNIT is '$compositor_state' (want active)" >&2
            return 1
            ;;
    esac
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
    # Ctrl-socket must respond with a real status line — proves the QML root
    # window mounted, the listener fd is up, AND introspection is active (a
    # production-gated socket would answer `error: command unavailable` and
    # exit 0, false-greening the check).
    case "$(qdlocker_ctrl status 2>/dev/null)" in
        *locked=*) ;;
        *)
            echo "qdlocker_session_healthy: ctrl-socket status not available "\
                 "(introspection off or socket down)" >&2
            return 1
            ;;
    esac
}
