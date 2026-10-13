#!/usr/bin/env bash
# Run `nix flake check` for this checkout without a host nix install.
#
# A rootless podman container (docker.io/nixos/nix) does the work; its /nix
# store lives in a named volume so later runs reuse every built and
# substituted path. The repo is bind-mounted at the same absolute path, so a
# git worktree's `.git` pointer resolves and nix evaluates the git-tracked
# tree only (untracked build dirs never enter the store).
#
#   nix/flake-check.sh                 # nix flake check -L
#   nix/flake-check.sh build .#qdwin   # any nix subcommand
#   NIX_CHECK_KEEP=0 nix/flake-check.sh   # remove the container afterwards
#
# Env: NIX_CHECK_CONTAINER (default nix-$USER), NIX_CHECK_VOLUME (default
# nix-$USER-store), NIX_CHECK_IMAGE, NIX_CHECK_JOBS / NIX_CHECK_CORES,
# NIX_CHECK_SANDBOX (default true), NIX_EXTRA_CONF (extra nix.conf lines,
# e.g. an attic substituter).
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
common=$(git -C "$here" rev-parse --path-format=absolute --git-common-dir)
repo_root=$(dirname "$common")   # main checkout; contains every worktree

name=${NIX_CHECK_CONTAINER:-nix-$(id -un)}
volume=${NIX_CHECK_VOLUME:-nix-$(id -un)-store}
image=${NIX_CHECK_IMAGE:-docker.io/nixos/nix:latest}
jobs=${NIX_CHECK_JOBS:-8}
cores=${NIX_CHECK_CORES:-4}

case "$here" in
  "$repo_root"|"$repo_root"/*) ;;
  *) echo "flake-check: $here is not under $repo_root" >&2; exit 2 ;;
esac

# Bump when the container's run options change; an older container is
# replaced (the store volume is kept).
spec=2
if podman container exists "$name" &&
   [ "$(podman inspect -f '{{index .Config.Labels "qdistro.flake-check"}}' "$name")" != "$spec" ]; then
  podman rm -f -t 1 "$name" >/dev/null
fi
if ! podman container exists "$name"; then
  # --privileged (rootless: still confined to the user's namespace) lets
  # nix create its build sandbox; without it nix silently falls back to
  # unsandboxed builds that share /tmp and the network.
  # label=disable: rootless + SELinux labels otherwise make the bind mount
  # unreadable inside the container.
  # --network host: reach the per-user attic cache on 127.0.0.1
  # (nix/attic/serve.sh); sandboxed builds still get no network.
  podman run -d --name "$name" --label "qdistro.flake-check=$spec" \
    --privileged --security-opt label=disable --network host \
    -v "$volume:/nix" -v "$repo_root:$repo_root" \
    --entrypoint sleep "$image" infinity >/dev/null
elif [ "$(podman inspect -f '{{.State.Running}}' "$name")" != true ]; then
  podman start "$name" >/dev/null
fi

# nix.conf lives outside the /nix volume, so (re)apply it every run.
podman exec -i "$name" sh -euc '
  # The image ships no sed; keep the pristine file aside and rebuild.
  conf=/etc/nix/nix.conf
  [ -e "$conf.orig" ] || cp "$conf" "$conf.orig"
  {
    cat "$conf.orig"
    echo "# qdistro-flake-check"
    echo "experimental-features = nix-command flakes"
    echo "max-jobs = $1"
    echo "cores = $2"
    echo "sandbox = $4"
    echo "sandbox-fallback = false"
    printf "%s\n" "$3"
  } > "$conf.new"
  mv "$conf.new" "$conf"
  git config --global --get-all safe.directory | grep -qx "\*" \
    || git config --global --add safe.directory "*"
 ' sh "$jobs" "$cores" "${NIX_EXTRA_CONF:-}" "${NIX_CHECK_SANDBOX:-true}"

[ $# -gt 0 ] || set -- flake check -L
rc=0
podman exec -w "$here" "$name" nix "$@" || rc=$?
[ "${NIX_CHECK_KEEP:-1}" = 1 ] || podman rm -f -t 1 "$name" >/dev/null
exit "$rc"
