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
# NIX_EXTRA_CONF (extra nix.conf lines, e.g. an attic substituter).
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

if ! podman container exists "$name"; then
  # label=disable: rootless + SELinux labels otherwise make the bind mount
  # unreadable inside the container.
  podman run -d --name "$name" --security-opt label=disable \
    -v "$volume:/nix" -v "$repo_root:$repo_root" \
    --entrypoint sleep "$image" infinity >/dev/null
elif [ "$(podman inspect -f '{{.State.Running}}' "$name")" != true ]; then
  podman start "$name" >/dev/null
fi

# nix.conf lives outside the /nix volume, so (re)apply it every run.
podman exec -i "$name" sh -euc '
  conf=/etc/nix/nix.conf
  sed -i "/^# qdistro-flake-check/,\$d" "$conf"
  {
    echo "# qdistro-flake-check"
    echo "experimental-features = nix-command flakes"
    echo "max-jobs = $1"
    echo "cores = $2"
    printf "%s\n" "$3"
  } >> "$conf"
  git config --global --get-all safe.directory | grep -qx "\*" \
    || git config --global --add safe.directory "*"
' sh "$jobs" "$cores" "${NIX_EXTRA_CONF:-}"

[ $# -gt 0 ] || set -- flake check -L
rc=0
podman exec -w "$here" "$name" nix "$@" || rc=$?
[ "${NIX_CHECK_KEEP:-1}" = 1 ] || podman rm -f -t 1 "$name" >/dev/null
exit "$rc"
