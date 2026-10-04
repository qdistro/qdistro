#!/usr/bin/env bash
# Build the test golden's tier-2 workload images once on the rootless host.
# Prints a checksum-verified multi-image Podman archive path on stdout.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
. "$here/lib/test-substrate.sh"
qdistro_load_test_substrate
# Rootless Podman needs the user bus the GUI gate dead-ends for its agents.
. "$here/lib/podman-user-bus.sh"
qdistro_podman_user_bus

command -v podman >/dev/null || { echo 'ERROR: rootless Podman is required' >&2; exit 2; }
[ "$(podman info --format '{{.Host.Security.Rootless}}')" = true ] || {
    echo 'ERROR: Podman must run rootless' >&2; exit 2;
}
# These Containerfiles name this image in FROM (tumbleweed:${SNAPSHOT}, passed
# below). Its resolved ID is part of the cache key.
base_image=registry.opensuse.org/opensuse/tumbleweed:$QDISTRO_SUBSTRATE_SNAPSHOT
if ! podman image exists "$base_image"; then
    case "${QCI_OFFLINE:-0}" in
        1|true|yes|on)
            echo "ERROR: tier-2 base image $base_image is missing (QCI_OFFLINE=1)" >&2
            exit 3
            ;;
    esac
    podman pull "$base_image" >&2
fi
base_id=$(podman image inspect "$base_image" --format '{{.Id}}')

cache=${QDWIN_CACHE_DIR:-$HOME/.cache/qdistro}/tier2-podman/$QDISTRO_SUBSTRATE_SNAPSHOT/$QDISTRO_SUBSTRATE_ARCH
mkdir -p "$cache"
work=$(mktemp -d "$cache/.work.XXXXXXXX")
trap 'rm -rf "$work"' EXIT

# Only visible source files enter the key or the build context. The pin is
# written into the staged context from snapshot.conf (tier2/ tracks none).
# qfileman COPYs first-party trees that live outside tier2/; those paths
# must bust the cache and be present in the extracted context.
git -C "$repo" ls-files -z --cached --others --exclude-standard -- \
    tier2 qdfileman sdk/presentation |
    LC_ALL=C sort -zu > "$work/source-files.list"
tar -C "$repo" --mtime=@0 --owner=0 --group=0 --numeric-owner \
    --no-recursion --null -T "$work/source-files.list" -cf "$work/source.tar"
source_sha=$(sha256sum "$work/source.tar" | awk '{print $1}')
recipe_sha=$(sha256sum "$here/build-tier2-podman-cache.sh" | awk '{print $1}')
key=$(printf '%s\n' "$source_sha" "$recipe_sha" "$base_id" \
    "$QDISTRO_SUBSTRATE_SNAPSHOT" | sha256sum | awk '{print $1}')
archive="$cache/$key.tar"
receipt="$archive.sha256"
exec 9>"$cache/.build.lock"
flock 9
if [ -s "$archive" ] && [ -s "$receipt" ] \
   && (cd "$cache" && sha256sum -c "$(basename "$receipt")" >/dev/null 2>&1); then
    echo "[tier2-podman] cache hit $archive" >&2
    printf '%s\n' "$archive"
    exit 0
fi
case "${QCI_OFFLINE:-0}" in
    1|true|yes|on)
        echo "ERROR: no cached tier-2 archive for snapshot $QDISTRO_SUBSTRATE_SNAPSHOT (QCI_OFFLINE=1)" >&2
        exit 3
        ;;
esac

mkdir -p "$work/context"
tar -C "$work/context" -xf "$work/source.tar"
printf '%s\n' "$QDISTRO_SUBSTRATE_SNAPSHOT" > "$work/context/tier2/SNAPSHOT"
# qfileman is the first-party presentation consumer. weston-terminal stays
# the bats-minimum image; do not fold PyQt6 into it. Per-workload context
# keeps qdfileman/ and presentation/ out of weston-terminal/text-viewer/
# url-preview builds.
workloads=(weston-terminal text-viewer url-preview qfileman)
tags=()
for workload in "${workloads[@]}"; do
    tag="qdistro/tier2-$workload:latest"
    echo "[tier2-podman] building $tag against test snapshot $QDISTRO_SUBSTRATE_SNAPSHOT" >&2
    wcontext=$(mktemp -d "$work/wcontext.$workload.XXXXXX")
    cp -a "$work/context/tier2/." "$wcontext/"
    rm -rf "$wcontext/consumer"
    if [ "$workload" = qfileman ]; then
        if [ ! -d "$work/context/qdfileman" ]; then
            echo "ERROR: qdfileman sources missing from cache context" >&2
            exit 2
        fi
        if [ ! -d "$work/context/sdk/presentation" ]; then
            echo "ERROR: presentation sources missing from cache context" >&2
            exit 2
        fi
        cp -a "$work/context/qdfileman" "$wcontext/qdfileman"
        cp -a "$work/context/sdk/presentation" "$wcontext/presentation"
    fi
    podman build --pull=never --layers \
        --file "$wcontext/Containerfile.$workload" \
        --build-arg "SNAPSHOT=$QDISTRO_SUBSTRATE_SNAPSHOT" \
        --tag "$tag" \
        --label "org.qdistro.test-snapshot=$QDISTRO_SUBSTRATE_SNAPSHOT" \
        "$wcontext" >&2
    tags+=("$tag")
done
podman save --multi-image-archive --output "$work/images.tar" "${tags[@]}" >&2
test -s "$work/images.tar"
tar -tf "$work/images.tar" > "$work/archive-files.list"
grep -Fqx manifest.json "$work/archive-files.list"
sha256sum "$work/images.tar" | awk -v name="$(basename "$archive")" '{print $1 "  " name}' > "$work/images.tar.sha256"
mv "$work/images.tar" "$archive"
mv "$work/images.tar.sha256" "$receipt"
echo "[tier2-podman] cached $archive" >&2
printf '%s\n' "$archive"
