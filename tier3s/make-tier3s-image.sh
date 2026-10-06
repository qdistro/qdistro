#!/bin/bash
# copied from tier2/make-tier2-image.sh, unify later
# make-tier3s-image.sh — build qdistro/tier3s-<workload>:latest from
# tier3s/Containerfile.<workload> (tier3s/CONTRACT.md §7). Run as the admin
# user inside a VM (rootless podman; never on the host).
#
#   tier3s/make-tier3s-image.sh [--oci-archive DIR] [workload ...]
#
# Differences from the tier-2 script: outputs are tagged qdistro/tier3s-*;
# the build context is a staged copy of only the files a recipe COPYs (the
# tier3s tree also holds spike evidence); the built image's
# org.qdistro.snapshot label must equal the pin; IMAGE_ID= / IMAGE_DIGEST=
# lines are printed for the worker log; --oci-archive saves
# DIR/tier3s-<workload>.oci.tar so a qci worker without registry access can
# load the exact built image.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SCRIPT_DIR"

log() { printf '[build-tier3s] %s\n' "$*" >&2; }

ARCHIVE_DIR=""
if [ "${1:-}" = --oci-archive ]; then
    ARCHIVE_DIR="${2:?--oci-archive needs a directory}"; shift 2
    [ -d "$ARCHIVE_DIR" ] || { log "FATAL: $ARCHIVE_DIR is not a directory"; exit 2; }
fi

if ! command -v podman >/dev/null 2>&1; then
    log "FATAL: podman not in PATH"
    exit 2
fi

# The one Tumbleweed pin: snapshot.conf of the checkout beside us, else a
# SNAPSHOT staged next to this script (an installed copy), else the image
# stamp. When both of the first two exist they must agree.
pin_from_conf() { sed -n 's/^snapshot=\([0-9]\{8\}\)$/\1/p' "$1" | head -n1; }
snapshot=""
if [ -e "$REPO_ROOT/snapshot.conf" ]; then
    snapshot="$(pin_from_conf "$REPO_ROOT/snapshot.conf")"
    [ -n "$snapshot" ] || { log "FATAL: $REPO_ROOT/snapshot.conf has no snapshot=YYYYMMDD line"; exit 2; }
fi
if [ -s SNAPSHOT ]; then
    local_snapshot="$(<SNAPSHOT)"
    if [[ ! "$local_snapshot" =~ ^[0-9]{8}$ ]]; then
        log "FATAL: $SCRIPT_DIR/SNAPSHOT must contain exactly YYYYMMDD"
        exit 2
    fi
    if [ -n "$snapshot" ] && [ "$local_snapshot" != "$snapshot" ]; then
        log "FATAL: $SCRIPT_DIR/SNAPSHOT ($local_snapshot) does not match snapshot.conf ($snapshot)"
        exit 2
    fi
    snapshot="$local_snapshot"
fi
for stamp in /etc/qdistro/release /etc/qdistro/test-substrate; do
    [ -z "$snapshot" ] && [ -r "$stamp" ] || continue
    snapshot="$(sed -n 's/^SNAPSHOT=\([0-9]\{8\}\)$/\1/p' "$stamp" | head -n1)"
done
if [ -z "$snapshot" ]; then
    log "FATAL: no snapshot pin (snapshot.conf, $SCRIPT_DIR/SNAPSHOT, /etc/qdistro/release or /etc/qdistro/test-substrate)"
    exit 2
fi

# Stage only what the recipes COPY, plus the pin: every Containerfile.*, the
# repo helper, the shared GUI entrypoint, and every workload script.
context="$(mktemp -d "${TMPDIR:-/var/tmp}/tier3s-context.XXXXXX")"
trap 'rm -rf "$context"' EXIT
cp -a Containerfile.* configure-snapshot-repos.sh qdistro-tier3s-entrypoint \
    headless-smoke.sh "$context/"
printf '%s\n' "$snapshot" > "$context/SNAPSHOT"
log "tier-3s workloads pinned to Tumbleweed snapshot $snapshot"

build_workload() {
    local workload="$1"
    local cf="Containerfile.${workload}"
    local tag="qdistro/tier3s-${workload}:latest"
    local label id digest

    [ -f "$cf" ] || { log "ERROR: $cf not found"; return 3; }
    log "building $tag from $cf"
    if ! podman build \
            --file "$context/$cf" \
            --build-arg "SNAPSHOT=$snapshot" \
            --tag "$tag" \
            --layers \
            "$context"; then
        log "FAIL: $tag (podman build returned non-zero)"
        return 1
    fi
    label="$(podman image inspect --format '{{index .Labels "org.qdistro.snapshot"}}' "$tag")" || return 1
    if [ "$label" != "$snapshot" ]; then
        log "FAIL: $tag carries snapshot label '$label', want $snapshot"
        return 1
    fi
    id="$(podman image inspect --format '{{.Id}}' "$tag")" || return 1
    digest="$(podman image inspect --format '{{.Digest}}' "$tag")" || return 1
    printf 'IMAGE=%s\nIMAGE_ID=%s\nIMAGE_DIGEST=%s\nIMAGE_SNAPSHOT=%s\n' "$tag" "$id" "$digest" "$label"
    if [ -n "$ARCHIVE_DIR" ]; then
        rm -f "$ARCHIVE_DIR/tier3s-$workload.oci.tar"
        podman save --format oci-archive -o "$ARCHIVE_DIR/tier3s-$workload.oci.tar" "$tag" || return 1
        printf 'IMAGE_ARCHIVE=%s\nIMAGE_ARCHIVE_SHA256=%s\n' "$ARCHIVE_DIR/tier3s-$workload.oci.tar" \
            "$(sha256sum < "$ARCHIVE_DIR/tier3s-$workload.oci.tar" | cut -d' ' -f1)"
    fi
    log "OK: $tag ($id)"
}

declare -a workloads
if [ "$#" -eq 0 ]; then
    workloads=()
    for f in Containerfile.*; do [ -f "$f" ] && workloads+=("${f#Containerfile.}"); done
    [ "${#workloads[@]}" -gt 0 ] || { log "no Containerfile.* found in $SCRIPT_DIR"; exit 0; }
else
    workloads=("$@")
fi

rc=0
for w in "${workloads[@]}"; do
    if ! build_workload "$w"; then
        rc=1
        log "  → build failed for $w"
    fi
done
exit "$rc"
