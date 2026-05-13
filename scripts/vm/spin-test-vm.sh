#!/bin/bash
# spin-test-vm.sh — one entry point from scratch to a qdistro test VM
# with qdwin + qdshell + broker installed.
#
# Pipeline:
#   1. build-baseweed-from-scratch.sh   (if baseweed-admin.qcow2 absent)
#   2. build-baked-baseweed.sh          (if baseweed-baked.qcow2 absent)
#   3. clone-baseweed.sh --from-baked   (fresh disposable VM)
#   4. tarball + HTTP-stage the three sibling repos
#   5. fresh-vm-bootstrap.sh in VM      (build qdwin, build daemons,
#                                        install broker + qdshell)
#   6. systemctl start greetd-qdwin.service
#   7. ping ctrl-socket; print PASS/FAIL + VM name
#
# Sibling-checkout layout required:
#   <parent>/qdistro/   (this repo)
#   <parent>/qdwin/
#   <parent>/qdshell/
#
# Usage:
#   QDISTRO_VM_PASSWORD=<pw> QDWIN_VM_TEMPLATE=<template-domain> \
#     scripts/vm/spin-test-vm.sh [<prefix>]
#
# Default prefix is "qd-test"; VM name (with timestamp suffix) is
# printed on the last stdout line.

set -euo pipefail

: "${QDISTRO_VM_PASSWORD:?must export QDISTRO_VM_PASSWORD (test password baked into cloned VM user accounts)}"

# QDWIN_VM_TEMPLATE: optional. clone-baseweed.sh dumps this domain's
# XML and substitutes name + disk path + MAC for each new test VM.
# If unset, auto-create a minimal qdistro-template domain.
QDWIN_VM_TEMPLATE="${QDWIN_VM_TEMPLATE:-qdistro-template}"
if ! virsh -c qemu:///session dominfo "$QDWIN_VM_TEMPLATE" >/dev/null 2>&1; then
    echo "[spin-test-vm] template domain '$QDWIN_VM_TEMPLATE' not defined; creating..." >&2
    "$(dirname "$0")/create-template-domain.sh" "$QDWIN_VM_TEMPLATE" >&2
fi
export QDWIN_VM_TEMPLATE

PREFIX="${1:-qd-test}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"           # qdistro/
PARENT="$(cd "$REPO/.." && pwd)"                  # qdistro-org/
IMG="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"

log() { echo "[spin-test-vm] $*" >&2; }

# Sanity-check sibling checkout.
for sib in qdwin qdshell; do
    if [ ! -d "$PARENT/$sib" ]; then
        log "ERROR: sibling repo '$sib' not found at $PARENT/$sib"
        log "       Clone codeberg.org/qdistro/$sib next to qdistro/"
        exit 2
    fi
done

# Stage 1.
if [ ! -f "$IMG/baseweed-admin.qcow2" ]; then
    log "stage 1: building baseweed-admin.qcow2 from scratch (~5-10 min)..."
    bash "$REPO/scripts/vm/build-baseweed-from-scratch.sh" >&2
else
    log "stage 1: baseweed-admin.qcow2 already present"
fi

# Stage 2.
if [ ! -f "$IMG/baseweed-baked.qcow2" ]; then
    log "stage 2: baking dependencies onto overlay (~15-25 min)..."
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

# Stage 4: tarball the three sibling repos and serve over SLIRP.
STAGE="$(mktemp -d -t qdistro-stage.XXXXXX)"
trap 'rm -rf "$STAGE"; [ -n "${HTTP_PID:-}" ] && kill "$HTTP_PID" 2>/dev/null || true' EXIT

log "stage 4a: tarballing qdistro, qdwin, qdshell..."
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='.git' --exclude='build' --exclude='build-*' \
    -czf "$STAGE/qdistro.tar.gz" -C "$PARENT/qdistro" .
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='.git' --exclude='build' --exclude='build-*' \
    --exclude='libweston-vendored/src/build' \
    -czf "$STAGE/qdwin.tar.gz" -C "$PARENT/qdwin" .
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='.git' --exclude='build' --exclude='build-*' \
    -czf "$STAGE/qdshell.tar.gz" -C "$PARENT/qdshell" .

# Also stage the bootstrap script next to the tarballs so the VM
# can fetch it before unpacking anything.
cp "$REPO/scripts/vm/fresh-vm-bootstrap.sh" "$STAGE/fresh-vm-bootstrap.sh"

log "stage 4b: starting host HTTP server on 0.0.0.0:8765..."
# A stale server from a prior test run will likely serve from a
# different staging dir (or be bound to 127.0.0.1 only, unreachable
# from the VM's SLIRP NAT view at 10.0.2.2). Take ownership of the
# port: if it's bound, kill the holder and rebind from $STAGE.
if ss -tln 2>/dev/null | awk '{print $4}' | grep -q ':8765$'; then
    log "stage 4b: port 8765 already bound; reclaiming..."
    PIDS=$(ss -tlnp 2>/dev/null | awk '/:8765 / { for(i=1;i<=NF;i++) if (match($i, /pid=([0-9]+)/, m)) print m[1] }' | sort -u)
    if [ -n "$PIDS" ]; then
        kill $PIDS 2>/dev/null || true
        sleep 0.5
    fi
fi
# Bind on 0.0.0.0 so the VM (which reaches us via 10.0.2.2 over SLIRP
# NAT) can connect. Restricting to 127.0.0.1 is unreachable from the
# guest.
(cd "$STAGE" && python3 -m http.server 8765 --bind 0.0.0.0 >/tmp/spin-http.log 2>&1) &
HTTP_PID=$!
sleep 1

log "stage 5: running fresh-vm-bootstrap.sh in VM..."
"$SCRIPT_DIR/vm-exec" "$VM" "wget -q -O /root/fresh-vm-bootstrap.sh http://10.0.2.2:8765/fresh-vm-bootstrap.sh" >&2 \
    || { log "ERROR: failed to fetch bootstrap script (port 8765 reachable?)"; exit 3; }

# Bootstrap fetches the three tarballs and runs the build.
"$SCRIPT_DIR/vm-exec" "$VM" "QDISTRO_VM_PASSWORD='$QDISTRO_VM_PASSWORD' bash /root/fresh-vm-bootstrap.sh" >&2

# Stage 6: verify ctrl-socket responds.
sleep 3
REPLY=$("$SCRIPT_DIR/vm-exec" "$VM" "echo list | socat - UNIX-CONNECT:/run/user/1000/qdshell.sock 2>&1 | head -1" 2>/dev/null | tail -1)
if [ "${REPLY:-}" = "ok list" ]; then
    log "PASS: qdshell ctrl-socket responsive"
else
    log "WARN: ctrl-socket not ready (reply was: ${REPLY:-empty})"
fi

log "ready. VM:"
echo "$VM"
