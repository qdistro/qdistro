#!/usr/bin/env bash
# qci module: snapshot-daily gate
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

gate_snapshot_daily() {
    qci_assert_run_dir || return $?
    qci_assert_vm_tools snapshot-daily || return $?
    local date_arg=$1 name_arg=$2 rc vm tmp old_disk new_name new_disk xml
    date_arg=${date_arg:-$(date -u +%F)}
    new_name=${name_arg:-qdistro-daily-$date_arg}
    if "${VIRSH[@]}" dominfo "$new_name" >/dev/null 2>&1; then
        record_blocked snapshot-daily "$new_name" "$EXIT_VM_PROVISION" vm "target VM already exists"
        return "$EXIT_VM_PROVISION"
    fi
    if [ -f "${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}/$new_name.qcow2" ]; then
        record_blocked snapshot-daily "$new_name" "$EXIT_VM_PROVISION" vm "target disk already exists"
        return "$EXIT_VM_PROVISION"
    fi
    vm=$(acquire_vm snapshot-daily "") || return "$EXIT_VM_PROVISION"
    tmp=$vm
    mkdir -p "$RDIR/vm"
    if ! "$VM_TOOLS/vm-exec" "$tmp" "bash /root/qdistro-src/qdistro/scripts/vm/enable-qdgreeter.sh" \
        > "$RDIR/vm/enable-qdgreeter.log" 2>&1; then
        record_result snapshot-daily "$new_name" fail "$EXIT_VM_PROVISION" vm_provision vm "$RDIR/vm/enable-qdgreeter.log" "could not enable qdgreeter/greetd"
        return "$EXIT_VM_PROVISION"
    fi
    if ! "$VM_TOOLS/vm-exec" "$tmp" "test -s /usr/bin/qdgreeter && sync" \
        >> "$RDIR/vm/enable-qdgreeter.log" 2>&1; then
        record_result snapshot-daily "$new_name" fail "$EXIT_VM_PROVISION" vm_provision vm "$RDIR/vm/enable-qdgreeter.log" "qdgreeter wrapper missing or empty after setup"
        return "$EXIT_VM_PROVISION"
    fi
    collect_vm_artifacts "$tmp" snapshot-source
    "${VIRSH[@]}" destroy "$tmp" >/dev/null 2>&1 || true
    old_disk=$(vm_disk_path "$tmp" || true)
    new_disk="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}/$new_name.qcow2"
    xml=$("${VIRSH[@]}" dumpxml "$tmp" --inactive 2>/dev/null)
    if [ -z "$xml" ] || [ ! -f "$old_disk" ]; then
        record_blocked snapshot-daily "$new_name" "$EXIT_VM_PROVISION" vm "could not read temporary VM XML or disk"
        return "$EXIT_VM_PROVISION"
    fi
    if ! "${VIRSH[@]}" undefine "$tmp" --nvram >/dev/null 2>&1 \
        && ! "${VIRSH[@]}" undefine "$tmp" >/dev/null 2>&1; then
        record_blocked snapshot-daily "$new_name" "$EXIT_VM_PROVISION" vm "could not undefine temporary VM $tmp"
        return "$EXIT_VM_PROVISION"
    fi
    if ! mv "$old_disk" "$new_disk"; then
        record_blocked snapshot-daily "$new_name" "$EXIT_VM_PROVISION" vm "could not move $old_disk to $new_disk"
        return "$EXIT_VM_PROVISION"
    fi
    xml=$(printf '%s\n' "$xml" \
        | sed -e "s|<name>$tmp</name>|<name>$new_name</name>|" \
              -e '/<uuid>/d' \
              -e "s|$old_disk|$new_disk|g" \
              -e "s|$tmp.qcow2|$new_name.qcow2|g")
    printf '%s\n' "$xml" | "${VIRSH[@]}" define /dev/stdin > "$RDIR/vm/snapshot-define.log" 2>&1
    rc=$?
    if [ "$rc" -ne 0 ]; then
        record_result snapshot-daily "$new_name" fail "$EXIT_VM_PROVISION" vm_provision vm "$RDIR/vm/snapshot-define.log" "virsh define failed"
        return "$EXIT_VM_PROVISION"
    fi
    if ! "${VIRSH[@]}" start "$new_name" >> "$RDIR/vm/snapshot-define.log" 2>&1; then
        record_result snapshot-daily "$new_name" fail "$EXIT_VM_BOOT" vm_boot vm "$RDIR/vm/snapshot-define.log" "virsh start failed"
        return "$EXIT_VM_BOOT"
    fi
    if ! "$VM_TOOLS/vm-start-and-wait" "$new_name" >> "$RDIR/vm/snapshot-define.log" 2>&1; then
        record_result snapshot-daily "$new_name" fail "$EXIT_VM_BOOT" vm_boot vm "$RDIR/vm/snapshot-define.log" "daily VM did not become ready"
        return "$EXIT_VM_BOOT"
    fi
    if ! "$VM_TOOLS/vm-exec" "$new_name" "test -s /usr/bin/qdgreeter && for i in \$(seq 1 30); do systemctl is-active --quiet greetd.service && exit 0; sleep 1; done; exit 1" \
        >> "$RDIR/vm/snapshot-define.log" 2>&1; then
        record_result snapshot-daily "$new_name" fail "$EXIT_VM_BOOT" vm_boot vm "$RDIR/vm/snapshot-define.log" "daily VM booted without active qdgreeter/greetd"
        return "$EXIT_VM_BOOT"
    fi
    kv daily_vm "$new_name"
    kv daily_vm_date "$date_arg"
    collect_vm_artifacts "$new_name" snapshot-daily
    record_result snapshot-daily "$new_name" pass 0 pass vm "$RDIR/vm/snapshot-define.log" "daily VM captures current source state"
    return 0
}
