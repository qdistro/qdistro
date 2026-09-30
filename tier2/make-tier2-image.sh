#!/bin/bash
# make-tier2-image.sh — build qdistro/tier2-<workload>:latest
# podman images from the Containerfiles in this directory.
#
# Usage:
#   tier2/make-tier2-image.sh [workload ...]
#
# Examples:
#   tier2/make-tier2-image.sh                    # builds all workloads
#   tier2/make-tier2-image.sh weston-terminal    # one workload
#
# Image-per-workload model. Each Containerfile.<workload> produces
# qdistro/tier2-<workload>:latest. See tier2/README.md for the
# rationale.
#
# Idempotent — re-running re-builds with the same tag. Layer cache
# is honoured.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SCRIPT_DIR"

log() { printf '[build-tier2] %s\n' "$*" >&2; }

if ! command -v podman >/dev/null 2>&1; then
    log "FATAL: podman not in PATH"
    exit 2
fi

# The Tumbleweed snapshot the workloads' zypper repos are pinned to. The one
# place it is written is the repo-root snapshot.conf (shared with the image and
# the test VM bases); there is no tracked tier2/SNAPSHOT. Sources, in order:
#   1. snapshot.conf of the checkout beside us;
#   2. a SNAPSHOT file already in this directory: the installed build context
#      (/usr/lib/qdistro/tier2, written by install-templates-for-vm.sh) or a
#      probe's staged copy;
#   3. /etc/qdistro/release on a qdistro image, or /etc/qdistro/test-substrate
#      on a cloud-derived qci VM (drivers that copy tier2/ alone to /tmp).
# When both 1 and 2 exist they must agree; a stale staged pin is refused.
pin_from_conf() { sed -n 's/^snapshot=\([0-9]\{8\}\)$/\1/p' "$1" | head -n1; }
tier2_snapshot=""
if [ -e "$REPO_ROOT/snapshot.conf" ]; then
    tier2_snapshot="$(pin_from_conf "$REPO_ROOT/snapshot.conf")"
    [ -n "$tier2_snapshot" ] || { log "FATAL: $REPO_ROOT/snapshot.conf has no snapshot=YYYYMMDD line"; exit 2; }
fi
if [ -s SNAPSHOT ]; then
    local_snapshot="$(<SNAPSHOT)"
    if [[ ! "$local_snapshot" =~ ^[0-9]{8}$ ]]; then
        log "FATAL: $SCRIPT_DIR/SNAPSHOT must contain exactly YYYYMMDD"
        exit 2
    fi
    if [ -n "$tier2_snapshot" ] && [ "$local_snapshot" != "$tier2_snapshot" ]; then
        log "FATAL: $SCRIPT_DIR/SNAPSHOT ($local_snapshot) does not match snapshot.conf ($tier2_snapshot)"
        exit 2
    fi
    tier2_snapshot="$local_snapshot"
fi
for stamp in /etc/qdistro/release /etc/qdistro/test-substrate; do
    [ -z "$tier2_snapshot" ] && [ -r "$stamp" ] || continue
    tier2_snapshot="$(sed -n 's/^SNAPSHOT=\([0-9]\{8\}\)$/\1/p' "$stamp" | head -n1)"
done
if [ -z "$tier2_snapshot" ]; then
    log "FATAL: no snapshot pin (snapshot.conf, $SCRIPT_DIR/SNAPSHOT, /etc/qdistro/release or /etc/qdistro/test-substrate)"
    exit 2
fi

# Build from a staged copy of this directory so the pin is part of the context
# without writing into the source tree (COPY hashes content, so the layer cache
# still hits across stagings).
context="$(mktemp -d "${TMPDIR:-/tmp}/tier2-context.XXXXXX")"
trap 'rm -rf "$context"' EXIT
cp -a . "$context/"
printf '%s\n' "$tier2_snapshot" > "$context/SNAPSHOT"
log "tier-2 workloads pinned to Tumbleweed snapshot $tier2_snapshot"

discover_workloads() {
    local -a out=()
    local f
    for f in Containerfile.*; do
        [ -f "$f" ] || continue
        out+=("${f#Containerfile.}")
    done
    if [ "${#out[@]}" -eq 0 ]; then
        return
    fi
    printf '%s\n' "${out[@]}"
}

build_workload() {
    local workload="$1"
    local cf="Containerfile.${workload}"
    local tag="qdistro/tier2-${workload}:latest"

    if [ ! -f "$cf" ]; then
        log "ERROR: $cf not found"
        return 3
    fi

    log "building $tag from $cf"
    if ! podman build \
            --file "$context/$cf" \
            --build-arg "SNAPSHOT=$tier2_snapshot" \
            --tag "$tag" \
            --layers \
            "$context"; then
        log "FAIL: $tag (podman build returned non-zero)"
        return 1
    fi
    log "OK: $tag"
}

declare -a workloads
if [ "$#" -eq 0 ]; then
    mapfile -t workloads < <(discover_workloads)
    if [ "${#workloads[@]}" -eq 0 ]; then
        log "no Containerfile.* found in $SCRIPT_DIR"
        exit 0
    fi
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
