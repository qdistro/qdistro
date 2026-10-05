#!/bin/bash
# tier3s/spike/run-phase-c2.sh — HOST side driver for the Phase C2 stage-1
# spike.  run-phase-c2.sh <vm> <logdir>
# Stages the tier3s provision+spike files into the VM, provisions the pinned
# runsc (guest downloads from the pinned base_url over user-net), then runs
# c2-silo-uid.sh as root in the guest via vmlog.sh (vm-exec). The host only
# stages + collects evidence. Commit before launching; never edit while it
# runs.
set -uo pipefail
vm=$1 L=$2
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
VMLOG_BIN=$here/vmlog.sh
PORT="${T3S_C2_PORT:-8765}"
FAILS=0
VMLOG() { "$VMLOG_BIN" "$@" || { echo "STEP FAILED (rc=$?): $1"; FAILS=$((FAILS+1)); }; }

[ -e "$L/10-stage.log" ] && { echo "refusing: $L already has a run (vmlog appends)"; exit 2; }
mkdir -p "$L"

echo "== stage spike+provision sources into $vm (commit $(git -C "$repo" rev-parse --short HEAD))"
# vm-exec cannot push files; serve a tarball over the guest's host route
# (10.0.2.2) like fresh-vm-bootstrap does — a one-shot server for this run.
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"; kill $SRV_PID 2>/dev/null' EXIT
mkdir -p "$STAGE/tier3s"
cp "$here"/../provision-runsc.sh "$here"/../RUNSC_RELEASE "$here"/../tier3s-runsc "$STAGE/tier3s/"
tar czf "$STAGE/spike.tgz" -C "$here" c2-lib.sh c2-silo-uid.sh smoke.json lib.sh -C "$STAGE" tier3s
(cd "$STAGE" && exec python3 -m http.server "$PORT" --bind 0.0.0.0 >/dev/null 2>&1) &
SRV_PID=$!
sleep 1

VMLOG "$L/10-stage.log" "$vm" \
  "mkdir -p /root/t3s-c2 && cd /root/t3s-c2 && curl -fsS http://10.0.2.2:$PORT/spike.tgz | tar xzf - --no-same-owner && chown -R root:root /root/t3s-c2 && ls -la tier3s/ && loginctl enable-linger admin && sleep 2 && ls -ld /run/user/1000"

VMLOG "$L/15-provision-runsc.log" "$vm" \
  "bash /root/t3s-c2/tier3s/provision-runsc.sh && /usr/libexec/qdistro/runsc/runsc --version | head -2"

VMLOG "$L/20-c2-silo-uid.log" "$vm" "bash /root/t3s-c2/c2-silo-uid.sh"

echo "== done; FAILS=$FAILS; logs in $L"
exit $FAILS
