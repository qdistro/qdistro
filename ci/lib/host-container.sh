#!/usr/bin/env bash
# Shared launcher for acceptance rows and explicit development commands.
host_container_run() {
    local image=$1 network=$2; shift 2
    local label='' git_common name run_path npm_cache
    [ "$(cat /sys/fs/selinux/enforce 2>/dev/null || true)" != 1 ] || label=,z
    local -a mounts=(--volume "$QDISTRO_REPO:$QDISTRO_REPO:rw$label") envs=()
    npm_cache=${QDWIN_CACHE_DIR:-$HOME/.cache/qdistro}/host-npm
    mkdir -p "$npm_cache" || return
    npm_cache=$(realpath "$npm_cache") || return
    mounts+=(--volume "$npm_cache:/tmp/qci-npm:rw$label")
    # Linked-worktree .git files point outside the source mount. Git metadata
    # is read-only: tests may inspect provenance but cannot edit another tree.
    git_common=$(git -C "$QDISTRO_REPO" rev-parse --path-format=absolute --git-common-dir) || return
    case "$git_common" in "$QDISTRO_REPO"/*) ;; *) mounts+=(--volume "$git_common:$git_common:ro$label");; esac
    if [ -n "${RDIR:-}" ]; then
        run_path=$(realpath "$RDIR") || return
        mounts+=(--volume "$run_path:$run_path:rw$label")
        envs+=(--env "QCI_HOST_RDIR=$run_path")
    fi
    # Pass documented gate budgets/floors, never the user's display, Python
    # search path, installed library prefixes, or login PATH.
    for name in QCI_HOST_STEP_TIMEOUT QCI_QDISTRO_PYTEST_TIMEOUT QCI_RELEASE QCI_OFFLINE; do
        [ -z "${!name+x}" ] || envs+=(--env "$name=${!name}")
    done
    podman run --rm --pull=never --userns=keep-id --network="$network" \
        "${mounts[@]}" "${envs[@]}" --workdir "$QDISTRO_REPO" \
        --env QT_QPA_PLATFORM=offscreen --env HOME=/tmp/qci-home \
        "$image" bash -c 'mkdir -p "$HOME"; exec "$@"' bash "$@"
}

host_container_gate() {
    local image rc
    mkdir -p "$RDIR/host"
    image=$("$QDISTRO_REPO/ci/bin/qci-host-image" 2>"$RDIR/host/container-image.log") || {
        record_result host container-image fail "$EXIT_BUILD" build build "$RDIR/host/container-image.log" 'host toolchain image unavailable'
        return "$EXIT_BUILD"
    }
    printf '%s\n' "$image" > "$RDIR/host/container-image.txt"
    local network=private
    case "${QCI_OFFLINE:-0}" in 1|true|yes|on) network=none;; esac
    host_container_run "$image" "$network" bash "$QDISTRO_REPO/ci/containers/prepare-host.sh" \
        >"$RDIR/host/container-prep.log" 2>&1 || {
        record_result host container-prep fail "$EXIT_BUILD" build build "$RDIR/host/container-prep.log" 'npm dependency preparation failed'
        return "$EXIT_BUILD"
    }
    rm -f "$RDIR/host/container-complete"
    host_container_run "$image" none bash "$QDISTRO_REPO/ci/containers/run-host.sh" \
        >"$RDIR/host/container.log" 2>&1
    rc=$?
    if [ ! -f "$RDIR/host/container-complete" ]; then
        record_result host container-run fail "$EXIT_HOST" host build "$RDIR/host/container.log" "container did not complete (rc=$rc)"
        return "$EXIT_HOST"
    fi
    return "$rc"
}
