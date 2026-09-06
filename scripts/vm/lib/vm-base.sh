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

qdistro_vm_base_kind() {
    local img="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
    local want="${QDISTRO_VM_BASE:-auto}"
    local kiwi
    kiwi="$(qdistro_kiwi_base_path)"
    case "$want" in
        baked)
            printf '%s\n' baked
            ;;
        kiwi)
            if [ ! -f "$kiwi" ]; then
                echo "ERROR: QDISTRO_VM_BASE=kiwi but $kiwi is missing — run scripts/vm/import-kiwi-base.sh" >&2
                return 2
            fi
            printf '%s\n' kiwi
            ;;
        auto|"")
            if [ -f "$kiwi" ]; then
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
