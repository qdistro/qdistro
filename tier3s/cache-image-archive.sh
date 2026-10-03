#!/bin/bash
# tier3s/cache-image-archive.sh — HOST side. Build the tier 3s workload
# images ONCE inside a dev test VM that has registry access, and keep them on
# the host as OCI archives for the qci bats workers, which load them instead
# of building (todo/paravirt 06 "Provisioning in the qci lane": the built
# tier3s image is transferred as an OCI archive, its image ID asserted in the
# worker log). podman never runs on the host: the host only serves the
# source and stores the bytes the VM uploads.
#
#   tier3s/cache-image-archive.sh --key            print the input key (repo root = cwd's checkout)
#   tier3s/cache-image-archive.sh --dir            print this key's cache directory
#   tier3s/cache-image-archive.sh <vm> [--force]   build in <vm>, store under --dir
#
# The cache directory is ~/.cache/qdistro/tier3s-images/<input key>/ with one
# tier3s-<workload>.oci.tar per declared workload image (every
# tier3s/Containerfile.<workload>) and manifest.txt. The manifest carries the
# shared keys (IMAGE_SNAPSHOT, INPUT_KEY, BUILT_FROM_COMMIT, BUILD_VM,
# BUILT_AT) and, per workload <w> (dashes -> underscores), IMAGE_<w>,
# IMAGE_ID_<w>, IMAGE_DIGEST_<w>, IMAGE_ARCHIVE_SHA256_<w>. headless-smoke —
# the workload the current VM drivers load — also keeps the legacy
# unprefixed keys (IMAGE, IMAGE_ID, IMAGE_DIGEST, IMAGE_ARCHIVE_SHA256).
# The input key is the sha256 over the snapshot.conf pin and the sha256 of
# every file the image recipes consume, so a changed Containerfile, entry
# point, smoke script, repo helper or pin selects a new (empty) cache entry
# and the worker setup fails until it is rebuilt. --key runs in the guest
# too (on the staged tested commit): the worker compares it with the
# manifest's INPUT_KEY.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
# Every recipe input, in a fixed order: the Containerfiles themselves (a new
# one is automatically an input), the shared GUI entrypoint, the smoke
# workload script and the repo helper.
mapfile -t INPUTS < <(
    cd "$repo"
    for f in tier3s/Containerfile.*; do [ -f "$f" ] && echo "$f"; done | LC_ALL=C sort
    printf '%s\n' tier3s/qdistro-tier3s-entrypoint tier3s/headless-smoke.sh \
        tier3s/configure-snapshot-repos.sh
)

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
    ''|-*) sed -n '2,29p' "$0" >&2; exit 2 ;;
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
# GET serves src.tar; PUT /<name> stores an upload (manifest.txt and the
# per-workload tier3s-<workload>.oci.tar archives only)
python3 - "$port" "$work/serve" "$work/up" > "$work/server.log" 2>&1 <<'PY' &
import http.server, os, re, sys
port, serve, up = int(sys.argv[1]), sys.argv[2], sys.argv[3]
name_re = re.compile(r"(manifest\.txt|tier3s-[a-z0-9-]+\.oci\.tar)\Z")
class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=serve, **k)
    def do_PUT(self):
        name = self.path.lstrip("/")
        if not name_re.fullmatch(name):
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
    bash -c 'cd "\$0/src" && bash tier3s/make-tier3s-image.sh --oci-archive "\$0"' "\$d" > "\$d/build.log" 2>&1 \
    || { tail -30 "\$d/build.log"; exit 1; }
tail -20 "\$d/build.log"
k=\$(cd "\$d/src" && bash tier3s/cache-image-archive.sh --key)
[ "\$k" = "$key" ] || { echo "guest input key \$k != host $key"; exit 1; }
ws=\$(cd "\$d/src/tier3s" && for f in Containerfile.*; do [ -f "\$f" ] && echo "\${f#Containerfile.}"; done | LC_ALL=C sort)
{
    for w in \$ws; do
        a="\$d/tier3s-\$w.oci.tar"
        [ -s "\$a" ] || { echo "archive for workload \$w missing after the build"; exit 1; }
    done
    # one IMAGE= block per workload in build.log, split on the image tag
    awk '
        /^IMAGE=qdistro\/tier3s-/ { if (w != "") emit(); w = substr(\$0, 22); sub(/:.*/, "", w); blk = \$0 ORS; next }
        /^IMAGE_[A-Z]/ { blk = blk \$0 ORS; next }
        END { if (w != "") emit() }
        function emit(   n, W, i, a, L) {
            W = toupper(w); gsub(/-/, "_", W)
            n = split(blk, L, "\n")
            for (i = 1; i <= n; i++) {
                if (L[i] ~ /^IMAGE=/)            print "IMAGE_" W "=" substr(L[i], 7)
                else if (L[i] ~ /^IMAGE_ID=/)    print "IMAGE_ID_" W "=" substr(L[i], 10)
                else if (L[i] ~ /^IMAGE_DIGEST=/) print "IMAGE_DIGEST_" W "=" substr(L[i], 14)
                else if (L[i] ~ /^IMAGE_SNAPSHOT=/) print "IMAGE_SNAPSHOT=" substr(L[i], 16)
            }
        }' "\$d/build.log"
    for w in \$ws; do
        W=\$(printf '%s' "\$w" | tr 'a-z-' 'A-Z_')
        echo "IMAGE_ARCHIVE_SHA256_\${W}=\$(sha256sum < "\$d/tier3s-\$w.oci.tar" | cut -d' ' -f1)"
    done
    echo "INPUT_KEY=\$k"
    echo "BUILT_FROM_COMMIT=$commit"
    echo "BUILD_VM=$vm"
    echo "BUILT_AT=\$(date -u +%FT%TZ)"
} > "\$d/manifest.txt"
# the current VM drivers load headless-smoke through the legacy unprefixed
# keys; mirror them until B-iii teaches the drivers the per-workload names
for k in IMAGE IMAGE_ID IMAGE_DIGEST IMAGE_ARCHIVE_SHA256; do
    v=\$(sed -n "s/^\${k}_HEADLESS_SMOKE=//p" "\$d/manifest.txt" | head -1)
    [ -n "\$v" ] && echo "\$k=\$v" >> "\$d/manifest.txt"
done
cat "\$d/manifest.txt"
for w in \$ws; do curl -fsS -T "\$d/tier3s-\$w.oci.tar" $U/tier3s-\$w.oci.tar; done
curl -fsS -T "\$d/manifest.txt" $U/manifest.txt
EOF
)
timeout 3600 "$repo/scripts/vm/vm-exec" "$vm" "$guest"
# verify every uploaded archive against the manifest, then install them all
rc=0
for f in "$work/up"/tier3s-*.oci.tar; do
    [ -e "$f" ] || continue
    w="${f##*/tier3s-}"; w="${w%.oci.tar}"; W=$(printf '%s' "$w" | tr 'a-z-' 'A-Z_')
    want=$(sed -n "s/^IMAGE_ARCHIVE_SHA256_$W=//p" "$work/up/manifest.txt")
    got=$(sha256sum < "$f" | cut -d' ' -f1)
    [ -n "$want" ] && [ "$want" = "$got" ] \
        || { echo "uploaded archive $w sha256 $got != manifest ${want:-<missing>}" >&2; rc=1; }
done
[ "$rc" = 0 ] || exit 1
mkdir -p "$dest"
for f in "$work/up"/tier3s-*.oci.tar; do
    [ -e "$f" ] || continue
    n="${f##*/}"
    mv "$f" "$dest/$n.new"; mv "$dest/$n.new" "$dest/$n"
done
mv "$work/up/manifest.txt" "$dest/manifest.txt"
echo "cached: $dest"
cat "$dest/manifest.txt"
