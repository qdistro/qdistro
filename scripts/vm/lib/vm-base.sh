#!/bin/bash
# vm-base.sh — which backing image spin-test-vm.sh clones.
# Sourced. Prints kiwi|baked on stdout, errors on stderr, returns non-zero
# on a bad QDISTRO_VM_BASE or a required kiwi base that is missing.
#
# QDISTRO_VM_BASE:
#   auto  (default)  qdistro-kiwi-base.qcow2 if imported, else baseweed-baked
#   kiwi             require the imported kiwi image (iso/14 Phase G)
#   baked            always baseweed-baked (build-in-vm.sh hard-wires this)
#
# The kiwi builder must stay on baked: using the kiwi image as the
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

# Canonicalize a qemu-img backing-file string: strip "(actual path:…)",
# resolve relative names against $2 (the overlay that named it), then
# readlink -m. Same rules as ci/lib/vm.sh backing_referrer_state.
qdistro_canonicalize_backing() {
    local stored="$1" overlay="$2" stripped real
    stripped="${stored%% (actual path:*}"
    stripped="${stripped#"${stripped%%[![:space:]]*}"}"
    stripped="${stripped%"${stripped##*[![:space:]]}"}"
    [ -n "$stripped" ] || return 1
    case "$stripped" in
        /*) real=$(readlink -m -- "$stripped" 2>/dev/null) ;;
        *)  real=$(readlink -m -- "$(dirname -- "$overlay")/$stripped" 2>/dev/null) ;;
    esac
    [ -n "$real" ] || return 1
    printf '%s\n' "$real"
}

# True if $1 and $2 are the same path or the same inode (hardlink / bind
# alias). Path compare is the qci default; inode catches a dest that is a
# hardlink of the stored -b (r3 N1). Missing paths are not the same file.
qdistro_same_file() {
    local a="$1" b="$2" ia ib
    [ -n "$a" ] && [ -n "$b" ] || return 1
    [ "$a" = "$b" ] && return 0
    ia=$(stat -c '%d:%i' -- "$a" 2>/dev/null) || return 1
    ib=$(stat -c '%d:%i' -- "$b" 2>/dev/null) || return 1
    [ -n "$ia" ] && [ "$ia" = "$ib" ]
}

# True if $1 is the imported kiwi base or a qcow2 overlay whose backing
# chain includes it. Per-run goldens are overlays; workers clone from the
# golden disk, not from --from-kiwi, and still need OVMF.
# Walk each chain member and canonicalize it; do not grep realpath out of
# qemu-img's stored-string dump (symlink / relative -b miss that grep).
# image: lines become the referrer for the following backing file: so a
# relative inner -b resolves against the overlay that named it, not the
# top worker (r3 N4).
qdistro_backing_needs_ovmf() {
    local disk="$1" kiwi kiwi_real line stored member overlay referrer
    kiwi="$(qdistro_kiwi_base_path)"
    [ -n "$disk" ] && [ -e "$disk" ] && [ -e "$kiwi" ] || return 1
    kiwi_real=$(readlink -m -- "$kiwi" 2>/dev/null) || return 1
    overlay=$(readlink -m -- "$disk" 2>/dev/null) || overlay="$disk"
    qdistro_same_file "$overlay" "$kiwi_real" && return 0
    referrer="$overlay"
    while IFS= read -r line; do
        case "$line" in
            "image: "*)
                stored="${line#*: }"
                member="$(qdistro_canonicalize_backing "$stored" "$referrer")" || continue
                qdistro_same_file "$member" "$kiwi_real" && return 0
                referrer="$member"
                ;;
            "backing file: "*)
                stored="${line#*: }"
                member="$(qdistro_canonicalize_backing "$stored" "$referrer")" || continue
                qdistro_same_file "$member" "$kiwi_real" && return 0
                ;;
        esac
    done < <(qemu-img info --backing-chain -U "$disk" 2>/dev/null)
    return 1
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
