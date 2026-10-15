#!/bin/bash
# vm-push-file.sh — copy a host file into a guest over vm-exec. Sourced; no
# side effects on source.
#
# vm-exec carries the whole guest command as ONE argv string (jq --arg, then
# virsh's JSON argument), and Linux refuses any single argument over
# MAX_ARG_STRLEN (128 KiB) with E2BIG. An inline `printf '<base64>' |
# base64 -d > dest` therefore stops working once the file passes ~96 KiB:
# the GUI waiter library did, and its delivery failed on every VM in
# full-20261015T072112Z-444803. This sends the base64 in chunks to a part
# file, decodes it, runs an optional check, and only then renames it into
# place, so a failed copy never leaves a truncated file at the destination.

# qdistro_vm_push_file <vm-exec> <vm> <src> <dest> [check-cmd]
# check-cmd (e.g. "bash -n") runs on the decoded file before the rename; a
# nonzero exit leaves the previous <dest> untouched. Returns nonzero on any
# failure. Guest output is discarded. QDISTRO_VM_PUSH_CHUNK sets the chunk
# size in base64 characters (default 48 KiB).
qdistro_vm_push_file() {
    local vmexec=$1 vm=$2 src=$3 dest=$4 check=${5:-} chunk=${QDISTRO_VM_PUSH_CHUNK:-49152}
    local b64 off=0 redirect='>' part new
    # At most 7 digits, checked as text: bash arithmetic wraps a huge value
    # (2^64 -> 0), and a zero chunk would never advance. One argument must
    # stay under 128 KiB anyway.
    case "$chunk" in
        ''|*[!0-9]*|0*|????????*)
            echo "qdistro_vm_push_file: QDISTRO_VM_PUSH_CHUNK must be a positive integer below 10^7, got '$chunk'" >&2
            return 2 ;;
    esac
    case "$dest" in
        *"'"*) echo "qdistro_vm_push_file: destination may not contain a quote: $dest" >&2; return 2 ;;
    esac
    [ -f "$src" ] || { echo "qdistro_vm_push_file: source missing: $src" >&2; return 1; }
    b64=$(base64 -w0 < "$src" 2>/dev/null) || b64=$(base64 < "$src" | tr -d '\n') || return 1
    part="'$dest.part'"
    new="'$dest.new'"
    # base64 has no quote characters, so each chunk is safe inside '...'.
    # An empty file still sends one (empty) chunk so the part file exists.
    while :; do
        "$vmexec" "$vm" "printf '%s' '${b64:off:chunk}' $redirect $part" \
            >/dev/null 2>&1 || return 1
        redirect='>>'
        off=$((off + chunk))
        [ "$off" -lt "${#b64}" ] || break
    done
    "$vmexec" "$vm" "base64 -d < $part > $new && rm -f $part${check:+ && $check $new} && mv -f $new '$dest'" \
        >/dev/null 2>&1
}
