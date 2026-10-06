#!/bin/bash
# tier3s/spike/run-model-a-verify.sh — HOST side driver for the Phase C2
# stage-2 model-A verification.  run-model-a-verify.sh <vm> <logdir>
#
# Stages `git archive HEAD` into the VM at /root/qdistro-src (root-owned, like
# tests/integration/vm/tier3s-guest-setup.sh), then runs
# tier3s/spike/model-a-verify.sh there: installs the broker + templates +
# session-manager (QDISTRO_TIER3S=1) stack from the staged tree and drives a
# headless-smoke tier3s launch end to end. Commit before launching; never
# edit while it runs.
set -uo pipefail
vm=$1 L=$2
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
VMLOG_BIN=$here/vmlog.sh
PORT="${T3S_MA_PORT:-8771}"
FAILS=0
VMLOG() { "$VMLOG_BIN" "$@" || { echo "STEP FAILED (rc=$?): $1"; FAILS=$((FAILS+1)); }; }

[ -e "$L/10-stage.log" ] && { echo "refusing: $L already has a run (vmlog appends)"; exit 2; }
mkdir -p "$L"

echo "== stage tested commit $(git -C "$repo" rev-parse --short HEAD) into $vm"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"; kill $SRV_PID 2>/dev/null' EXIT
git -C "$repo" archive HEAD > "$STAGE/src.tar"
(cd "$STAGE" && exec python3 -m http.server "$PORT" --bind 0.0.0.0 >/dev/null 2>&1) &
SRV_PID=$!
sleep 1

VMLOG "$L/10-stage.log" "$vm" \
  "rm -rf /root/qdistro-src && mkdir -p /root/qdistro-src && curl -fsS http://10.0.2.2:$PORT/src.tar | tar -C /root/qdistro-src -xf - && chown -R root:root /root/qdistro-src && chmod 0755 /root/qdistro-src && ls /root/qdistro-src | head -20"
[ "$FAILS" -gt 0 ] && { echo "staging failed; aborting before the verify run"; exit $FAILS; }

# The image build inside the run can take a while (registry pull + zypper);
# give vm-exec's deadline counter more room than vmlog's 900 s default.
VMLOG_TIMEOUT=${VMLOG_TIMEOUT:-1700} \
VMLOG "$L/20-model-a-verify.log" "$vm" \
  "bash /root/qdistro-src/tier3s/spike/model-a-verify.sh /root/qdistro-src"

{
  echo "# phase-C2 stage-2 model-A verify evidence ($(basename "$L"))"
  echo
  echo "vm=$vm  commit=$(git -C "$repo" rev-parse --short HEAD)  driver-rc/FAILS=$FAILS"
  echo
  for f in "$L"/*.log; do
    echo "- \`$(basename "$f")\` — $(grep -m1 -oP '(?<=### host ).*(?=: vm-exec)' "$f" 2>/dev/null || date -r "$f" -u +%FT%TZ)"
  done
} > "$L/INDEX.md"

echo "== done; FAILS=$FAILS; logs in $L"
exit $FAILS
