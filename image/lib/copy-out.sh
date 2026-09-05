#!/bin/bash
# copy-out.sh -- pull the build artifacts off the builder's build disk with
# guestfish (todo/iso/14 Phase C). Sourced by image/build-in-vm.sh and the
# hermetic tests.
#
# qdistro_copy_out <disk> <dest> [device]
#   disk    the builder's build disk (a bare xfs on the whole device, no
#           partition table, so libguestfs inspection finds nothing: the
#           device is mounted explicitly; default /dev/sda)
#   dest    host directory; receives *.raw, bundle/ and the small kiwi
#           result files. Whitespace/quotes are refused: the path goes into
#           a generated guestfish command stream.
# guestfish aborts the WHOLE stream at the first failing command, and a
# `glob` with no match is a failure. Run 24 (2026-09-05) lost its bundle
# exactly so: with installiso="false" there is no *.install.iso, the glob
# for it errored, bundle/ was never copied, and the driver's set -e exited
# on guestfish's rc with no message. So: only the raw and bundle/ are
# required commands; everything optional carries guestfish's `-` prefix
# (ignore this command's error), and the function's own status is
# guestfish's, for the caller's retry loop to judge together with the size
# checks. stderr is left to the caller to redirect.
qdistro_copy_out() {
    local disk="$1" dest="$2" dev="${3:-/dev/sda}"
    for p in "$disk" "$dest"; do
        case "$p" in
            *[[:space:]\"\'\\]*) echo "copy-out: refusing path with whitespace or quotes: $p" >&2; return 2 ;;
        esac
    done
    [ -d "$dest" ] || { echo "copy-out: destination is not a directory: $dest" >&2; return 2; }
    guestfish --ro -a "$disk" -m "$dev" <<EOF
glob copy-out /out/*.raw $dest/
copy-out /out/bundle $dest/
-glob copy-out /out/*.install.iso $dest/
-glob copy-out /out/*.packages $dest/
-glob copy-out /out/*.changes $dest/
-glob copy-out /out/*.verified $dest/
EOF
}

# qdistro_copy_out_settled <disk> <dest> <raw-bytes> <xz-name> <xz-bytes> <iso-bytes|""> <stderr-log>
#   Runs qdistro_copy_out until the host holds a *.raw of exactly <raw-bytes>,
#   bundle/<xz-name> of exactly <xz-bytes> with its .sha256, and (only when
#   <iso-bytes> is given) an .install.iso of that size -- the sizes the
#   driver read inside the VM, so "a raw exists" cannot be satisfied by a
#   previous build or a copy that is still settling (qemu keeps flushing the
#   build qcow2 for seconds after libvirt reports the domain shut off).
#   guestfish's stderr goes to <stderr-log>. QDISTRO_COPY_OUT_TRIES (6) and
#   QDISTRO_COPY_OUT_SLEEP_S (5) exist for the tests. Every probe below is
#   written so that a missing file is a value, never a failing command:
#   under set -euo pipefail, `ls ... | head -1` exits the caller on ls's
#   status 2 (round-2 review: that is what killed runs 24 and 25 right
#   after the copy-out, silently).
qdistro_copy_out_settled() {
    local disk="$1" dest="$2" raw_bytes="$3" xz_name="$4" xz_bytes="$5" iso_bytes="$6" log="$7"
    local tries="${QDISTRO_COPY_OUT_TRIES:-6}" pause="${QDISTRO_COPY_OUT_SLEEP_S:-5}"
    local attempt host_raw host_iso host_xz f raw_ok iso_ok xz_ok
    for attempt in $(seq 1 "$tries"); do
        qdistro_copy_out "$disk" "$dest" 2>>"$log" || true
        host_raw=""; for f in "$dest"/*.raw; do [ -f "$f" ] && { host_raw="$f"; break; }; done
        host_iso=""; for f in "$dest"/*.install.iso; do [ -f "$f" ] && { host_iso="$f"; break; }; done
        host_xz="$dest/bundle/$xz_name"
        raw_ok=0; [ -n "$host_raw" ] && [ "$(stat -c %s "$host_raw")" = "$raw_bytes" ] && raw_ok=1
        iso_ok=1
        if [ -n "$iso_bytes" ]; then
            iso_ok=0; [ -n "$host_iso" ] && [ "$(stat -c %s "$host_iso")" = "$iso_bytes" ] && iso_ok=1
        fi
        xz_ok=0; [ -f "$host_xz" ] && [ "$(stat -c %s "$host_xz")" = "$xz_bytes" ] && [ -s "$host_xz.sha256" ] && xz_ok=1
        if [ "$raw_ok" = 1 ] && [ "$iso_ok" = 1 ] && [ "$xz_ok" = 1 ]; then
            echo "copy-out: settled on attempt $attempt/$tries: $host_raw ($raw_bytes bytes), bundle/$xz_name ($xz_bytes bytes)"
            return 0
        fi
        echo "copy-out: artifacts not settled yet (attempt $attempt/$tries; want raw $raw_bytes bytes, $xz_name $xz_bytes bytes${iso_bytes:+, iso $iso_bytes bytes}); waiting..."
        sleep "$pause"
    done
    return 1
}
