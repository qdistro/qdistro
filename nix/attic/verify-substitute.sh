#!/usr/bin/env bash
# Prove the attic cache serves qdistro's builds: in a throwaway container
# with an EMPTY store, realise packages with --max-jobs 0 (no local builds
# allowed). Every path must come from cache.nixos.org or the attic cache;
# any path needing a build fails the run.
#
#   nix/attic/verify-substitute.sh              # qfileman (the first slice)
#   nix/attic/verify-substitute.sh qdwin qdshell
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
serve=$here/nix/attic/serve.sh
"$serve" status >/dev/null

eval "$("$serve" env)"
export NIX_EXTRA_CONF
export NIX_CHECK_CONTAINER=nix-$(id -un)-clean NIX_CHECK_VOLUME=nix-$(id -un)-clean-store
export NIX_CHECK_KEEP=0

podman rm -f -t 1 "$NIX_CHECK_CONTAINER" >/dev/null 2>&1 || true
podman volume rm -f "$NIX_CHECK_VOLUME" >/dev/null 2>&1 || true
trap 'podman volume rm -f "$NIX_CHECK_VOLUME" >/dev/null 2>&1 || true' EXIT

[ $# -gt 0 ] || set -- qfileman
refs=()
for a in "$@"; do refs+=(".#$a"); done

log=$(mktemp)
rc=0
"$here/nix/flake-check.sh" build --no-link --max-jobs 0 -L "${refs[@]}" 2>&1 | tee "$log" || rc=$?
from_attic=$(grep -c "from '$ATTIC_ENDPOINT/$ATTIC_CACHE'" "$log" || true)
rm -f "$log"
if [ "$rc" -ne 0 ]; then
  echo "verify-substitute: FAIL (a path was not substitutable; see above)" >&2
  exit 1
fi
[ "$from_attic" -gt 0 ] || { echo "verify-substitute: FAIL (nothing came from attic)" >&2; exit 1; }
echo "verify-substitute: OK — $* realised with no local builds; $from_attic path(s) from attic"
