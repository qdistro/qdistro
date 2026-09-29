#!/usr/bin/env bash
# Build the test golden's tier-2 workload images once on the rootless host.
# Prints a checksum-verified multi-image Podman archive path on stdout.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
. "$here/lib/test-substrate.sh"
qdistro_load_test_substrate

command -v podman >/dev/null || { echo 'ERROR: rootless Podman is required' >&2; exit 2; }
[ "$(podman info --format '{{.Host.Security.Rootless}}')" = true ] || {
    echo 'ERROR: Podman must run rootless' >&2; exit 2;
}
# These Containerfiles name this image in FROM. Its resolved ID is part of the
# cache key; the test-only build context replaces their old release snapshot.
base_image=registry.opensuse.org/opensuse/tumbleweed:latest
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

# Only visible source files enter the key or the build context. Keep the
# production tier2/SNAPSHOT untouched: test workload images follow the test
# cloud snapshot, while the product image retains its separate release pin.
git -C "$repo" ls-files -z --cached --others --exclude-standard -- tier2 \
    | LC_ALL=C sort -zu > "$work/source-files.list"
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
workloads=(weston-terminal text-viewer url-preview)
tags=()
for workload in "${workloads[@]}"; do
    tag="qdistro/tier2-$workload:latest"
    echo "[tier2-podman] building $tag against test snapshot $QDISTRO_SUBSTRATE_SNAPSHOT" >&2
    podman build --pull=never --layers \
        --file "$work/context/tier2/Containerfile.$workload" \
        --tag "$tag" \
        --label "org.qdistro.test-snapshot=$QDISTRO_SUBSTRATE_SNAPSHOT" \
        "$work/context/tier2" >&2
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
