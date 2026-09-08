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

# Keep the container build pin tied to the image release pin. A snapshot bump
# must update both in one change; refusing a mismatch avoids silently drifting
# tier-2 packages back to a different distribution state.
if [ ! -x "$REPO_ROOT/image/build.sh" ] || [ ! -s SNAPSHOT ]; then
    log "FATAL: qdistro image snapshot source or tier2/SNAPSHOT is missing"
    exit 2
fi
tier2_snapshot="$(<SNAPSHOT)"
image_snapshot="$(bash "$REPO_ROOT/image/build.sh" --snapshot-id)" || {
    log "FATAL: could not read qdistro's pinned image snapshot"
    exit 2
}
if [[ ! "$tier2_snapshot" =~ ^[0-9]{8}$ ]] \
        || [ "$tier2_snapshot" != "$image_snapshot" ]; then
    log "FATAL: tier2/SNAPSHOT ($tier2_snapshot) does not match image snapshot ($image_snapshot)"
    exit 2
fi

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
            --file "$cf" \
            --tag "$tag" \
            --layers \
            .; then
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
