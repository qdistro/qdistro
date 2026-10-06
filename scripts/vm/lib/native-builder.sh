# shellcheck shell=bash
# Shared rootless-Podman native builder image resolution.
#
# qdistro_native_builder_base_image
#   Prints the base image ref (registry Tumbleweed container for the pinned
#   snapshot; QCI_PODMAN_IMAGE overrides). Pulls it when absent; honours
#   QCI_OFFLINE=1.
#
# qdistro_ensure_native_builder_image
#   Computes the content-addressed builder key (base image id + snapshot +
#   container-native-deps.sh + Containerfile.native-builder), builds the
#   toolchain image when missing, verifies its snapshot label, and prints
#   the localhost/qdistro/native-builder:<key> ref. The printed ref is what
#   callers pass to `podman run`.
#
# Requires: qdistro_load_test_substrate already run by the caller
# (QDISTRO_SUBSTRATE_SNAPSHOT / _ARCH populated). Same contract as
# build-native-podman.sh — extracted so the host gate's podman build mode
# resolves the identical image instead of duplicating the recipe.

qdistro_native_builder_base_image() {
    local image image_id
    image=${QCI_PODMAN_IMAGE:-registry.opensuse.org/opensuse/tumbleweed:$QDISTRO_SUBSTRATE_SNAPSHOT}
    if ! podman image exists "$image"; then
        case "${QCI_OFFLINE:-0}" in
            1|true|yes|on) echo "ERROR: native base image $image is missing (QCI_OFFLINE=1)" >&2; return 3 ;;
        esac
        podman pull "$image" >&2 || return 3
    fi
    printf '%s\n' "$image"
}

qdistro_native_builder_key() {
    local here image image_id deps_sha
    here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

    command -v podman >/dev/null || { echo 'ERROR: rootless Podman is required' >&2; return 2; }
    [ "$(podman info --format '{{.Host.Security.Rootless}}')" = true ] || {
        echo 'ERROR: Podman must run rootless' >&2; return 2;
    }

    image=$(qdistro_native_builder_base_image) || return $?
    image_id=$(podman image inspect "$image" --format '{{.Id}}') || {
        echo "ERROR: cannot inspect native base image $image" >&2; return 3;
    }

    # Hash stable relative names: absolute worktree paths must not invalidate
    # a shared toolchain cache when the dependency recipe bytes are identical.
    deps_sha=$(cd "$here" && sha256sum container-native-deps.sh Containerfile.native-builder | sha256sum | awk '{print $1}') || return $?
    printf '%s\n' "$image_id" "$QDISTRO_SUBSTRATE_SNAPSHOT" "$deps_sha" | sha256sum | awk '{print $1}'
}

qdistro_ensure_native_builder_image() {
    local here image builder_key builder_image rpm_cache work
    here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
    builder_key=$(qdistro_native_builder_key) || return $?
    builder_image="localhost/qdistro/native-builder:$builder_key"

    if ! podman image exists "$builder_image"; then
        case "${QCI_OFFLINE:-0}" in
            1|true|yes|on)
                echo "ERROR: no cached native builder image for snapshot $QDISTRO_SUBSTRATE_SNAPSHOT (QCI_OFFLINE=1)" >&2
                return 3
                ;;
        esac
        image=$(qdistro_native_builder_base_image) || return $?
        rpm_cache=${QDWIN_CACHE_DIR:-$HOME/.cache/qdistro}/podman-rpm/$QDISTRO_SUBSTRATE_SNAPSHOT/$QDISTRO_SUBSTRATE_ARCH/packages
        mkdir -p "$rpm_cache"
        work=$(mktemp -d "${TMPDIR:-/tmp}/native-builder.XXXXXXXX")
        mkdir -p "$work/builder-context"
        cp "$here/container-native-deps.sh" "$here/Containerfile.native-builder" "$work/builder-context/"
        echo "[native-podman] building toolchain image for snapshot $QDISTRO_SUBSTRATE_SNAPSHOT" >&2
        if ! podman build --pull=never --layers \
            --volume "$rpm_cache:/var/cache/zypp/packages:rw,Z" \
            --build-arg "BASE_IMAGE=$image" \
            --build-arg "SNAPSHOT=$QDISTRO_SUBSTRATE_SNAPSHOT" \
            --file "$work/builder-context/Containerfile.native-builder" \
            --tag "$builder_image" "$work/builder-context" >&2; then
            rm -rf "$work"
            echo 'ERROR: native builder image build failed' >&2
            return 3
        fi
        rm -rf "$work"
    else
        echo "[native-podman] toolchain image cache hit $builder_image" >&2
    fi
    [ "$(podman image inspect "$builder_image" --format '{{index .Labels "org.qdistro.test-snapshot"}}')" = "$QDISTRO_SUBSTRATE_SNAPSHOT" ] || {
        echo 'ERROR: native builder image snapshot mismatch' >&2; return 3;
    }
    printf '%s\n' "$builder_image"
}
