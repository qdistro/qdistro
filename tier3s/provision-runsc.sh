#!/bin/bash
# provision-runsc.sh — install the pinned upstream gVisor runsc bundle for
# tier 3s (D1 = (a): optional, on demand, NOT in the image, NOT called by any
# installer). Run as root on a dev-profile machine/VM.
#
#   provision-runsc.sh [--offline] [--cache-dir DIR] [--pin FILE]
#
# Source: ${cache_dir}/${release}/gvisor.tar.zstd (cache_dir defaults to
# ${QDISTRO_CACHE_DIR:-/var/cache/qdistro}/runsc). Without --offline a missing
# tarball is downloaded from the pinned base_url; with --offline it is fatal.
# Fails closed: any tarball/file sha512 mismatch, unexpected or missing file,
# or a `runsc --version` that differs from the pin aborts before (or rolls
# back) the install. There is no unpinned fallback.
#
# Installs:
#   /usr/libexec/qdistro/runsc/runsc
#   /usr/libexec/qdistro/runsc/gvisor-bin/<sidecars>
#   /usr/libexec/qdistro/tier3s-runsc          (wrapper)
#   /etc/qdistro/runsc-release                 (copy of the pin)
# all root:root, 0755 (0644 for the release file). Idempotent: an install
# that already matches the pin byte-for-byte is left untouched.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PIN="$HERE/RUNSC_RELEASE"
WRAPPER_SRC="$HERE/tier3s-runsc"
CACHE_DIR="${QDISTRO_CACHE_DIR:-/var/cache/qdistro}/runsc"
OFFLINE=0
PREFIX="${QDISTRO_RUNSC_PREFIX:-}"   # test hook only: alternate root; empty = /

die() { printf 'provision-runsc: FAIL: %s\n' "$*" >&2; exit 1; }
log() { printf 'provision-runsc: %s\n' "$*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --offline) OFFLINE=1 ;;
        --cache-dir) shift; CACHE_DIR="${1:?--cache-dir needs a value}" ;;
        --pin) shift; PIN="${1:?--pin needs a value}" ;;
        -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done

[ -n "$PREFIX" ] || [ "$(id -u)" -eq 0 ] || die "must run as root"
[ -r "$PIN" ] || die "pin file not readable: $PIN"
[ -r "$WRAPPER_SRC" ] || die "wrapper not found: $WRAPPER_SRC"

# --- parse the pin (never source it) ---------------------------------------
declare -A P=()
while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    [[ "$line" =~ ^([A-Za-z0-9_.-]+)=(.*)$ ]] || die "malformed pin line: $line"
    P["${BASH_REMATCH[1]}"]="${BASH_REMATCH[2]}"
done < "$PIN"
for k in release arch base_url version_string tarball tarball_sha512 runsc_sha512; do
    [ -n "${P[$k]:-}" ] || die "pin missing key: $k"
done
is_hash() { [[ "$1" =~ ^[0-9a-f]{128}$ ]]; }
declare -A WANT=()   # relative path in the bundle -> sha512
WANT[runsc]="${P[runsc_sha512]}"
for k in "${!P[@]}"; do
    if [[ "$k" =~ ^sidecar_(.+)_sha512$ ]]; then
        WANT["gvisor-bin/${BASH_REMATCH[1]}"]="${P[$k]}"
    fi
done
[ "${#WANT[@]}" -ge 2 ] || die "pin lists no gvisor-bin sidecars"
is_hash "${P[tarball_sha512]}" || die "pin tarball_sha512 is not a sha512"
for f in "${!WANT[@]}"; do is_hash "${WANT[$f]}" || die "pin hash for $f is not a sha512"; done
[ "$(uname -m)" = "${P[arch]}" ] || die "arch $(uname -m) != pinned ${P[arch]}"

REL="${P[release]}"
DEST="$PREFIX/usr/libexec/qdistro/runsc"
WRAPPER_DEST="$PREFIX/usr/libexec/qdistro/tier3s-runsc"
STAMP="$PREFIX/etc/qdistro/runsc-release"

sha() { sha512sum "$1" | cut -d' ' -f1; }

# --- idempotence: already installed and matching? -------------------------
installed_matches() {
    [ -d "$DEST" ] || return 1
    local f n
    for f in "${!WANT[@]}"; do
        [ -f "$DEST/$f" ] || return 1
        [ "$(sha "$DEST/$f")" = "${WANT[$f]}" ] || return 1
    done
    n="$(find "$DEST" -type f | wc -l)"
    [ "$n" -eq "${#WANT[@]}" ] || return 1
    cmp -s "$WRAPPER_SRC" "$WRAPPER_DEST" || return 1
    cmp -s "$PIN" "$STAMP" || return 1
    return 0
}
if installed_matches; then
    log "already installed and matching pin $REL; nothing to do"
    exit 0
fi

# --- obtain + verify the tarball ------------------------------------------
SRC_DIR="$CACHE_DIR/$REL"
TARBALL="$SRC_DIR/${P[tarball]}"
if [ ! -f "$TARBALL" ]; then
    [ "$OFFLINE" -eq 0 ] || die "offline and $TARBALL is not in the cache"
    command -v curl >/dev/null || die "curl missing and tarball not cached"
    install -d -m 0755 "$SRC_DIR"
    log "downloading ${P[base_url]}/${P[tarball]}"
    curl -fsS --proto =https -o "$TARBALL.part" "${P[base_url]}/${P[tarball]}" \
        || { rm -f "$TARBALL.part"; die "download failed"; }
    mv "$TARBALL.part" "$TARBALL"
fi
got="$(sha "$TARBALL")"
[ "$got" = "${P[tarball_sha512]}" ] || die "tarball sha512 mismatch: got $got"
log "tarball sha512 OK ($TARBALL)"

STAGE="$(mktemp -d "${TMPDIR:-/var/tmp}/runsc-stage.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
tar --zstd --no-same-owner --no-same-permissions -xf "$TARBALL" -C "$STAGE" \
    || die "tarball extraction failed"

# Every pinned file present with the pinned hash; no extra executables in
# gvisor-bin/ (extra = release skew we have not reviewed).
for f in "${!WANT[@]}"; do
    [ -f "$STAGE/$f" ] && [ ! -L "$STAGE/$f" ] || die "bundle missing $f"
    got="$(sha "$STAGE/$f")"
    [ "$got" = "${WANT[$f]}" ] || die "sha512 mismatch for $f: got $got"
done
while IFS= read -r f; do
    rel="${f#"$STAGE"/}"
    [ -n "${WANT[$rel]:-}" ] || die "unpinned file in gvisor-bin/: $rel"
done < <(find "$STAGE/gvisor-bin" -mindepth 1)
log "all ${#WANT[@]} pinned files verified"

# --- assemble the new tree, check the version, swap atomically ------------
install -d -m 0755 "$PREFIX/usr/libexec/qdistro" "$PREFIX/etc/qdistro"
NEW="$DEST.new.$$"
rm -rf "$NEW"
install -d -m 0755 "$NEW/gvisor-bin"
for f in "${!WANT[@]}"; do
    install -m 0755 "$STAGE/$f" "$NEW/$f"
done
[ -n "$PREFIX" ] || chown -R root:root "$NEW"
for f in "${!WANT[@]}"; do
    [ "$(sha "$NEW/$f")" = "${WANT[$f]}" ] || { rm -rf "$NEW"; die "post-copy hash mismatch for $f"; }
done
ver="$(env -i PATH=/usr/bin:/bin "$NEW/runsc" --version 2>&1 | head -1)" || true
if [ "$ver" != "${P[version_string]}" ]; then
    rm -rf "$NEW"
    die "release skew: runsc --version says '$ver', pin says '${P[version_string]}'"
fi
log "runsc --version: $ver"

OLD="$DEST.old.$$"
[ ! -e "$DEST" ] || mv "$DEST" "$OLD"
mv "$NEW" "$DEST"
rm -rf "$OLD"
install -m 0755 "$WRAPPER_SRC" "$WRAPPER_DEST.new.$$" && mv "$WRAPPER_DEST.new.$$" "$WRAPPER_DEST"
install -m 0644 "$PIN" "$STAMP.new.$$" && mv "$STAMP.new.$$" "$STAMP"
[ -n "$PREFIX" ] || chown root:root "$WRAPPER_DEST" "$STAMP"
installed_matches || die "post-install verification failed"
log "PASS: installed runsc $REL to $DEST"
