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
CLEANUP_DIRS=("$context")
trap 'rm -rf "${CLEANUP_DIRS[@]}"' EXIT
cp -a . "$context/"
printf '%s\n' "$tier2_snapshot" > "$context/SNAPSHOT"
log "tier-2 workloads pinned to Tumbleweed snapshot $tier2_snapshot"

# qfileman COPYs first-party sources that live outside this directory in a
# monorepo checkout. A copied-only-tier2 tree (s40) can stage the same trees
# under consumer/. weston-terminal / text-viewer / url-preview stay self-
# contained so those builds keep working from a tier2-only copy.
resolve_consumer_src() {
    local repo_rel="$1"
    local staged_name="$2"
    if [ -d "$REPO_ROOT/$repo_rel" ]; then
        printf '%s\n' "$REPO_ROOT/$repo_rel"
        return 0
    fi
    if [ -d "$SCRIPT_DIR/consumer/$staged_name" ]; then
        printf '%s\n' "$SCRIPT_DIR/consumer/$staged_name"
        return 0
    fi
    return 1
}

stage_workload_context() {
    local dest="$1"
    local workload="$2"
    local app pres
    case "$workload" in
        qfileman)
            if ! app="$(resolve_consumer_src qdfileman qdfileman)"; then
                log "FATAL: qfileman sources missing (need $REPO_ROOT/qdfileman or $SCRIPT_DIR/consumer/qdfileman)"
                return 2
            fi
            if ! pres="$(resolve_consumer_src sdk/presentation presentation)"; then
                log "FATAL: presentation sources missing (need $REPO_ROOT/sdk/presentation or $SCRIPT_DIR/consumer/presentation)"
                return 2
            fi
            cp -a "$app" "$dest/qdfileman"
            cp -a "$pres" "$dest/presentation"
            ;;
    esac
    return 0
}

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
    local wcontext

    if [ ! -f "$cf" ]; then
        log "ERROR: $cf not found"
        return 3
    fi

    wcontext="$(mktemp -d "${TMPDIR:-/tmp}/tier2-${workload}.XXXXXX")"
    CLEANUP_DIRS+=("$wcontext")
    cp -a "$context/." "$wcontext/"
    if ! stage_workload_context "$wcontext" "$workload"; then
        return 2
    fi

    log "building $tag from $cf"
    if ! podman build \
            --file "$wcontext/$cf" \
            --build-arg "SNAPSHOT=$tier2_snapshot" \
            --tag "$tag" \
            --layers \
            "$wcontext"; then
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
    if build_workload "$w"; then
        :
    else
        rc=$?
        log "  → build failed for $w"
    fi
done
exit "$rc"
