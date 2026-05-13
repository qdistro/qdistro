#!/bin/bash
# Host-side: package qdistro/ + qterminator/ + qnotebook/ from the
# repo, push them into $VM via the http-server SLIRP pattern, then
# run bootstrap-vm.sh inside.
#
# Usage: deploy-to-vm.sh <vm-name>
#
# Requires: vm-exec on $PATH (or QDISTRO_VM_EXEC points at it).

set -euo pipefail

VM="${1:-${VMNAME:-}}"
if [ -z "$VM" ]; then
    echo "usage: $0 <vm-name>" >&2
    exit 2
fi

REPO="${QDISTRO_REPO:-${QDISTRO_REPO}}"
VM_EXEC="${QDISTRO_VM_EXEC:-$REPO/scripts/vm/vm-exec}"
PORT=8765

if [ ! -x "$VM_EXEC" ]; then
    echo "[deploy] vm-exec not found at $VM_EXEC" >&2
    exit 1
fi

log() { echo "[deploy] $*" >&2; }

# --- 1. Build tarballs ---------------------------------------------------
log "tarball phase1 → /tmp/qdistro-phase1.tar.gz"
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    -czf /tmp/qdistro-phase1.tar.gz -C "$REPO/phase1" .

log "tarball qterminator → /tmp/qdistro-qterminator.tar.gz"
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
    --exclude='build' --exclude='.pytest_cache' \
    -czf /tmp/qdistro-qterminator.tar.gz -C "$REPO/qterminator" .

log "tarball qnotebook → /tmp/qdistro-qnotebook.tar.gz"
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
    --exclude='.pytest_cache' --exclude='.zim-qt' \
    -czf /tmp/qdistro-qnotebook.tar.gz -C "$REPO/qnotebook" .

# --- 2. Start host http server -------------------------------------------
# Kill any stale server we started previously. Don't clobber someone
# else's server on the same port — if the port is held by a different
# command, bail.
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$PORT\$"; then
    # Our previous run?
    if pgrep -f "python3 -m http.server $PORT" >/dev/null; then
        pkill -f "python3 -m http.server $PORT" || true
        sleep 1
    else
        log "port $PORT held by another process; aborting"
        exit 1
    fi
fi

log "http.server :$PORT serving /tmp"
(cd /tmp && python3 -m http.server "$PORT" --bind 127.0.0.1 >/tmp/deploy-http.log 2>&1 &)
# Give it a moment to bind.
sleep 1
trap 'pkill -f "python3 -m http.server '"$PORT"'" 2>/dev/null || true' EXIT

# vm-exec's JSON encoder chokes on newlines and embedded ". Every
# multi-line script we feed through vm-exec gets base64-wrapped so the
# payload is a single quote-free `echo <b64> | base64 -d | bash` line.
run_in_vm() {
    local b64
    b64=$(base64 -w0)
    "$VM_EXEC" "$VM" "echo $b64 | base64 -d | bash"
}

# --- 3. Ensure VM has tar + wget -----------------------------------------
log "ensure tar + wget in VM"
run_in_vm <<'EOF'
set -e
command -v tar >/dev/null && command -v wget >/dev/null \
    || zypper -n install tar wget >/dev/null 2>&1
EOF

# --- 4. Pull + unpack inside the VM -------------------------------------
log "pull tarballs into VM"
run_in_vm <<EOF
set -e
rm -rf /root/qdistro-deploy
mkdir -p /root/qdistro-deploy
wget -q http://10.0.2.2:${PORT}/qdistro-phase1.tar.gz -O /tmp/phase1.tar.gz
tar xzf /tmp/phase1.tar.gz -C /root/qdistro-deploy
rm -f /tmp/phase1.tar.gz
EOF

log "pull qterminator source"
run_in_vm <<EOF
set -e
rm -rf /usr/local/lib/qdistro/qterminator-src
mkdir -p /usr/local/lib/qdistro/qterminator-src
wget -q http://10.0.2.2:${PORT}/qdistro-qterminator.tar.gz -O /tmp/qt.tar.gz
tar xzf /tmp/qt.tar.gz -C /usr/local/lib/qdistro/qterminator-src
rm -f /tmp/qt.tar.gz
EOF

log "pull qnotebook source"
run_in_vm <<EOF
set -e
rm -rf /usr/local/lib/qdistro/qnotebook-src
mkdir -p /usr/local/lib/qdistro/qnotebook-src
wget -q http://10.0.2.2:${PORT}/qdistro-qnotebook.tar.gz -O /tmp/qn.tar.gz
tar xzf /tmp/qn.tar.gz -C /usr/local/lib/qdistro/qnotebook-src
rm -f /tmp/qn.tar.gz
EOF

# --- 5. Run bootstrap ----------------------------------------------------
log "running bootstrap-vm.sh (this builds QTermWidget bindings — may be slow)"
run_in_vm <<'EOF' || { echo "[deploy] bootstrap failed; see VM logs" >&2; exit 1; }
bash /root/qdistro-deploy/deploy/bootstrap-vm.sh 2>&1 | tail -200
EOF

log "deploy complete for $VM"
