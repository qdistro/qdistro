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
    # Verified, cached acquisition: local store, else digest-keyed saved
    # archive (podman load, zero network), else a verified pull that is
    # itself archived for the next cold host. Offline fails closed.
    # shellcheck source=deps-cache.sh
    . "$(dirname "${BASH_SOURCE[0]}")/deps-cache.sh"
    qd_oci_ensure "$image" >/dev/null || return 3
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
        image=$(qdistro_native_builder_base_image) || return $?
        rpm_cache=${QDWIN_CACHE_DIR:-$HOME/.cache/qdistro}/podman-rpm/$QDISTRO_SUBSTRATE_SNAPSHOT/$QDISTRO_SUBSTRATE_ARCH/packages
        mkdir -p "$rpm_cache"
        work=$(mktemp -d "${TMPDIR:-/tmp}/native-builder.XXXXXXXX")
        mkdir -p "$work/builder-context"
        cp "$here/container-native-deps.sh" "$here/Containerfile.native-builder" "$work/builder-context/"
        # Mount the verified mirror read-only for the RUN zypper steps —
        # file:// needs no qd_serve inside a build. With the mirror wired
        # the build needs no netns at all, so drop it to prove the contract.
        # Must run in THIS shell — a subshell drops the export.
        local -a mirror_args=() net_args=()
        if qd_repo_wire_container "$QDISTRO_SUBSTRATE_SNAPSHOT" /qdistro-mirror; then
            mirror_args=(--volume "$(qd_deps_root)/repos:/qdistro-mirror:ro,z")
            net_args=(--network=none)
        fi
        # Offline, the image is built from the cache alone: the verified OCI
        # base (above) plus the warm mirror under --network=none. Without a
        # warm mirror or an explicit QDISTRO_REPO_BASE there is nothing to
        # install from — fail closed rather than reach upstream.
        if [ "${#mirror_args[@]}" -eq 0 ] && [ -z "${QDISTRO_REPO_BASE:-}" ]; then
            if qd_deps_offline; then
                rm -rf "$work"
                echo "ERROR: no cached native builder image for snapshot $QDISTRO_SUBSTRATE_SNAPSHOT and no warm repo mirror to build one offline (run qd-cache-warm)" >&2
                return 3
            fi
        fi
        echo "[native-podman] building toolchain image for snapshot $QDISTRO_SUBSTRATE_SNAPSHOT" >&2
        if ! podman build --pull=never --layers "${net_args[@]}" "${mirror_args[@]}" \
            --volume "$rpm_cache:/var/cache/zypp/packages:rw,Z" \
            --build-arg "BASE_IMAGE=$image" \
            --build-arg "SNAPSHOT=$QDISTRO_SUBSTRATE_SNAPSHOT" \
            --build-arg "QDISTRO_REPO_BASE=${QDISTRO_REPO_BASE:-}" \
            --build-arg "QDISTRO_DEPS_OFFLINE=${QDISTRO_DEPS_OFFLINE:-}" \
            --build-arg "QCI_OFFLINE=${QCI_OFFLINE:-}" \
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
