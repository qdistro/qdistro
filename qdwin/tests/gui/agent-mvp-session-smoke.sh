#!/usr/bin/env bash
set -euo pipefail

# Agent/CI smoke for the plan2 MVP desktop session.
#
# Covers the implemented P01-style qdwin/qdshell boot surface and the
# qdshell/qddwin regressions that make a daily-driver VM look blank:
#   - qdwin and qdshell user services are active
#   - qdshell binds qdwin_shell_v1 at a usable protocol version
#   - LXQt/labwc fallback is not the active desktop
#   - cursor sprite helper is installed and active
#   - Ctrl+Space reaches qdwin's launcher keybinding instrumentation
#   - a normal xdg_toplevel is released from qdwin's holding layer
#
# It does not depend on the old /run/user/1000/qdshell.sock ctrl socket.

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
QDWIN_REPO=${QDWIN_REPO:-$ROOT}
VMNAME=${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -n1)}

export QDWIN_REPO
export QDWIN_VIRSH=${QDWIN_VIRSH:-virsh -c qemu:///session}

# shellcheck source=qdwin-helpers.sh
source "$QDWIN_REPO/tests/gui/qdwin-helpers.sh"
qdwin_set_vm "$VMNAME"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

pass() {
    echo "PASS: $*"
}

vm_exec() {
    "$QDWIN_VM_EXEC" "$VMNAME" "$@"
}

journal_cursor() {
    vm_exec "journalctl _UID=1000 -n 1 --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'"
}

journal_after() {
    local cursor=$1
    vm_exec "journalctl _UID=1000 --after-cursor='$cursor' --no-pager 2>/dev/null"
}

wait_for_log() {
    local cursor=$1 pattern=$2 label=$3
    for _ in $(seq 1 30); do
        if journal_after "$cursor" | grep -qE "$pattern"; then
            pass "$label"
            return 0
        fi
        sleep 0.2
    done
    fail "$label"
}

cleanup() {
    vm_exec "pkill -KILL -u admin -f '[q]distro-test-window --title agent-mvp-' 2>/dev/null || true" >/dev/null || true
}
trap cleanup EXIT

[ -n "$VMNAME" ] || fail "no running VM; set VMNAME or start qdistro-daily"
$QDWIN_VIRSH domstate "$VMNAME" >/dev/null || fail "VM '$VMNAME' not found"

vm_exec "test -S /run/user/1000/wayland-1" >/dev/null \
    || fail "admin qdwin Wayland socket is not up"
pass "admin qdwin Wayland socket is up"

vm_exec "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active qdwin-compositor.service qdshell.service qdistro-cursor-sprites.service" \
    | grep -qx active || fail "one or more qdwin session services are inactive"
pass "qdwin compositor, qdshell, and cursor sprite services are active"

if vm_exec "pgrep -x labwc >/dev/null || pgrep -x lxqt-panel >/dev/null || pgrep -x lxqt-session >/dev/null"; then
    fail "legacy LXQt/labwc fallback processes are running in the production session"
fi
pass "LXQt/labwc fallback is not running"

# `systemctl is-active` above only proves qdshell's UNIT started, not that it
# completed the qdwin_shell_v1 protocol bind. The bind can lag service start by
# a beat, so poll the boot journal for the bind line instead of sampling once
# (a single sample raced the bind and spuriously failed). On a genuinely dead
# session (e.g. qdshell.service start-limit-hit) the bind never appears and we
# still fail — with the qdshell/qdwin journal tail attached for triage.
bound_ver=""
for _ in $(seq 1 50); do
    bound_ver=$(vm_exec "journalctl _UID=1000 -b --no-pager 2>/dev/null | sed -n 's/.*qdwin_shell_v1 bound v\\([0-9][0-9]*\\).*/\\1/p' | tail -1")
    [ -n "$bound_ver" ] && break
    sleep 0.2
done
if [ -z "$bound_ver" ]; then
    vm_exec "journalctl _UID=1000 -b --no-pager -u qdshell.service -u qdwin-compositor.service 2>/dev/null | tail -200" >&2 || true
    fail "qdshell qdwin_shell_v1 bind version not found in journal"
fi
[ "$bound_ver" -ge 14 ] || fail "qdshell bound qdwin_shell_v1 v$bound_ver; need >=14"
pass "qdshell bound qdwin_shell_v1 v$bound_ver"

if vm_exec "journalctl _UID=1000 -b --no-pager | grep -q 'cursor-sprite registered shape=pointer'"; then
    pass "cursor sprite helper registered pointer shape"
else
    fail "cursor sprite helper did not register pointer shape"
fi

cursor_key=$(journal_cursor)
qdwin_chord ctrl -- spc
sleep 0.4
qdwin_send_key KEY_ESC
wait_for_log "$cursor_key" 'qdwin: launcher_requested' \
    "Ctrl+Space emitted qdwin launcher_requested"

cleanup
cursor_win=$(journal_cursor)
title="agent-mvp-release-$$"
# setsid -f detaches the client into its own session so it survives the
# vm_exec shell returning; a bare `&` background gets SIGHUP'd when the agent
# command exits, so the toplevel never maps and "reached qdwin" times out.
vm_exec "setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 qdistro-test-window --title '$title' --width 300 --height 180 --color 0xff304050 >/tmp/$title.log 2>&1"
wait_for_log "$cursor_win" 'qdwin: toplevel_added handle=[0-9]+ uid=1000 pid=[0-9]+ app_id=qdistro-test-window' \
    "test toplevel reached qdwin"
wait_for_log "$cursor_win" 'qdwin: holding_released handle=[0-9]+ via (set_border_color|default_toplevel_policy)' \
    "qdshell released ordinary toplevel from qdwin holding layer"

qdwin_screenshot "/tmp/qdwin-agent-mvp-session-smoke-$$.png" >/dev/null
pass "artifact /tmp/qdwin-agent-mvp-session-smoke-$$.png"
