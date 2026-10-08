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
PORT="${T3S_C2_PORT:-0}"   # 0 = kernel-assigned; a fixed port collides across test users
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
# Bind + readback: the server prints the port it ACTUALLY bound, which is
# also proof this run (not a foreign listener) owns it — a stale server on
# the old fixed 8765 would otherwise hand the guest a 404 or wrong tarball.
PORTF="$STAGE/.http-port"; : > "$PORTF"
(cd "$STAGE" && exec python3 -c '
import http.server, socketserver, sys
socketserver.TCPServer.allow_reuse_address = (sys.argv[2] == "1")
httpd = socketserver.TCPServer(("0.0.0.0", int(sys.argv[1])), http.server.SimpleHTTPRequestHandler)
sys.stdout.write(str(httpd.server_address[1]) + "\n"); sys.stdout.flush()
httpd.serve_forever()
' "$PORT" "$([ "$PORT" = 0 ] && echo 0 || echo 1)" >"$PORTF" 2>/dev/null) &
SRV_PID=$!
for _ in $(seq 1 50); do
    PORT=$(head -1 "$PORTF" 2>/dev/null | tr -dc '0-9')
    [ -n "$PORT" ] && break
    kill -0 "$SRV_PID" 2>/dev/null || break
    sleep 0.2
done
[ -n "$PORT" ] || { echo "staging HTTP server failed to bind" >&2; exit 3; }

VMLOG "$L/10-stage.log" "$vm" \
  "mkdir -p /root/t3s-c2 && cd /root/t3s-c2 && curl -fsS http://10.0.2.2:$PORT/spike.tgz | tar xzf - --no-same-owner && chown -R root:root /root/t3s-c2 && ls -la tier3s/ && loginctl enable-linger admin && sleep 2 && ls -ld /run/user/1000"
[ "$FAILS" -gt 0 ] && { echo "staging failed; aborting before probes"; exit $FAILS; }

VMLOG "$L/15-provision-runsc.log" "$vm" \
  "bash /root/t3s-c2/tier3s/provision-runsc.sh && /usr/libexec/qdistro/runsc/runsc --version | head -2"
[ "$FAILS" -gt 0 ] && { echo "provision failed; aborting before probes"; exit $FAILS; }

VMLOG "$L/20-c2-silo-uid.log" "$vm" "bash /root/t3s-c2/c2-silo-uid.sh"

{
  echo "# phase-C2 stage-1 spike evidence ($(basename "$L"))"
  echo
  echo "vm=$vm  commit=$(git -C "$repo" rev-parse --short HEAD)  driver-rc/FAILS=$FAILS"
  echo
  for f in "$L"/*.log; do
    echo "- \`$(basename "$f")\` — $(grep -m1 -oP '(?<=### host ).*(?=: vm-exec)' "$f" 2>/dev/null || date -r "$f" -u +%FT%TZ)"
  done
} > "$L/INDEX.md"

echo "== done; FAILS=$FAILS; logs in $L"
exit $FAILS
