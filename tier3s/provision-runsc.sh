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
#
# Concurrency: one exclusive flock on /run/qdistro-runsc/provision.lock (root
# 0700 dir) is taken before the live state is inspected and held until exit,
# through swap, verification, rollback and cleanup. A second invocation waits
# ("waiting for the provisioning lock") and then re-inspects the live state.
# The cached tarball is only a source: it is copied into a private stage dir
# and verified + extracted from that copy; a download goes to a private file
# and is published into the cache by an atomic rename only after its sha512
# matched the pin.
set -euo pipefail
umask 022   # created parents must not be group/other-writable (trusted_chain)

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
        -h|--help) sed -n '2,/^set -euo pipefail$/{/^set -euo/d;p}' "$0"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done

if [ -n "$PREFIX" ]; then
    [ "$(id -u)" -ne 0 ] || die "QDISTRO_RUNSC_PREFIX is a unit-test hook and is refused for root"
    case "$PREFIX" in /*) ;; *) die "QDISTRO_RUNSC_PREFIX must be absolute" ;; esac
    while [ "$PREFIX" != "/" ] && [ "${PREFIX%/}" != "$PREFIX" ]; do PREFIX="${PREFIX%/}"; done
    [ "$PREFIX" != "/" ] || die "QDISTRO_RUNSC_PREFIX=/ is not a test prefix"
    log "TEST MODE: installing under prefix $PREFIX (not a real install)"
else
    [ "$(id -u)" -eq 0 ] || die "must run as root"
    [ "$PIN_OVERRIDE" -eq 0 ] || die "--pin is a unit-test option (needs QDISTRO_RUNSC_PREFIX); a real install uses $HERE/RUNSC_RELEASE"
    for h in QDISTRO_RUNSC_FAIL_AFTER_SWAP QDISTRO_RUNSC_PAUSE_AFTER_SWAP; do
        [ -z "${!h:-}" ] || die "$h is a unit-test hook (needs QDISTRO_RUNSC_PREFIX)"
    done
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

# Owner is root:root for a real install; the TEST prefix uses the caller.
OWN="root:root"; OWN_UID=0; TRUST_STOP=/
[ -z "$PREFIX" ] || { OWN="$(id -un):$(id -gn)"; OWN_UID="$(id -u)"; TRUST_STOP="$PREFIX"; }

# --- trusted parents --------------------------------------------------------
# Every EXISTING component from $1 up to / (to the TEST prefix) must be a real
# directory owned by OWN_UID and not group/other-writable, so nobody else can
# swap the staged tree, the live paths or the lock between check and use.
# Missing components are created later by install -d (0755, root).
trusted_chain() {
    local d="$1" st owner mode
    while :; do
        if [ -e "$d" ] || [ -L "$d" ]; then
            [ ! -L "$d" ] || die "untrusted path: $d is a symlink"
            [ -d "$d" ] || die "untrusted path: $d is not a directory"
            st="$(stat -c '%u %a' -- "$d")"; owner="${st%% *}"; mode="${st#* }"
            [ "$owner" = "$OWN_UID" ] || die "untrusted path: $d owned by uid $owner, want $OWN_UID"
            (( (8#$mode & 8#022) == 0 )) || die "untrusted path: $d is group/other-writable (mode $mode)"
        fi
        [ "$d" != "$TRUST_STOP" ] || return 0
        [ "$d" != / ] || die "untrusted path: walked past / without meeting $TRUST_STOP"
        d="$(dirname -- "$d")"
    done
}
LOCK_DIR="$PREFIX/run/qdistro-runsc"
LOCK="$LOCK_DIR/provision.lock"
for d in "$PREFIX/usr/libexec/qdistro" "$PREFIX/etc/qdistro" "$LOCK_DIR"; do trusted_chain "$d"; done

# --- the transaction lock (held until this process exits) -------------------
install -d -m 0700 "$LOCK_DIR"
trusted_chain "$LOCK_DIR"
if [ -e "$LOCK" ] || [ -L "$LOCK" ]; then
    [ -f "$LOCK" ] && [ ! -L "$LOCK" ] || die "lock $LOCK is not a regular file"
fi
exec {LOCKFD}>>"$LOCK"
[ "$(stat -c '%u' -- "$LOCK")" = "$OWN_UID" ] || die "lock $LOCK not owned by uid $OWN_UID"
if ! flock -n -x "$LOCKFD"; then
    log "waiting for the provisioning lock $LOCK (another provision-runsc.sh holds it)"
    flock -x -w 900 "$LOCKFD" || die "timed out after 900 s waiting for $LOCK"
fi
log "holding the provisioning lock $LOCK"

# --- idempotence: already installed and matching? -------------------------
# Expected tree listing: `<type> <mode> <owner:group> <relpath>` for every
# entry (find -printf %y %m %u:%g %P). Exact match only, so an extra entry,
# a symlink, a lost exec bit or a foreign owner all count as "not installed".
expected_listing() {
    { echo "d 755 $OWN ."; echo "d 755 $OWN gvisor-bin"
      for f in "${!WANT[@]}"; do echo "f 755 $OWN $f"; done; } | LC_ALL=C sort
}
listing() { find "$1" -printf '%y %m %u:%g %P\n' | sed 's/ $/ ./' | LC_ALL=C sort; }
tree_matches() {   # $1 = tree root
    local root="$1" f
    [ -d "$root" ] && [ ! -L "$root" ] || return 1
    [ "$(listing "$root")" = "$(expected_listing)" ] || return 1
    for f in "${!WANT[@]}"; do
        [ "$(sha "$root/$f")" = "${WANT[$f]}" ] || return 1
    done
}
file_is() {        # $1 path, $2 mode, $3 reference content
    [ -f "$1" ] && [ ! -L "$1" ] && [ "$(stat -c '%a %U:%G' "$1")" = "$2 $OWN" ] && cmp -s "$3" "$1"
}
installed_matches() {
    tree_matches "$DEST" || return 1
    file_is "$WRAPPER_DEST" 755 "$WRAPPER_SRC" || return 1
    file_is "$STAMP" 644 "$PIN" || return 1
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
# Verification and extraction use a private copy (STAGE is a fresh 0700
# mktemp dir), so nothing that can write the cache can change the bytes
# between the hash check and tar. A download lands in STAGE first and is
# published into the cache (unique temp name + atomic rename) only after it
# matched the pin.
SRC_DIR="$CACHE_DIR/$REL"
TARBALL="$SRC_DIR/${P[tarball]}"
# A real install stages under /var/tmp regardless of $TMPDIR, and only if
# /var/tmp is root-owned and either sticky or not other-writable: then no one
# else can rename the 0700 stage dir away and substitute their own between
# verification and install. The TEST prefix honours $TMPDIR (tests set it).
if [ -n "$PREFIX" ]; then STAGE_PARENT="${TMPDIR:-/var/tmp}"
else
    STAGE_PARENT=/var/tmp
    st="$(stat -c '%u %a' -- "$STAGE_PARENT")" || die "cannot stat $STAGE_PARENT"
    [ ! -L "$STAGE_PARENT" ] && [ -d "$STAGE_PARENT" ] && [ "${st%% *}" = 0 ] \
        && (( (8#${st#* } & 8#1000) != 0 || (8#${st#* } & 8#022) == 0 )) \
        || die "untrusted stage parent $STAGE_PARENT ($st): must be root-owned, sticky or not group/other-writable"
fi
STAGE="$(mktemp -d "$STAGE_PARENT/runsc-stage.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
mkdir "$STAGE/tree"
PRIV="$STAGE/${P[tarball]}"
if [ -f "$TARBALL" ]; then
    cp -- "$TARBALL" "$PRIV" || die "cannot copy $TARBALL"
    got="$(sha "$PRIV")"
    [ "$got" = "${P[tarball_sha512]}" ] || die "tarball sha512 mismatch: got $got"
    log "tarball sha512 OK ($TARBALL, verified private copy)"
else
    [ "$OFFLINE" -eq 0 ] || die "offline and $TARBALL is not in the cache"
    command -v curl >/dev/null || die "curl missing and tarball not cached"
    log "downloading ${P[base_url]}/${P[tarball]}"
    curl -fsS --proto =https -o "$PRIV" "${P[base_url]}/${P[tarball]}" || die "download failed"
    got="$(sha "$PRIV")"
    [ "$got" = "${P[tarball_sha512]}" ] || die "tarball sha512 mismatch: got $got (download not cached)"
    log "tarball sha512 OK (download)"
    install -d -m 0755 "$SRC_DIR"
    PUB="$(mktemp "$SRC_DIR/.${P[tarball]}.XXXXXX")"
    if cp -- "$PRIV" "$PUB" && chmod 0644 "$PUB" && mv -fT -- "$PUB" "$TARBALL"; then
        log "cached $TARBALL"
    else
        rm -f -- "$PUB"; die "could not publish the verified download to $TARBALL"
    fi
fi
tar --zstd --no-same-owner --no-same-permissions -xf "$PRIV" -C "$STAGE/tree" \
    || die "tarball extraction failed"
TREE="$STAGE/tree"

# Every pinned file present with the pinned hash; no extra executables in
# gvisor-bin/ (extra = release skew we have not reviewed).
for f in "${!WANT[@]}"; do
    [ -f "$TREE/$f" ] && [ ! -L "$TREE/$f" ] || die "bundle missing $f"
    got="$(sha "$TREE/$f")"
    [ "$got" = "${WANT[$f]}" ] || die "sha512 mismatch for $f: got $got"
done
while IFS= read -r f; do
    rel="${f#"$TREE"/}"
    [ -n "${WANT[$rel]:-}" ] || die "unpinned file in gvisor-bin/: $rel"
done < <(find "$TREE/gvisor-bin" -mindepth 1)
log "all ${#WANT[@]} pinned files verified"

# --- assemble everything beside the live paths, then swap ------------------
# Nothing live changes until the new runsc tree, wrapper and stamp are all
# staged and verified. The tree swap is one renameat2(RENAME_EXCHANGE)
# (`mv --exchange`), so /usr/libexec/qdistro/runsc is never absent. Any
# failure after the swap restores the previous tree, wrapper and stamp.
install -d -m 0755 "$PREFIX/usr/libexec/qdistro" "$PREFIX/etc/qdistro"
for d in "$PREFIX/usr/libexec/qdistro" "$PREFIX/etc/qdistro"; do trusted_chain "$d"; done
NEW="$DEST.new.$$"; WNEW="$WRAPPER_DEST.new.$$"; SNEW="$STAMP.new.$$"
WOLD="$WRAPPER_DEST.old.$$"; SOLD="$STAMP.old.$$"
cleanup_new() { rm -rf "$NEW" "$WNEW" "$SNEW"; rm -rf "$STAGE"; }
trap cleanup_new EXIT
rm -rf "$NEW"
install -d -m 0755 "$NEW" "$NEW/gvisor-bin"
for f in "${!WANT[@]}"; do install -m 0755 "$TREE/$f" "$NEW/$f"; done
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
for x in "$WRAPPER_DEST" "$STAMP"; do
    if [ -e "$x" ] || [ -L "$x" ]; then
        [ -f "$x" ] && [ ! -L "$x" ] || die "$x exists and is not a regular file"
    fi
done
# Backups of the live wrapper/stamp; the old tree is kept in $NEW after the
# exchange. All three are deleted only after success or a VERIFIED rollback;
# otherwise they are left on disk and named.
[ ! -e "$WRAPPER_DEST" ] || cp -a "$WRAPPER_DEST" "$WOLD"
[ ! -e "$STAMP" ] || cp -a "$STAMP" "$SOLD"
SWAPPED=0
rollback() {       # returns 0 only if the previous state is back in place
    local ok=0
    log "rolling back"
    if [ "$SWAPPED" -eq 1 ]; then
        if [ "$HAD_OLD" -eq 1 ]; then mv -T --exchange "$NEW" "$DEST" || ok=1
        else rm -rf "$DEST" || ok=1; fi
    fi
    if [ -e "$WOLD" ]; then mv -fT "$WOLD" "$WRAPPER_DEST" || ok=1; else rm -f "$WRAPPER_DEST" || ok=1; fi
    if [ -e "$SOLD" ]; then mv -fT "$SOLD" "$STAMP" || ok=1; else rm -f "$STAMP" || ok=1; fi
    return $ok
}
on_exit() {
    local rc=$1
    if [ "$rc" -ne 0 ]; then
        if ! rollback; then
            # name only what is still on disk (a restore that succeeded has
            # consumed its backup); nothing named here is deleted
            local kept=() x
            for x in "$NEW" "$WOLD" "$SOLD"; do [ ! -e "$x" ] || kept+=("$x"); done
            printf 'provision-runsc: ROLLBACK INCOMPLETE; kept for manual recovery: %s\n' \
                "${kept[*]:-(nothing left to keep)}" >&2
            rm -rf "$STAGE"
            return
        fi
    fi
    rm -f "$WOLD" "$SOLD"; cleanup_new
}
trap 'on_exit $?' EXIT
if [ "$HAD_OLD" -eq 1 ]; then mv -T --exchange "$NEW" "$DEST"   # NEW now holds the old tree
else mv -T "$NEW" "$DEST"; fi
SWAPPED=1
# Unit-test hooks (refused above without the TEST prefix): pause after the
# swap until <dir>/release exists (announcing <dir>/reached), then optionally
# fail, so tests can interleave a second invocation at this exact point.
if [ -n "$PREFIX" ] && [ -n "${QDISTRO_RUNSC_PAUSE_AFTER_SWAP:-}" ]; then
    : > "$QDISTRO_RUNSC_PAUSE_AFTER_SWAP/reached"
    log "TEST: paused after swap"
    for _ in $(seq 1 1200); do [ ! -e "$QDISTRO_RUNSC_PAUSE_AFTER_SWAP/release" ] || break; sleep 0.1; done
    [ -e "$QDISTRO_RUNSC_PAUSE_AFTER_SWAP/release" ] || die "TEST: pause not released within 120 s"
fi
[ -z "${QDISTRO_RUNSC_FAIL_AFTER_SWAP:-}" ] || [ -z "$PREFIX" ] || die "TEST: injected failure after swap"
mv -fT "$WNEW" "$WRAPPER_DEST"
mv -fT "$SNEW" "$STAMP"
installed_matches || die "post-install verification failed"
check_version "$DEST/runsc" || die "post-install version check failed ('$VER_SEEN')"
if [ -n "$PREFIX" ]; then log "PASS (TEST prefix $PREFIX): installed runsc $REL"
else log "PASS: installed runsc $REL to $DEST"; fi
