#!/usr/bin/env bash
# Build qdistro's native test-VM payload outside the guest, with rootless Podman.
# Prints the verified cache archive path on stdout; diagnostics go to stderr.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
. "$here/lib/test-substrate.sh"
qdistro_load_test_substrate

command -v podman >/dev/null || { echo 'ERROR: rootless Podman is required' >&2; exit 2; }
[ "$(podman info --format '{{.Host.Security.Rootless}}')" = true ] || {
    echo 'ERROR: Podman must run rootless' >&2; exit 2;
}
image=${QCI_PODMAN_IMAGE:-registry.opensuse.org/opensuse/tumbleweed:latest}
podman image exists "$image" || podman pull "$image" >&2
image_id=$(podman image inspect "$image" --format '{{.Id}}')

cache=${QDWIN_CACHE_DIR:-$HOME/.cache/qdistro}/native-podman/$QDISTRO_SUBSTRATE_SNAPSHOT/$QDISTRO_SUBSTRATE_ARCH
mkdir -p "$cache"
work=$(mktemp -d "$cache/.work.XXXXXXXX")
trap 'rm -rf "$work"' EXIT

# Use Git's visible source set so ignored build outputs cannot change the
# key or sneak into the container. Dirty tracked and visible untracked source
# files are included, and archive metadata is normalized.
git -C "$repo" ls-files -z --cached --others --exclude-standard -- \
    qdwin qdshell daemons qsu selinux \
    scripts/install/install-vendored-libweston.sh \
    scripts/vm/container-build-native.sh \
    scripts/vm/container-check-broker-ratchet.sh \
    | LC_ALL=C sort -zu > "$work/source-files.list"
tar -C "$repo" --mtime=@0 --owner=0 --group=0 --numeric-owner \
    --no-recursion --null -T "$work/source-files.list" -cf "$work/source.tar"
source_sha=$(sha256sum "$work/source.tar" | awk '{print $1}')
builder_sha=$(sha256sum "$here/build-native-podman.sh" | awk '{print $1}')
key=$(printf '%s\n' "$source_sha" "$builder_sha" "$image_id" "$QDISTRO_SUBSTRATE_SNAPSHOT" \
    "${QDWIN_EXTRA_MESON_OPTS:-}" | sha256sum | awk '{print $1}')
archive="$cache/$key.tar"
receipt="$archive.sha256"
exec 9>"$cache/.build.lock"
flock 9
if [ -s "$archive" ] && [ -s "$receipt" ] \
   && (cd "$cache" && sha256sum -c "$(basename "$receipt")" >/dev/null 2>&1); then
    echo "[native-podman] cache hit $archive" >&2
    printf '%s\n' "$archive"
    exit 0
fi

mkdir -p "$work/src" "$work/out/stage"
tar -C "$work/src" -xf "$work/source.tar"
rpm_cache=${QDWIN_CACHE_DIR:-$HOME/.cache/qdistro}/podman-rpm/$QDISTRO_SUBSTRATE_SNAPSHOT/$QDISTRO_SUBSTRATE_ARCH/packages
mkdir -p "$rpm_cache"
echo "[native-podman] building $source_sha against snapshot $QDISTRO_SUBSTRATE_SNAPSHOT ($image_id)" >&2
podman run --rm --pull=never \
    --volume "$work/src:/src:rw,Z" \
    --volume "$work/out:/out:rw,Z" \
    --volume "$rpm_cache:/var/cache/zypp/packages:rw,Z" \
    --env "QDISTRO_SUBSTRATE_SNAPSHOT=$QDISTRO_SUBSTRATE_SNAPSHOT" \
    --env "QDWIN_EXTRA_MESON_OPTS=${QDWIN_EXTRA_MESON_OPTS:-}" \
    --workdir /src "$image" \
    /bin/bash /src/scripts/vm/container-build-native.sh >&2

tar -C "$work/out/stage" --sort=name --mtime=@0 --owner=0 --group=0 \
    --numeric-owner -cf "$work/native.tar" .
tar -tf "$work/native.tar" > "$work/native-files.list"
grep -Fqx './usr/local/bin/qsu' "$work/native-files.list"
sha256sum "$work/native.tar" | awk -v name="$(basename "$archive")" '{print $1 "  " name}' > "$work/native.tar.sha256"
mv "$work/native.tar" "$archive"
mv "$work/native.tar.sha256" "$receipt"
echo "[native-podman] cached $archive" >&2
printf '%s\n' "$archive"
