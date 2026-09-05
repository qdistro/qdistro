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
