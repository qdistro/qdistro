#!/usr/bin/env bash
set -euo pipefail

# Agent/CI protocol audit for qdwin + qdshell compatibility.
#
# This is intentionally broader than a unit smoke. It records:
#   - Wayland globals advertised by the running qdwin session
#   - qdshell's static Quickshell/Wayland protocol usage
#   - whether qdshell's launcher popup path trips the xdg_popup
#     "parent must be non-null" protocol error

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

detect_screen_size() {
    local tmp=/tmp/qdwin-protocol-audit-screen-$$.ppm
    $QDWIN_VIRSH screenshot "$VMNAME" "$tmp" >/dev/null
    local dims
    dims=$(file -b "$tmp" | sed -n 's/.* \([0-9][0-9]*\) x \([0-9][0-9]*\).*/\1 \2/p')
    rm -f "$tmp"
    [ -n "$dims" ] || fail "could not read screenshot dimensions"
    echo "$dims"
}

require_global() {
    local globals=$1 iface=$2
    if grep -qx "$iface" "$globals"; then
        pass "global advertised: $iface"
    else
        fail "missing Wayland global: $iface"
    fi
}

[ -n "$VMNAME" ] || fail "no running VM; set VMNAME or start qdistro-daily"
$QDWIN_VIRSH domstate "$VMNAME" >/dev/null || fail "VM '$VMNAME' not found"

vm_exec "test -S /run/user/1000/wayland-1" >/dev/null \
    || fail "admin qdwin Wayland socket is not up"

# qdistro-daily ships qdwin-compositor.service + qdshell.service;
# noctalia-bootstrap ships noctalia-session.service + noctalia-shell.service.
if vm_exec "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user list-unit-files qdwin-compositor.service" >/dev/null 2>&1; then
    QDWIN_UNITS="qdwin-compositor.service qdshell.service"
else
    QDWIN_UNITS="noctalia-session.service noctalia-shell.service"
fi
vm_exec "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active $QDWIN_UNITS" \
    > /tmp/audit-svc-$$.txt 2>&1
if grep -qE '^(inactive|failed|unknown)' /tmp/audit-svc-$$.txt; then
    cat /tmp/audit-svc-$$.txt >&2
    rm -f /tmp/audit-svc-$$.txt
    fail "qdwin/qdshell services are not active"
fi
rm -f /tmp/audit-svc-$$.txt
pass "qdwin and qdshell services are active ($QDWIN_UNITS)"

read -r QDWIN_SCREEN_W QDWIN_SCREEN_H < <(detect_screen_size)
export QDWIN_SCREEN_W QDWIN_SCREEN_H

tmpdir=${TMPDIR:-/tmp}/qdwin-protocol-audit-$$
mkdir -p "$tmpdir"
echo "INFO: audit artifact dir: $tmpdir"

globals_file="$tmpdir/globals.txt"
if vm_exec "command -v wayland-info >/dev/null"; then
    vm_exec "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 wayland-info 2>/dev/null | sed -n 's/^interface: '\\''\\([^'\\'']*\\)'\\''.*/\\1/p' | sort -u" \
        > "$globals_file"
else
    fail "wayland-info is not installed in VM; cannot audit advertised globals"
fi

require_global "$globals_file" wl_compositor
require_global "$globals_file" wl_shm
require_global "$globals_file" wl_seat
require_global "$globals_file" xdg_wm_base
require_global "$globals_file" zwlr_layer_shell_v1
require_global "$globals_file" wp_cursor_shape_manager_v1
require_global "$globals_file" wp_fractional_scale_manager_v1
require_global "$globals_file" zwp_text_input_manager_v3
require_global "$globals_file" zwp_input_method_manager_v2
require_global "$globals_file" zwp_virtual_keyboard_manager_v1
require_global "$globals_file" qdwin_shell_v1

static_file="$tmpdir/qdshell-static-protocol-use.txt"
{
    echo "== QML imports =="
    rg -n '^import (Quickshell|QtWayland|Qdistro)' "$QDWIN_WORKSPACE/qdshell" || true
    echo
    echo "== Layer-shell usage =="
    rg -n 'WlrLayershell|WlrLayer|WlrKeyboardFocus|PanelWindow|PopupWindow|xdg|popup|get_popup' "$QDWIN_WORKSPACE/qdshell" || true
} > "$static_file"
echo "INFO: static qdshell protocol scan: $static_file"

cursor=$(journal_cursor)
qdwin_click 16 16
sleep 1.0

if journal_after "$cursor" | grep -q 'popup parent must be non-null\|Protocol error'; then
    journal_after "$cursor" | tail -80 >&2
    fail "qdshell launcher click caused Wayland protocol error"
fi
pass "launcher click did not crash qdshell with null-parent popup"

if journal_after "$cursor" | grep -q 'qdwin: layer-shell get_popup'; then
    pass "layer-shell get_popup path observed"
else
    echo "WARN: launcher opened without observing qdwin layer-shell get_popup"
fi

# plan3 M4 (deep-review): positive H1 grab discriminator. When the
# launcher opens a popup with xdg_popup.grab, qdwin logs
# "qdwin: layer-popup grab started popup=...". Soft check — Quickshell
# may not always grab on this path, and on stock libweston the dlsym
# fallback will refuse the grab (logged at startup as DEGRADED).
if journal_after "$cursor" | grep -q 'qdwin: layer-popup grab started'; then
    pass "layer-popup grab path observed"
elif vm_exec "journalctl _UID=1000 -b --no-pager | grep -q 'layer-popup grab handler NOT registered'"; then
    echo "GAP: layer-popup grab handler not registered — stock libweston without qdistro patch; grab assertion is N/A on this build"
else
    echo "WARN: launcher opened but no qdwin: layer-popup grab started observed; the popup may not have requested grab on this path"
fi

# ---- Crash-log assertions ----------------------------------------------
# A clean interaction must not have left compositor/shell crash evidence
# in the boot journal. These patterns are deliberately specific so an
# app-level Qt warning does not trip the gate.
crashlog="$tmpdir/crash-scan.txt"
vm_exec "journalctl _UID=1000 -b --no-pager 2>/dev/null" > "$crashlog" || true
crash_hits=$(grep -nE \
    'weston.*(Segmentation fault|assert|Aborted)|qs\[[0-9]+\]: .*(SIGSEGV|core dumped)|wl_display@[0-9]+: error|Fatal:.*qdwin|qdwin: .*FATAL' \
    "$crashlog" || true)
if [ -n "$crash_hits" ]; then
    echo "$crash_hits" | tail -40 >&2
    fail "crash/protocol-error evidence in qdwin/qdshell journal"
fi
pass "no crash/protocol-error evidence in journal"

# coredumpctl (if present) is the authoritative crash record.
if vm_exec "command -v coredumpctl >/dev/null 2>&1"; then
    # coredumpctl's default list spans retained journals from older boots. The
    # assertion is explicitly about this boot, matching the journal scan above,
    # so bind it to the guest's current boot ID rather than flagging historical
    # crashes on a preserved/reused CI VM.
    boot_id=$(vm_exec "cat /proc/sys/kernel/random/boot_id")
    [ -n "$boot_id" ] || fail "could not read current boot ID for coredump audit"
    if vm_exec "coredumpctl --no-pager list _BOOT_ID=$boot_id 2>/dev/null | grep -qE 'weston|qs|quickshell'"; then
        vm_exec "coredumpctl --no-pager list _BOOT_ID=$boot_id 2>/dev/null | grep -E 'weston|qs|quickshell'" >&2 || true
        fail "coredumpctl recorded a weston/quickshell crash this boot"
    fi
    pass "no weston/quickshell coredumps recorded for boot $boot_id"
fi

# ---- Vendored-libweston escalation -------------------------------------
# When the session is running the patched libweston (the shipped config),
# the DEGRADED warnings must be ABSENT and the deeper popup-grab
# behaviour gets its own gate. agent-vendored-libweston-verify.sh is the
# live discriminator; here we just escalate the DEGRADED check from soft
# to hard whenever the vendored tree is the loaded library.
vendored_prefix=${QDWIN_VENDORED_LIBWESTON_PREFIX:-/usr/libexec/qdistro/qdwin-libweston}
loaded_lw=$(vm_exec "pmap \$(pgrep -x weston | head -n1) 2>/dev/null | grep -o '/[^ ]*libweston-[0-9]*\.so[^ ]*' | sort -u | head -n1" || true)
case "$loaded_lw" in
    "$vendored_prefix"/*)
        if vm_exec "journalctl _UID=1000 -b --no-pager | grep -q 'layer-popup grab handler NOT registered'"; then
            fail "vendored libweston is loaded ($loaded_lw) but qdwin logged DEGRADED grab-handler — patched symbols not resolved"
        fi
        pass "vendored libweston loaded ($loaded_lw) with no DEGRADED grab warning"
        echo "INFO: run tests/gui/agent-vendored-libweston-verify.sh for the full live grab/dismiss discriminators"
        ;;
    "")
        echo "WARN: could not determine the loaded libweston-16.so path"
        ;;
    *)
        echo "GAP: session running NON-vendored libweston ($loaded_lw) — layer-popup grab paths are N/A; install-vendored-libweston.sh not applied or unit not restarted"
        ;;
esac

qdwin_screenshot "/tmp/qdwin-protocol-audit-$VMNAME-$$.png" >/dev/null
pass "artifact /tmp/qdwin-protocol-audit-$VMNAME-$$.png"
