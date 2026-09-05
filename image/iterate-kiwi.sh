#!/bin/bash
# Push the local config.xml/build.sh/config.sh (plus the source manifest and
# lib/release-stamp.sh, which config.sh reads from the synced tree) into the
# running builder VM and re-run kiwi. Faster than build-in-vm.sh from
# scratch, but it stops at the kiwi log: the copy-out and the host proof of
# the .raw.xz are build-in-vm.sh --reuse's job.
#
# Usage:
#   ./iterate-kiwi.sh              # picks most-recent qdistro-builder-*
#   ./iterate-kiwi.sh <vm-name>    # explicit VM
set -euo pipefail
if [ -n "${1:-}" ]; then
    VM="$1"
else
    # VM names end in YYMMDD-HHMM so lexicographic sort = chronological.
    VM=$(virsh -c qemu:///session list --name 2>/dev/null \
         | grep '^qdistro-builder-' | sort | tail -1)
fi
[ -n "$VM" ] || { echo "no qdistro-builder VM running" >&2; exit 2; }
echo "[iterate] target VM: $VM"
HERE="$(cd "$(dirname "$0")" && pwd)"
EXEC="$HERE/../scripts/vm/vm-exec"
SCRIPT="$HERE/../scripts/vm/vm-script"

# The pin in config.xml must agree with the manifest build.sh --no-sync reads
# (a bumped snapshot would otherwise stop the loop with "re-sync"), so
# refresh the manifest here and push it too. release-stamp.sh is sourced by
# config.sh from the SYNCED tree (/root/qdistro-src/qdistro/image/lib/), not
# from /root/qdistro-image/, so it is pushed there or an edit stays invisible.
bash "$HERE/build.sh" --sync-only >/dev/null
MANIFEST="$HERE/root/root/qdistro-source-manifest"
[ -s "$MANIFEST" ] || { echo "[iterate] no manifest at $MANIFEST" >&2; exit 2; }
B64_CFG=$(base64 -w0 < "$HERE/config.xml")
B64_BUILD=$(base64 -w0 < "$HERE/build.sh")
B64_CONFSH=$(base64 -w0 < "$HERE/config.sh")
B64_MANIFEST=$(base64 -w0 < "$MANIFEST")
B64_STAMP=$(base64 -w0 < "$HERE/lib/release-stamp.sh")

cat <<PUSH | "$SCRIPT" "$VM"
set -e
echo "$B64_CFG" | base64 -d > /root/qdistro-image/config.xml
echo "$B64_BUILD" | base64 -d > /root/qdistro-image/build.sh
echo "$B64_CONFSH" | base64 -d > /root/qdistro-image/config.sh
echo "$B64_MANIFEST" | base64 -d > /root/qdistro-image/root/root/qdistro-source-manifest
mkdir -p /root/qdistro-image/root/root/qdistro-src/qdistro/image/lib
echo "$B64_STAMP" | base64 -d > /root/qdistro-image/root/root/qdistro-src/qdistro/image/lib/release-stamp.sh
chmod +x /root/qdistro-image/build.sh /root/qdistro-image/config.sh
echo "config.xml -> \$(wc -l < /root/qdistro-image/config.xml) lines; snapshot \$(sed -n 's/^SNAPSHOT=//p' /root/qdistro-image/root/root/qdistro-source-manifest)"
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
