#!/bin/bash
# spin-test-vm.sh — one entry point from scratch to a working qdwin
# session in a libvirt VM. Idempotent on the slow stages.
#
# Chain:
#   1. build-baseweed-from-scratch.sh   (if baseweed-admin.qcow2 absent)
#   2. build-baked-baseweed.sh          (if baseweed-baked.qcow2 absent)
#   3. clone-baseweed.sh --from-baked   (fresh disposable VM)
#   4. fresh-vm-bootstrap.sh in VM      (build qdwin from source, install services)
#   5. bootstrap-qdwin-in-vm.sh in VM   (install greetd-qdwin + qdshell + RDP cert)
#   6. systemctl start greetd-qdwin.service
#   7. ping ctrl-socket; print PASS/FAIL + VM name
#
# Usage:
#   QDISTRO_VM_PASSWORD=<pw> QDWIN_VM_TEMPLATE=<existing-template-vm> \
#     scripts/vm/spin-test-vm.sh [<prefix>]
#
# Default prefix is "qd-test". Resulting VM name is printed on the
# last line of stdout (other progress goes to stderr).

set -euo pipefail

: "${QDISTRO_VM_PASSWORD:?must export QDISTRO_VM_PASSWORD}"
: "${QDWIN_VM_TEMPLATE:?must export QDWIN_VM_TEMPLATE (name of an existing libvirt domain whose XML we clone)}"

PREFIX="${1:-qd-test}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMG="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"

log() { echo "[spin-test-vm] $*" >&2; }

# Stage 1.
if [ ! -f "$IMG/baseweed-admin.qcow2" ]; then
    log "stage 1: building baseweed-admin.qcow2 from scratch (~5-10 min)..."
    bash "$REPO/scripts/vm/build-baseweed-from-scratch.sh" >&2
else
    log "stage 1: baseweed-admin.qcow2 already present"
fi

# Stage 2.
if [ ! -f "$IMG/baseweed-baked.qcow2" ]; then
    log "stage 2: baking 82 packages onto overlay (~15-25 min)..."
    bash "$REPO/scripts/vm/build-baked-baseweed.sh" >&2
else
    log "stage 2: baseweed-baked.qcow2 already present"
fi

# Stage 3.
log "stage 3: cloning a fresh VM from baked..."
VM=$(bash "$REPO/scripts/vm/clone-baseweed.sh" "$PREFIX" --from-baked | tail -1)
log "    VM = $VM"

log "    starting + waiting for guest agent..."
virsh -c qemu:///session start "$VM" >/dev/null 2>&1 || true
"$SCRIPT_DIR/vm-start-and-wait" "$VM" >&2

# Stage 4: host needs to serve compositor/ over HTTP so the VM can wget.
HTTP_PID=""
if ! ss -tln 2>/dev/null | grep -q ':8765 '; then
    log "stage 4: starting host HTTP server on 127.0.0.1:8765..."
    (cd "$REPO/compositor" && python3 -m http.server 8765 --bind 127.0.0.1 >/tmp/spin-http.log 2>&1) &
    HTTP_PID=$!
    sleep 1
fi
trap '[ -n "$HTTP_PID" ] && kill "$HTTP_PID" 2>/dev/null || true' EXIT

log "stage 4: running fresh-vm-bootstrap.sh in VM..."
"$SCRIPT_DIR/vm-exec" "$VM" "wget -q -O /root/fresh-vm-bootstrap.sh http://10.0.2.2:8765/spike-6.5/fresh-vm-bootstrap.sh" >&2
B64=$(base64 -w0 <<EOF
export QDISTRO_VM_PASSWORD=$QDISTRO_VM_PASSWORD QDISTRO_ADMIN_USER=admin
bash /root/fresh-vm-bootstrap.sh
EOF
)
"$SCRIPT_DIR/vm-exec" "$VM" "echo $B64 | base64 -d | bash" >&2

# Stage 5: install + start the qdwin session.
log "stage 5: installing qdwin session + starting greetd-qdwin..."
"$SCRIPT_DIR/vm-exec" "$VM" "bash /root/qdwin-src/deploy/bootstrap-qdwin-in-vm.sh && systemctl start greetd-qdwin.service" >&2

# Stage 6: verify ctrl-socket responds.
sleep 3
REPLY=$("$SCRIPT_DIR/vm-exec" "$VM" "echo list | socat - UNIX-CONNECT:/run/user/1000/qdshell.sock 2>&1 | head -1" 2>/dev/null | tail -1)
if [ "${REPLY:-}" = "ok list" ]; then
    log "PASS: qdshell ctrl-socket responsive"
else
    log "FAIL: ctrl-socket reply was: $REPLY"
    exit 4
fi

log "ready. VM:"
echo "$VM"
