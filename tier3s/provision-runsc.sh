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
# Test hook ONLY: an alternate install root for tests/unit. Refused for root
# (a root run always installs to the real /), and every message says TEST.
PREFIX="${QDISTRO_RUNSC_PREFIX:-}"
PIN_OVERRIDE=0

die() { printf 'provision-runsc: FAIL: %s\n' "$*" >&2; exit 1; }
log() { printf 'provision-runsc: %s\n' "$*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --offline) OFFLINE=1 ;;
        --cache-dir) shift; CACHE_DIR="${1:?--cache-dir needs a value}" ;;
        --pin) shift; PIN="${1:?--pin needs a value}"; PIN_OVERRIDE=1 ;;
        -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done

if [ -n "$PREFIX" ]; then
    [ "$(id -u)" -ne 0 ] || die "QDISTRO_RUNSC_PREFIX is a unit-test hook and is refused for root"
    log "TEST MODE: installing under prefix $PREFIX (not a real install)"
else
    [ "$(id -u)" -eq 0 ] || die "must run as root"
    [ "$PIN_OVERRIDE" -eq 0 ] || die "--pin is a unit-test option (needs QDISTRO_RUNSC_PREFIX); a real install uses $HERE/RUNSC_RELEASE"
fi
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
# Expected tree listing: `<type> <relpath>` for every entry, types from
# find -printf %y (d dir, f regular file, l symlink, ...). Exact match only.
expected_listing() {
    { echo "d gvisor-bin"; for f in "${!WANT[@]}"; do echo "f $f"; done; } | LC_ALL=C sort
}
tree_matches() {   # $1 = tree root
    local root="$1" f
    [ -d "$root" ] && [ ! -L "$root" ] || return 1
    [ "$(find "$root" -mindepth 1 -printf '%y %P\n' | LC_ALL=C sort)" = "$(expected_listing)" ] || return 1
    for f in "${!WANT[@]}"; do
        [ "$(sha "$root/$f")" = "${WANT[$f]}" ] || return 1
    done
}
installed_matches() {
    tree_matches "$DEST" || return 1
    [ -f "$WRAPPER_DEST" ] && [ ! -L "$WRAPPER_DEST" ] && cmp -s "$WRAPPER_SRC" "$WRAPPER_DEST" || return 1
    [ -f "$STAMP" ] && [ ! -L "$STAMP" ] && cmp -s "$PIN" "$STAMP" || return 1
}
check_version() {  # $1 = runsc path; exit 0 AND exact first line required
    local out rc
    out="$(env -i PATH=/usr/bin:/bin "$1" --version 2>&1)"; rc=$?
    VER_SEEN="$(printf '%s\n' "$out" | head -1)"
    [ "$rc" -eq 0 ] && [ "$VER_SEEN" = "${P[version_string]}" ]
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

# --- assemble everything beside the live paths, then swap ------------------
# Nothing live changes until the new runsc tree, wrapper and stamp are all
# staged and verified. The tree swap is one renameat2(RENAME_EXCHANGE)
# (`mv --exchange`), so /usr/libexec/qdistro/runsc is never absent. Any
# failure after the swap restores the previous tree, wrapper and stamp.
install -d -m 0755 "$PREFIX/usr/libexec/qdistro" "$PREFIX/etc/qdistro"
NEW="$DEST.new.$$"; WNEW="$WRAPPER_DEST.new.$$"; SNEW="$STAMP.new.$$"
WOLD="$WRAPPER_DEST.old.$$"; SOLD="$STAMP.old.$$"
cleanup_new() { rm -rf "$NEW" "$WNEW" "$SNEW"; rm -rf "$STAGE"; }
trap cleanup_new EXIT
rm -rf "$NEW"
install -d -m 0755 "$NEW" "$NEW/gvisor-bin"
for f in "${!WANT[@]}"; do install -m 0755 "$STAGE/$f" "$NEW/$f"; done
install -m 0755 "$WRAPPER_SRC" "$WNEW"
install -m 0644 "$PIN" "$SNEW"
[ -n "$PREFIX" ] || chown -R root:root "$NEW" "$WNEW" "$SNEW"
tree_matches "$NEW" || die "staged tree does not match the pin"
check_version "$NEW/runsc" || die "release skew: runsc --version says '$VER_SEEN', pin says '${P[version_string]}'"
log "runsc --version: $VER_SEEN"

HAD_OLD=0; [ -e "$DEST" ] && HAD_OLD=1
if [ "$HAD_OLD" -eq 1 ] && { [ -L "$DEST" ] || [ ! -d "$DEST" ]; }; then
    die "$DEST exists and is not a directory"
fi
SWAPPED=0
rollback() {
    log "rolling back"
    if [ "$SWAPPED" -eq 1 ]; then
        if [ "$HAD_OLD" -eq 1 ]; then mv --exchange "$NEW" "$DEST"; else rm -rf "$DEST"; fi
    fi
    if [ -e "$WOLD" ]; then mv -f "$WOLD" "$WRAPPER_DEST"; else rm -f "$WRAPPER_DEST"; fi
    if [ -e "$SOLD" ]; then mv -f "$SOLD" "$STAMP"; else rm -f "$STAMP"; fi
}
[ ! -e "$WRAPPER_DEST" ] || cp -a "$WRAPPER_DEST" "$WOLD"
[ ! -e "$STAMP" ] || cp -a "$STAMP" "$SOLD"
trap 'rc=$?; [ $rc -eq 0 ] || rollback; rm -f "$WOLD" "$SOLD"; cleanup_new' EXIT
if [ "$HAD_OLD" -eq 1 ]; then mv --exchange "$NEW" "$DEST"   # NEW now holds the old tree
else mv -T "$NEW" "$DEST"; fi
SWAPPED=1
mv -f "$WNEW" "$WRAPPER_DEST"
mv -f "$SNEW" "$STAMP"
installed_matches || die "post-install verification failed"
check_version "$DEST/runsc" || die "post-install version check failed ('$VER_SEEN')"
if [ -n "$PREFIX" ]; then log "PASS (TEST prefix $PREFIX): installed runsc $REL"
else log "PASS: installed runsc $REL to $DEST"; fi
