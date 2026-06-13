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
#   <parent>/qnotebook/
#
# Usage:
#   QDISTRO_VM_PASSWORD=<pw> QDWIN_VM_TEMPLATE=<template-domain> \
#     scripts/vm/spin-test-vm.sh [<prefix>]
#
# Default prefix is "qd-test"; VM name (with timestamp suffix) is
# printed on the last stdout line.

set -euo pipefail

: "${QDISTRO_VM_PASSWORD:=admin}"
export QDISTRO_VM_PASSWORD

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
for sib in qdwin qdshell qnotebook; do
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
# `build-*` excludes match by basename anywhere in the tree; the
# old `--exclude='build-*'` ate `print-vm/build-print-image.sh`
# (spec/20 priority #5 source-of-truth probed by s64). Restrict the
# exclude to the known meson-host build dirs in sibling repos so
# regular scripts named `build-*` survive into the staged tarballs.
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='.git' --exclude='build' --exclude='build-host*' \
    -czf "$STAGE/qdistro.tar.gz" -C "$PARENT/qdistro" .
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='.git' --exclude='build' --exclude='build-host*' \
    --exclude='libweston-vendored/src/build' \
    -czf "$STAGE/qdwin.tar.gz" -C "$PARENT/qdwin" .
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='.git' --exclude='build' --exclude='build-host*' \
    -czf "$STAGE/qdshell.tar.gz" -C "$PARENT/qdshell" .
if [ -d "$PARENT/qdlocker" ]; then
    tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
        --exclude='.git' --exclude='build' --exclude='build-host*' \
        -czf "$STAGE/qdlocker.tar.gz" -C "$PARENT/qdlocker" .
fi
# qdbrowser ships the outer ``qdbrowser/qdbrowser/`` python package
# that install-browser-bridge-for-vm.sh stages to
# /usr/local/lib/qdistro/qdbrowser/ (so probes can
# ``from qdbrowser.pwd_autofill import ...``). Without staging the
# tarball here the bridge installer auto-search misses the package
# and the bake-time python313-jeepney is wasted.
if [ -d "$PARENT/qdbrowser" ]; then
    tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
        --exclude='.git' --exclude='build' --exclude='build-host*' \
        -czf "$STAGE/qdbrowser.tar.gz" -C "$PARENT/qdbrowser" .
fi
if [ -d "$PARENT/qdgreeter" ]; then
    tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
        --exclude='.git' --exclude='build' --exclude='build-host*' \
        -czf "$STAGE/qdgreeter.tar.gz" -C "$PARENT/qdgreeter" .
fi
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='.git' --exclude='build' --exclude='build-host*' \
    -czf "$STAGE/qnotebook.tar.gz" -C "$PARENT/qnotebook" .

# Also stage the bootstrap script next to the tarballs so the VM
# can fetch it before unpacking anything.
cp "$REPO/scripts/vm/fresh-vm-bootstrap.sh" "$STAGE/fresh-vm-bootstrap.sh"

# Use a per-run EPHEMERAL port + a log inside the per-user $STAGE dir
# rather than a fixed host-wide port (8765) and a fixed /tmp path. On a
# shared host, multiple users running this script collide on both: a
# /tmp/spin-http.log owned by another uid is unwritable (sticky /tmp),
# and port 8765 may already be held by another user's server we cannot
# kill — and the guest's 10.0.2.2:8765 would then hit THAT server.
# Picking a free port per run and serving the log from $STAGE makes
# concurrent multi-user runs independent.
SPIN_HTTP_PORT="${SPIN_HTTP_PORT:-$(python3 -c 'import socket; s=socket.socket(); s.bind(("0.0.0.0",0)); print(s.getsockname()[1]); s.close()')}"
log "stage 4b: starting host HTTP server on 0.0.0.0:$SPIN_HTTP_PORT..."
# Bind on 0.0.0.0 so the VM (which reaches us via 10.0.2.2 over SLIRP
# NAT) can connect. Restricting to 127.0.0.1 is unreachable from the
# guest.
(cd "$STAGE" && python3 -m http.server "$SPIN_HTTP_PORT" --bind 0.0.0.0 >"$STAGE/spin-http.log" 2>&1) &
HTTP_PID=$!
sleep 1

STAGE_URL="http://10.0.2.2:$SPIN_HTTP_PORT"
log "stage 5: running fresh-vm-bootstrap.sh in VM..."
"$SCRIPT_DIR/vm-exec" "$VM" "wget -q -O /root/fresh-vm-bootstrap.sh $STAGE_URL/fresh-vm-bootstrap.sh" >&2 \
    || { log "ERROR: failed to fetch bootstrap script (port $SPIN_HTTP_PORT reachable?)"; exit 3; }

# Bootstrap fetches the three tarballs and runs the build. Pass the
# per-run staging URL so the in-VM bootstrap fetches from THIS run's
# server (its default is the old fixed http://10.0.2.2:8765).
"$SCRIPT_DIR/vm-exec" "$VM" "QDISTRO_HTTP_HOST='$STAGE_URL' QDISTRO_VM_PASSWORD='$QDISTRO_VM_PASSWORD' bash /root/fresh-vm-bootstrap.sh" >&2

# Stage 6: verify the session came up.
# wayland-1 is the core "compositor came up" signal — fatal on miss.
# qdshell ctrl-socket + qdlocker.sock are warn-only (the qdlocker bug
# is still in flight in the qdlocker repo and qdshell may not have
# bound the ctrl-socket yet at this exact moment).
sleep 3
WAYLAND_OK=$("$SCRIPT_DIR/vm-exec" "$VM" "[ -S /run/user/1000/wayland-1 ] && echo yes || echo no" 2>/dev/null | tail -1)
if [ "${WAYLAND_OK:-no}" != "yes" ]; then
    log "FAIL: /run/user/1000/wayland-1 missing — compositor did not come up"
    exit 4
fi
log "PASS: /run/user/1000/wayland-1 present (compositor up)"

REPLY=$("$SCRIPT_DIR/vm-exec" "$VM" "echo list | socat - UNIX-CONNECT:/run/user/1000/qdshell.sock 2>&1 | head -1" 2>/dev/null | tail -1)
if [ "${REPLY:-}" = "ok list" ]; then
    log "PASS: qdshell ctrl-socket responsive"
else
    log "WARN: qdshell ctrl-socket not ready (reply was: ${REPLY:-empty})"
fi

LOCKER_OK=$("$SCRIPT_DIR/vm-exec" "$VM" "[ -S /run/user/1000/qdlocker.sock ] && echo yes || echo no" 2>/dev/null | tail -1)
if [ "${LOCKER_OK:-no}" = "yes" ]; then
    log "PASS: qdlocker.sock present"
else
    log "WARN: qdlocker.sock missing (known qdlocker env bug; fix in flight)"
fi

log "ready. VM:"
echo "$VM"
