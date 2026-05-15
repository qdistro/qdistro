#!/bin/bash
# Push the local config.xml/build.sh/config.sh into the running builder VM
# and re-run kiwi. Faster than re-running build-in-vm.sh from scratch.
set -euo pipefail
VM="${1:-$(virsh -c qemu:///session list --name 2>/dev/null | grep -m1 ^qdistro-builder-)}"
[ -n "$VM" ] || { echo "no qdistro-builder VM running" >&2; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
EXEC=/home/playai/doc/qdistro-org/qdistro/scripts/vm/vm-exec
SCRIPT=/home/playai/doc/qdistro-org/qdistro/scripts/vm/vm-script

B64_CFG=$(base64 -w0 < "$HERE/config.xml")
B64_BUILD=$(base64 -w0 < "$HERE/build.sh")
B64_CONFSH=$(base64 -w0 < "$HERE/config.sh")

cat <<PUSH | "$SCRIPT" "$VM"
set -e
echo "$B64_CFG" | base64 -d > /root/qdistro-image/config.xml
echo "$B64_BUILD" | base64 -d > /root/qdistro-image/build.sh
echo "$B64_CONFSH" | base64 -d > /root/qdistro-image/config.sh
chmod +x /root/qdistro-image/build.sh /root/qdistro-image/config.sh
echo "config.xml -> \$(wc -l < /root/qdistro-image/config.xml) lines"
PUSH

echo "[iterate] cleaning previous build state and re-running kiwi..."
cat <<'EOS' | "$SCRIPT" "$VM"
set -e
rm -rf /build/out /root/kiwi-build.log
cd /root/qdistro-image
QDISTRO_BUILD_DIR=/build/out bash build.sh --no-sync \
    >/root/kiwi-build.log 2>&1 &
KIWI_PID=$!
echo "kiwi-pid=$KIWI_PID"
disown
EOS

echo "[iterate] kiwi started in VM; tail with:"
echo "  $EXEC $VM 'tail -f /root/kiwi-build.log'"
