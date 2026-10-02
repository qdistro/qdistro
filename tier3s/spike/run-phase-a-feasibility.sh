#!/bin/bash
# tier3s/spike/run-phase-a-feasibility.sh — HOST driver for the Phase A
# (milestone A-i) feasibility checks behind tier3s/CONTRACT.md D-A1/D-A3b.
# Serves `git archive HEAD` + the sha512-checked runsc tarball on 127.0.0.1
# (the guest's 10.0.2.2), stages a root-owned checkout, provisions runsc
# offline, then runs phase-a-feasibility.sh steps through vmlog.sh. Every
# podman/runsc step runs inside the VM.
#   run-phase-a-feasibility.sh <vm> <logdir>     (refuses a non-empty logdir)
set -u
vm=${1:?usage: run-phase-a-feasibility.sh <vm> <logdir>}
out=${2:?usage: run-phase-a-feasibility.sh <vm> <logdir>}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
port=${T3S_PORT:-42941}
if [ -d "$out" ] && [ -n "$(ls -A "$out")" ]; then echo "refusing: $out is not empty" >&2; exit 2; fi
mkdir -p "$out"
pin=$repo/tier3s/RUNSC_RELEASE
rel=$(sed -n 's/^release=//p' "$pin")
want=$(sed -n 's/^tarball_sha512=//p' "$pin")
tarball=$HOME/.cache/qdistro/runsc/$rel/gvisor.tar.zstd
[ "$(sha512sum < "$tarball" | cut -d' ' -f1)" = "$want" ] || { echo "host tarball sha512 != pin" >&2; exit 2; }
serve=$(mktemp -d /var/tmp/t3s-serve.XXXXXX)
git -C "$repo" archive --format=tar HEAD > "$serve/src.tar"
git -C "$repo" rev-parse HEAD > "$serve/commit.txt"
cp "$tarball" "$serve/gvisor.tar.zstd"
python3 -m http.server --bind 127.0.0.1 "$port" --directory "$serve" > /dev/null 2>&1 &
srv=$!
trap 'kill "$srv" 2>/dev/null; wait "$srv" 2>/dev/null; rm -rf "$serve"; echo "host file server stopped"' EXIT
sleep 1
fails=0
step() {
    local rc
    VMLOG_TIMEOUT=${VMLOG_TIMEOUT:-1200} VMLOG_TAIL=0 "$here/vmlog.sh" "$out/$1.log" "$vm" "$2" > /dev/null
    rc=$?
    printf '%-32s rc=%s %s BAD\n' "$1" "$rc" "$(grep -c ': BAD' "$out/$1.log")"
    [ "$rc" -eq 0 ] || fails=$((fails + 1))
}
U=http://10.0.2.2:$port
F=/root/qdistro-src/tier3s/spike/phase-a-feasibility.sh
echo "### host: staged commit $(cat "$serve/commit.txt")" > "$out/00-stage.log"
step 00-stage "set -e; rm -rf /root/qdistro-src; mkdir -p /root/qdistro-src; cd /root/qdistro-src
curl -fsS $U/src.tar | tar -xf -; chown -R root:root /root/qdistro-src; echo \"staged commit \$(curl -fsS $U/commit.txt)\"
mkdir -p /var/cache/qdistro/runsc/$rel; curl -fsS $U/gvisor.tar.zstd -o /var/cache/qdistro/runsc/$rel/gvisor.tar.zstd
cat /etc/qdistro/profile; uname -r; podman --version; systemctl --version | head -1; getenforce
tier3s/provision-runsc.sh --offline --cache-dir /var/cache/qdistro/runsc"
step 01-setup "$F setup"
step 10-a1-fixed-root "$F a1"
step 11-a1-negative "$F a1neg"
step 20-a3b-scope-stop "$F a3b"
step 21-a3b-scope-sigkill "$F a3bkill"
echo "failed steps: $fails"
exit "$fails"
