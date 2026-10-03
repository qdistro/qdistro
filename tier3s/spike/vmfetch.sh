#!/bin/bash
# tier3s/spike/vmfetch.sh — HOST side. Copy a directory out of the VM into
# the evidence tree: vmfetch.sh <vm> <guest-dir> <host-dir>
# Transfers tar.gz as base64 between explicit markers (vm-exec's own lines
# ride in the same capture), then checks the guest's sha256 of the archive.
set -euo pipefail
vm=$1 src=$2 dst=$3
here=$(cd "$(dirname "$0")" && pwd)
cap=$(mktemp); trap 'rm -f "$cap" "$cap.tgz"' EXIT
timeout 600 "$here/../../scripts/vm/vm-exec" "$vm" \
  "t=\$(mktemp); tar -C '$src' -czf \$t . && echo SHA=\$(sha256sum < \$t | cut -d' ' -f1) && echo '<<B64' && base64 -w0 < \$t && echo && echo 'B64>>'; rm -f \$t" > "$cap" 2>&1
want=$(sed -n 's/^SHA=\([0-9a-f]*\).*/\1/p' "$cap")
sed -n '/^<<B64$/,/^B64>>$/p' "$cap" | sed '1d;$d' | base64 -d > "$cap.tgz"
got=$(sha256sum < "$cap.tgz" | cut -d' ' -f1)
[ -n "$want" ] && [ "$want" = "$got" ] || { echo "vmfetch: sha mismatch ($want vs $got)" >&2; exit 1; }
mkdir -p "$dst"; tar -C "$dst" -xzf "$cap.tgz"
echo "vmfetch: $src -> $dst ($(du -sh "$dst" | cut -f1), sha256 $got)"
