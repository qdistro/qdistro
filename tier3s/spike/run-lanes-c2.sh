#!/bin/bash
# tier3s/spike/run-lanes-c2.sh — HOST side driver for the s12x lane dev-run.
#   run-lanes-c2.sh <vm> <logdir> <driver.sh> [<driver.sh> ...]
#
# Stages `git archive HEAD` into the VM at /root/qdistro-src-t3s (root-owned,
# the path the drivers read the snapshot pin from), plus the guest lib, the
# named drivers and lanes-c2.sh under /var/tmp/t3s-dl, then runs
# lanes-c2.sh. Commit before launching; never edit while it runs.
set -uo pipefail
vm=$1 L=$2; shift 2
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
VMLOG_BIN=$here/vmlog.sh
PORT="${T3S_LANE_PORT:-8772}"
FAILS=0
VMLOG() { "$VMLOG_BIN" "$@" || { echo "STEP FAILED (rc=$?): $1"; FAILS=$((FAILS+1)); }; }

[ -e "$L/10-stage.log" ] && { echo "refusing: $L already has a run (vmlog appends)"; exit 2; }
mkdir -p "$L"

echo "== stage tested commit $(git -C "$repo" rev-parse --short HEAD) + drivers into $vm"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"; kill $SRV_PID 2>/dev/null' EXIT
git -C "$repo" archive HEAD > "$STAGE/src.tar"
cp "$repo/tests/integration/vm/tier3s-guest-lib.sh" "$here/lanes-c2.sh" "$STAGE/"
for d in "$@"; do cp "$repo/tests/integration/vm/$d" "$STAGE/" || exit 2; done
(cd "$STAGE" && exec python3 -m http.server "$PORT" --bind 0.0.0.0 >/dev/null 2>&1) &
SRV_PID=$!
sleep 1

DL_LIST="tier3s-guest-lib.sh lanes-c2.sh $(printf '%s ' "$@")"
VMLOG "$L/10-stage.log" "$vm" \
  "rm -rf /root/qdistro-src-t3s /var/tmp/t3s-dl && mkdir -p /root/qdistro-src-t3s /var/tmp/t3s-dl && curl -fsS http://10.0.2.2:$PORT/src.tar | tar -C /root/qdistro-src-t3s -xf - && chown -R root:root /root/qdistro-src-t3s && chmod 0755 /root/qdistro-src-t3s && cd /var/tmp/t3s-dl && for f in $DL_LIST; do curl -fsS -o \$f http://10.0.2.2:$PORT/\$f || exit 97; done && chmod +x lanes-c2.sh && ls -la /var/tmp/t3s-dl"
[ "$FAILS" -gt 0 ] && { echo "staging failed; aborting"; exit $FAILS; }

# stack reinstall + image build + the drivers; generous deadline (the image
# build inside the VM can take several minutes).
VMLOG_TIMEOUT=${VMLOG_TIMEOUT:-2400} \
VMLOG "$L/20-lanes.log" "$vm" \
  "bash /var/tmp/t3s-dl/lanes-c2.sh /root/qdistro-src-t3s $*"

{
  echo "# s12x lane dev-run evidence ($(basename "$L"))"
  echo
  echo "vm=$vm  commit=$(git -C "$repo" rev-parse --short HEAD)  drivers=$(printf '%s ' "$@") driver-rc/FAILS=$FAILS"
  echo
  for f in "$L"/*.log; do
    echo "- \`$(basename "$f")\` — $(grep -m1 -oP '(?<=### host ).*(?=: vm-exec)' "$f" 2>/dev/null || date -r "$f" -u +%FT%TZ)"
  done
} > "$L/INDEX.md"

echo "== done; FAILS=$FAILS; logs in $L"
exit $FAILS
