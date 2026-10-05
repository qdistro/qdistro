#!/bin/bash
# tier3s/spike/run-phase-c2.sh — HOST side driver for the Phase C2 stage-1
# spike.  run-phase-c2.sh <vm> <logdir>
# Stages the spike scripts into the VM and runs c2-silo-uid.sh as root in the
# guest via vmlog.sh (vm-exec). The host only stages + collects evidence.
# Commit before launching; never edit while it runs.
set -uo pipefail
vm=$1 L=$2
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
VMLOG_BIN=$here/vmlog.sh
FAILS=0
VMLOG() { "$VMLOG_BIN" "$@" || { echo "STEP FAILED (rc=$?): $1"; FAILS=$((FAILS+1)); }; }

[ -e "$L/10-stage.log" ] && { echo "refusing: $L already has a run (vmlog appends)"; exit 2; }
mkdir -p "$L"

echo "== stage spike sources into $vm (commit $(git -C "$repo" rev-parse --short HEAD))"
# vm-exec cannot push files; serve the spike dir over the guest's host route
# like fresh-vm-bootstrap does (10.0.2.2:8765) — a one-shot server for this run.
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"; kill $SRV_PID 2>/dev/null' EXIT
tar czf "$STAGE/spike.tgz" -C "$here" c2-lib.sh c2-silo-uid.sh smoke.json lib.sh
(cd "$STAGE" && python3 -m http.server 8765 --bind 0.0.0.0 >/dev/null 2>&1) &
SRV_PID=$!
sleep 1

VMLOG "$L/10-stage.log" "$vm" \
  "mkdir -p /root/t3s-c2 && cd /root/t3s-c2 && curl -fsS http://10.0.2.2:8765/spike.tgz | tar xzf - && ls -la && /usr/libexec/qdistro/runsc/runsc --version | head -1 && podman --version"

VMLOG "$L/20-c2-silo-uid.log" "$vm" "bash /root/t3s-c2/c2-silo-uid.sh"

echo "== done; FAILS=$FAILS; logs in $L"
exit $FAILS
