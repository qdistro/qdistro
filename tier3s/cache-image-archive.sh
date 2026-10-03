#!/bin/bash
# tier3s/cache-image-archive.sh — HOST side. Build the tier 3s headless-smoke
# workload image ONCE inside a dev test VM that has registry access, and keep
# it on the host as an OCI archive for the qci bats workers, which load it
# instead of building (todo/paravirt 06 "Provisioning in the qci lane": the
# built tier3s image is transferred as an OCI archive, its image ID asserted
# in the worker log). podman never runs on the host: the host only serves the
# source and stores the bytes the VM uploads.
#
#   tier3s/cache-image-archive.sh --key            print the input key (repo root = cwd's checkout)
#   tier3s/cache-image-archive.sh --dir            print this key's cache directory
#   tier3s/cache-image-archive.sh <vm> [--force]   build in <vm>, store under --dir
#
# The cache directory is ~/.cache/qdistro/tier3s-images/<input key>/ with
# tier3s-headless-smoke.oci.tar and manifest.txt (IMAGE, IMAGE_ID,
# IMAGE_DIGEST, IMAGE_SNAPSHOT, IMAGE_ARCHIVE_SHA256, INPUT_KEY,
# BUILT_FROM_COMMIT, BUILD_VM, BUILT_AT). The input key is the sha256 over the
# snapshot.conf pin and the sha256 of every file the image recipe consumes, so
# a changed Containerfile, smoke script, repo helper or pin selects a new
# (empty) cache entry and the worker setup fails until it is rebuilt. --key
# runs in the guest too (on the staged tested commit): the worker compares it
# with the manifest's INPUT_KEY.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
INPUTS=(tier3s/Containerfile.headless-smoke tier3s/headless-smoke.sh tier3s/configure-snapshot-repos.sh)

input_key() {
    local pin f
    pin=$(sed -n 's/^snapshot=\([0-9]\{8\}\)$/\1/p' "$repo/snapshot.conf" | head -1)
    [ -n "$pin" ] || { echo "no snapshot pin in $repo/snapshot.conf" >&2; return 1; }
    {
        echo "snapshot $pin"
        for f in "${INPUTS[@]}"; do echo "$(sha256sum < "$repo/$f" | cut -d' ' -f1) $f"; done
    } | sha256sum | cut -c1-32
}
cache_dir() { echo "${QDISTRO_TIER3S_IMAGE_CACHE:-$HOME/.cache/qdistro/tier3s-images}/$(input_key)"; }

case "${1:-}" in
    --key) input_key; exit ;;
    --dir) cache_dir; exit ;;
    ''|-*) sed -n '2,24p' "$0" >&2; exit 2 ;;
esac
vm=$1
force=${2:-}
dest=$(cache_dir)
if [ -s "$dest/manifest.txt" ] && [ "$force" != --force ]; then
    echo "cache entry exists: $dest (use --force to rebuild)"; cat "$dest/manifest.txt"; exit 0
fi
[ -z "$(git -C "$repo" status --porcelain -- tier3s snapshot.conf)" ] \
    || { echo "refusing: uncommitted changes under tier3s/ or snapshot.conf (the VM builds git archive HEAD)" >&2; exit 2; }
key=$(input_key)
commit=$(git -C "$repo" rev-parse HEAD)
work=$(mktemp -d /var/tmp/t3s-imgcache.XXXXXX)
trap 'kill "${srv:-}" 2>/dev/null; rm -rf "$work"' EXIT
mkdir -p "$work/serve" "$work/up"
git -C "$repo" archive --format=tar HEAD > "$work/serve/src.tar"
port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
# GET serves src.tar; PUT /<name> stores an upload (only these two names)
python3 - "$port" "$work/serve" "$work/up" > "$work/server.log" 2>&1 <<'PY' &
import http.server, os, sys
port, serve, up = int(sys.argv[1]), sys.argv[2], sys.argv[3]
class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=serve, **k)
    def do_PUT(self):
        name = self.path.lstrip("/")
        if name not in ("image.oci.tar", "manifest.txt"):
            self.send_error(403); return
        n = int(self.headers["Content-Length"])
        with open(os.path.join(up, name), "wb") as fh:
            while n:
                b = self.rfile.read(min(n, 1 << 20))
                if not b: break
                fh.write(b); n -= len(b)
        self.send_response(201 if n == 0 else 400); self.end_headers()
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
PY
srv=$!
sleep 1
U=http://10.0.2.2:$port
guest=$(cat <<EOF
set -euo pipefail
d=/var/tmp/t3s-imgcache; rm -rf "\$d"; mkdir -p "\$d/src"
curl -fsS $U/src.tar | tar -C "\$d/src" -xf -
chown -R admin:users "\$d"
runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin USER=admin LOGNAME=admin \
    XDG_RUNTIME_DIR=/run/user/1000 TMPDIR=/var/tmp \
    bash -c 'cd "\$0/src" && bash tier3s/make-tier3s-image.sh --oci-archive "\$0" headless-smoke' "\$d" > "\$d/build.log" 2>&1 \
    || { tail -30 "\$d/build.log"; exit 1; }
tail -12 "\$d/build.log"
k=\$(cd "\$d/src" && bash tier3s/cache-image-archive.sh --key)
[ "\$k" = "$key" ] || { echo "guest input key \$k != host $key"; exit 1; }
{
    grep -E '^(IMAGE|IMAGE_ID|IMAGE_DIGEST|IMAGE_SNAPSHOT)=' "\$d/build.log"
    echo "IMAGE_ARCHIVE_SHA256=\$(sha256sum < "\$d/tier3s-headless-smoke.oci.tar" | cut -d' ' -f1)"
    echo "INPUT_KEY=\$k"
    echo "BUILT_FROM_COMMIT=$commit"
    echo "BUILD_VM=$vm"
    echo "BUILT_AT=\$(date -u +%FT%TZ)"
} > "\$d/manifest.txt"
cat "\$d/manifest.txt"
curl -fsS -T "\$d/tier3s-headless-smoke.oci.tar" $U/image.oci.tar
curl -fsS -T "\$d/manifest.txt" $U/manifest.txt
EOF
)
timeout 3600 "$repo/scripts/vm/vm-exec" "$vm" "$guest"
want=$(sed -n 's/^IMAGE_ARCHIVE_SHA256=//p' "$work/up/manifest.txt")
got=$(sha256sum < "$work/up/image.oci.tar" | cut -d' ' -f1)
[ -n "$want" ] && [ "$want" = "$got" ] || { echo "uploaded archive sha256 $got != manifest $want" >&2; exit 1; }
mkdir -p "$dest"
mv "$work/up/image.oci.tar" "$dest/tier3s-headless-smoke.oci.tar.new"
mv "$dest/tier3s-headless-smoke.oci.tar.new" "$dest/tier3s-headless-smoke.oci.tar"
mv "$work/up/manifest.txt" "$dest/manifest.txt"
echo "cached: $dest"
cat "$dest/manifest.txt"
