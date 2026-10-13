#!/usr/bin/env bash
# Build every flake package and push the closures to the per-user attic
# cache (nix/attic/serve.sh). Paths cache.nixos.org already serves are
# skipped by attic's upstream filter, so only qdistro's own builds upload.
#
#   nix/attic/push.sh            # all packages
#   nix/attic/push.sh qfileman   # selected packages
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
serve=$here/nix/attic/serve.sh
nixc() { NIX_CHECK_KEEP=1 "$here/nix/flake-check.sh" "$@"; }

"$serve" status >/dev/null || "$serve" start

if [ $# -eq 0 ]; then
  mapfile -t attrs < <(nixc eval --json .#packages.x86_64-linux --apply builtins.attrNames \
    | tr -d '[]"' | tr ',' '\n')
else
  attrs=("$@")
fi
refs=()
for a in "${attrs[@]}"; do refs+=(".#$a"); done

mapfile -t paths < <(nixc build --no-link --print-out-paths "${refs[@]}")
[ ${#paths[@]} -gt 0 ] || { echo "push: nothing built" >&2; exit 1; }
echo "push: ${#paths[@]} output path(s) from ${#attrs[@]} package(s)"
"$serve" client push qdistro "${paths[@]}"
