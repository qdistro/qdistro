#!/bin/bash
# vm-base.sh — which backing image spin-test-vm.sh clones.
# Sourced. Prints kiwi|baked on stdout, errors on stderr, returns non-zero
# on a bad QDISTRO_VM_BASE or a required kiwi base that is missing.
#
# QDISTRO_VM_BASE:
#   auto  (default)  qdistro-kiwi-base.qcow2 if imported, else baseweed-baked
#   kiwi             require the imported tester image (iso/14 Phase G)
#   baked            always baseweed-baked (build-in-vm.sh hard-wires this)
#
# The kiwi builder must stay on baked: using the tester image as the
# builder backing is circular. Per-run goldens still overlay current
# source via fresh-vm-bootstrap.sh; this only replaces the *base*.

qdistro_kiwi_base_path() {
    local img="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
    printf '%s\n' "${QDISTRO_KIWI_BASE:-$img/qdistro-kiwi-base.qcow2}"
}

# True if import-kiwi-base.sh finished: dest exists, stamp exists, qemu-img
# says qcow2. A leftover empty file must not flip QDISTRO_VM_BASE=auto.
qdistro_kiwi_base_ok() {
    local kiwi stamp
    kiwi="$(qdistro_kiwi_base_path)"
    stamp="${kiwi}.stamp"
    [ -f "$kiwi" ] && [ -f "$stamp" ] || return 1
    qemu-img info "$kiwi" 2>/dev/null | grep -q 'file format: qcow2'
}

# True if $1 is the imported kiwi base or a qcow2 overlay whose backing
# chain includes it. Per-run goldens are overlays; workers clone from the
# golden disk, not from --from-kiwi, and still need OVMF.
qdistro_backing_needs_ovmf() {
    local disk="$1" kiwi disk_real kiwi_real
    kiwi="$(qdistro_kiwi_base_path)"
    [ -n "$disk" ] && [ -f "$disk" ] && [ -f "$kiwi" ] || return 1
    disk_real="$(realpath -e -- "$disk" 2>/dev/null)" || return 1
    kiwi_real="$(realpath -e -- "$kiwi" 2>/dev/null)" || return 1
    [ "$disk_real" = "$kiwi_real" ] && return 0
    qemu-img info --backing-chain -U "$disk" 2>/dev/null | grep -qF "$kiwi_real"
}

qdistro_vm_base_kind() {
    local want="${QDISTRO_VM_BASE:-auto}"
    local kiwi
    kiwi="$(qdistro_kiwi_base_path)"
    case "$want" in
        baked)
            printf '%s\n' baked
            ;;
        kiwi)
            if ! qdistro_kiwi_base_ok; then
                echo "ERROR: QDISTRO_VM_BASE=kiwi but $kiwi is not a stamped qcow2 — run scripts/vm/import-kiwi-base.sh" >&2
                return 2
            fi
            printf '%s\n' kiwi
            ;;
        auto|"")
            if qdistro_kiwi_base_ok; then
                printf '%s\n' kiwi
            else
                printf '%s\n' baked
            fi
            ;;
        *)
            echo "ERROR: QDISTRO_VM_BASE=$want (want auto|kiwi|baked)" >&2
            return 2
            ;;
    esac
}
