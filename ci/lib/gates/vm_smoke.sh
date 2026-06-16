#!/usr/bin/env bash
# qci module: vm-smoke gate
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

gate_vm_smoke() {
    qci_assert_run_dir || return $?
    qci_assert_vm_tools vm-smoke || return $?
    local explicit=${1:-} vm rc=$EXIT_OK out cmd
    vm=$(acquire_vm vm-smoke "$explicit") || return "$EXIT_VM_PROVISION"
    kv vm "$vm"
    out="$RDIR/vm/vm-smoke.log"
    : > "$out"
    cmd="test -S /run/user/1000/wayland-1"
    "$VM_TOOLS/vm-exec" "$vm" "$cmd" >> "$out" 2>&1 || rc=$EXIT_VM_BOOT
    if [ "$rc" -eq 0 ]; then
        cmd=$(cat <<'EOF'
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 bash -lc '
set -u
check_unit() {
    unit=$1
    state=$(systemctl --user is-active "$unit" 2>/dev/null || true)
    printf "%s %s\n" "$unit" "${state:-unknown}"
    [ "$state" = active ]
}
if systemctl --user list-unit-files qdwin-compositor.service >/dev/null 2>&1; then
    ok=0
    check_unit qdwin-compositor.service || ok=1
    check_unit qdshell.service || ok=1
    check_unit qdistro-cursor-sprites.service || ok=1
    exit "$ok"
else
    echo NO_QDWIN_UNITS
    exit 1
fi
'
EOF
)
        "$VM_TOOLS/vm-exec" "$vm" "$cmd" >> "$out" 2>&1 || rc=$EXIT_SERVICE
        if grep -qE ' (inactive|failed|unknown)$|^NO_QDWIN_UNITS' "$out"; then
            rc=$EXIT_SERVICE
        fi
    fi
    collect_vm_artifacts "$vm" vm-smoke
    if [ "$rc" -eq 0 ]; then
        record_result vm-smoke "$vm" pass 0 pass vm "$out" "session smoke passed"
    else
        record_result vm-smoke "$vm" fail "$rc" "$(exit_class_name "$rc")" vm "$out" "session smoke failed"
    fi
    release_vm "$vm" "$rc"
    return "$rc"
}
