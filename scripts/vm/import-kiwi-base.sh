#!/bin/bash
# import-kiwi-base.sh — turn a kiwi .raw / .raw.xz (tester or ci profile)
# into the CI backing image $QDWIN_IMG_DIR/qdistro-kiwi-base.qcow2
# (iso/14 Phase G intermediate).
#
# The qcow2 is sparse (qemu-img convert -S 64k, no cluster compression) so
# clone-baseweed.sh --from-kiwi can overlay it the same way --from-baked
# overlays baseweed-baked.qcow2. A stamp next to the qcow2 records the
# source digest; a matching stamp is a no-op.
#
# Usage:
#   scripts/vm/import-kiwi-base.sh                     # select-artifact default
#   scripts/vm/import-kiwi-base.sh /path/to/file.raw.xz
#   scripts/vm/import-kiwi-base.sh --force [path]      # rebuild even if stamp matches
#
# Env: QDWIN_IMG_DIR, QDISTRO_BUILD_DIR, QDISTRO_IMAGE, QDISTRO_IMAGE_SHA256,
#      QDISTRO_KIWI_BASE (override dest path).

set -euo pipefail

FORCE=0
SRC_ARG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        -h|--help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        --*)
            echo "ERROR: unknown flag $1" >&2
            exit 2
            ;;
        *)
            if [ -n "$SRC_ARG" ]; then
                echo "ERROR: extra positional arg $1" >&2
                exit 2
            fi
            SRC_ARG="$1"
            shift
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=lib/vm-base.sh
. "$SCRIPT_DIR/lib/vm-base.sh"
# shellcheck source=../../image/lib/select-artifact.sh
. "$REPO/image/lib/select-artifact.sh"

DEST="$(qdistro_kiwi_base_path)"
STAMP="${DEST}.stamp"
PARTIAL="${DEST}.partial"
IMG_DIR="$(dirname "$DEST")"
mkdir -p "$IMG_DIR"

log() { echo "[import-kiwi-base] $*" >&2; }

digest_of() {
    sha256sum -- "$1" | awk '{print $1}'
}

SOURCE=""
KIND=""
DIGEST=""

if [ -n "$SRC_ARG" ]; then
    [ -f "$SRC_ARG" ] || { echo "ERROR: not a file: $SRC_ARG" >&2; exit 2; }
    SOURCE="$(realpath -e -- "$SRC_ARG")"
    case "$SOURCE" in
        *.raw.xz) KIND=xz ;;
        *.raw)    KIND=raw ;;
        *.qcow2)  KIND=qcow2 ;;
        *)
            echo "ERROR: want .raw, .raw.xz or .qcow2, got $SOURCE" >&2
            exit 2
            ;;
    esac
    DIGEST="$(digest_of "$SOURCE")"
else
    qdistro_resolve_image || exit $?
    SOURCE="$QDISTRO_RESOLVED_PATH"
    KIND="$QDISTRO_RESOLVED_KIND"
    DIGEST="${QDISTRO_RESOLVED_DIGEST:-}"
    [ -n "$DIGEST" ] || DIGEST="$(digest_of "$SOURCE")"
fi

if [ -f "$DEST" ] && [ -f "$STAMP" ] && [ "$FORCE" != 1 ]; then
    old="$(awk -F= '/^DIGEST=/{print $2; exit}' "$STAMP" || true)"
    if [ "$old" = "$DIGEST" ]; then
        log "already imported (digest $DIGEST) at $DEST"
        qemu-img info "$DEST" | sed 's/^/[import-kiwi-base]   /' >&2
        exit 0
    fi
    log "stamp digest $old != $DIGEST; reconverting"
fi

RAW="$SOURCE"
if [ "$KIND" = xz ]; then
    export QDISTRO_IMAGE="$SOURCE"
    unset QDISTRO_IMAGE_SHA256 2>/dev/null || true
    qdistro_resolve_image || exit $?
    qdistro_materialize_raw || exit $?
    RAW="$QDISTRO_RESOLVED_DISK"
elif [ "$KIND" = qcow2 ]; then
    # Already a qcow2: copy as the base (no convert). Still go through a
    # partial so a crash cannot leave a half-written dest that later
    # clones treat as valid.
    log "copying qcow2 $SOURCE -> $PARTIAL"
    rm -f "$PARTIAL"
    cp --sparse=always -- "$SOURCE" "$PARTIAL"
    qemu-img info "$PARTIAL" >/dev/null
    mv -f "$PARTIAL" "$DEST"
    cat > "$STAMP" <<EOF
SOURCE=$SOURCE
KIND=$KIND
DIGEST=$DIGEST
IMPORTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DEST=$DEST
EOF
    chmod 0644 "$STAMP"
    log "imported $DEST"
    qemu-img info "$DEST" | sed 's/^/[import-kiwi-base]   /' >&2
    exit 0
fi

command -v qemu-img >/dev/null 2>&1 || { echo "ERROR: qemu-img not found" >&2; exit 3; }

log "converting $RAW -> $PARTIAL (sparse qcow2, -S 64k)"
rm -f "$PARTIAL"
# -S 64k: skip zero clusters so a 28 GiB zero-padded raw does not become a
# 28 GiB qcow2. No -c: compressed clusters slow every clone overlay write.
qemu-img convert -p -S 64k -O qcow2 "$RAW" "$PARTIAL"
qemu-img info "$PARTIAL" >/dev/null
mv -f "$PARTIAL" "$DEST"
cat > "$STAMP" <<EOF
SOURCE=$SOURCE
KIND=$KIND
DIGEST=$DIGEST
IMPORTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DEST=$DEST
EOF
chmod 0644 "$STAMP"
log "imported $DEST"
qemu-img info "$DEST" | sed 's/^/[import-kiwi-base]   /' >&2
