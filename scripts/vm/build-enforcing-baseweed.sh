#!/bin/bash
# build-enforcing-baseweed.sh — produce a "baked" baseweed-derived
# image whose /etc/selinux/config is pinned to SELINUX=enforcing AND
# which carries a host-injected SSH authorized_keys for root, so the
# bats suite can drive enforcing-mode tests via SSH (qga is denied
# under enforcing because virt_qemu_ga_t is too restricted).
#
# Output: $IMG/baseweed-enforcing-baked.qcow2 — qcow2 overlay backed
# by baseweed-baked.qcow2 (which itself is backed by baseweed.qcow2).
# A 3-level chain: enforcing-baked → baked → baseweed.
#
# Strategy:
#   1. Clone baseweed-baked → temp build VM (still permissive at boot).
#   2. Run fresh-vm-bootstrap.sh inside (qdwin/qdshell/broker, etc.).
#   3. Generate the host SSH keypair (~/.ssh/qdistro_enforcing_id_ed25519)
#      if absent; ssh-inject the .pub into root@ via virt-customize
#      offline AFTER the VM is shut down (qga can't write authorized_keys
#      atomically pre-shutdown without race on sshd reload).
#   4. Ensure sshd is enabled — done in-VM via qga before shutdown.
#   5. Shut down cleanly.
#   6. virt-edit /etc/selinux/config to SELINUX=enforcing AND
#      virt-customize ssh-inject root.
#   7. mv to baseweed-enforcing-baked.qcow2; chmod 0644.
#
# Per-clone usage (downstream): clone-baseweed.sh --from-enforcing-baked
# stages the qcow2 chain + adds a unique <portForward> so a host SSH
# client can reach guest:22 on 127.0.0.1:<port>.
#
# Time budget: roughly equal to one normal fresh-vm-bootstrap plus
# ~30s of virt-customize. Reuses baseweed-baked, so no zypper install.
#
set -eo pipefail

FORCE=0
KEEP_VM=0
IMG_DIR_OVERRIDE=""
HTTP_PORT=8765

while [ $# -gt 0 ]; do
    case "$1" in
        --force)      FORCE=1; shift ;;
        --keep-vm)    KEEP_VM=1; shift ;;
        --image-dir)  IMG_DIR_OVERRIDE="$2"; shift 2 ;;
        --http-port)  HTTP_PORT="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,30p' "$0"; exit 0 ;;
        *)
            echo "ERROR: unknown arg '$1'" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSITOR_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$COMPOSITOR_DIR/.." && pwd)"
VM_TOOLS="$REPO_ROOT/scripts/vm"
IMG="${IMG_DIR_OVERRIDE:-${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}}"

BASE="$IMG/baseweed-baked.qcow2"
ENFORCING="$IMG/baseweed-enforcing-baked.qcow2"
KEYFILE="$HOME/.ssh/qdistro_enforcing_id_ed25519"

if [ ! -f "$BASE" ]; then
    echo "ERROR: baseweed-baked image not found at $BASE" >&2
    echo "       run build-baked-baseweed.sh first" >&2
    exit 1
fi
if [ -f "$ENFORCING" ] && [ "$FORCE" -ne 1 ]; then
    echo "$ENFORCING already exists. Use --force to rebuild." >&2
    qemu-img info "$ENFORCING" | sed 's/^/    /'
    exit 0
fi

# Generate host SSH keypair if missing. Stored in ~/.ssh/, mode 0600.
if [ ! -f "$KEYFILE" ]; then
    echo "[bake-enforcing] generating host SSH keypair → $KEYFILE"
    install -d -m 0700 "$(dirname "$KEYFILE")"
    ssh-keygen -t ed25519 -f "$KEYFILE" -N "" \
        -C "qdistro-enforcing-vm@$(hostname -s)" >/dev/null
fi
PUBKEY="$(cat "${KEYFILE}.pub")"

# 1. Clone baseweed-baked → temp VM (permissive). The clone-baseweed.sh
#    --from-baked path lays down a 2-level chain (clone → baked → base);
#    we'll later promote the resulting overlay to baseweed-enforcing-baked
#    while the chain remains intact.
PREFIX="bake-enforcing"
echo "[bake-enforcing] cloning baseweed-baked → temporary build VM ($PREFIX-...)"
VM=$("$SCRIPT_DIR/clone-baseweed.sh" "$PREFIX" --from-baked)
echo "[bake-enforcing] build VM: $VM"

cleanup() {
    rc=$?
    if [ -n "${HTTP_PID:-}" ] && kill -0 "$HTTP_PID" 2>/dev/null; then
        kill "$HTTP_PID" 2>/dev/null || true
    fi
    if [ -n "${STAGE:-}" ] && [ -d "$STAGE" ]; then
        rm -rf "$STAGE"
    fi
    if [ "$KEEP_VM" -eq 1 ]; then
        echo "[bake-enforcing] --keep-vm set; leaving '$VM' defined and disk at $IMG/${VM}.qcow2"
        return $rc
    fi
    if [ "$rc" -ne 0 ]; then
        echo "[bake-enforcing] FAILED (rc=$rc); leaving build VM '$VM' for inspection." >&2
        return $rc
    fi
}
trap cleanup EXIT

# 2. Stage tarballs + the bootstrap script in a temp dir, then serve
#    that dir over http on 0.0.0.0 (the VM reaches us via 10.0.2.2 over
#    SLIRP/passt NAT — 127.0.0.1 is unreachable from the guest). This
#    mirrors spin-test-vm.sh's staging pattern; fresh-vm-bootstrap.sh
#    expects /qdistro.tar.gz, /qdwin.tar.gz, /qdshell.tar.gz at the
#    HTTP root, optionally /qdlocker.tar.gz.
PARENT="$(cd "$REPO_ROOT/.." && pwd)"
STAGE="$(mktemp -d -t bake-enforcing-stage.XXXXXX)"
echo "[bake-enforcing] tarballing qdistro, qdwin, qdshell, qdlocker into $STAGE..."
TAR_EXCLUDES=(--exclude='__pycache__' --exclude='*.pyc'
              --exclude='.pytest_cache' --exclude='.git'
              --exclude='build' --exclude='build-*')
tar "${TAR_EXCLUDES[@]}" -czf "$STAGE/qdistro.tar.gz" -C "$PARENT/qdistro" .
tar "${TAR_EXCLUDES[@]}" --exclude='libweston-vendored/src/build' \
    -czf "$STAGE/qdwin.tar.gz" -C "$PARENT/qdwin" .
tar "${TAR_EXCLUDES[@]}" -czf "$STAGE/qdshell.tar.gz" -C "$PARENT/qdshell" .
if [ -d "$PARENT/qdlocker" ]; then
    tar "${TAR_EXCLUDES[@]}" -czf "$STAGE/qdlocker.tar.gz" -C "$PARENT/qdlocker" .
fi
cp "$VM_TOOLS/fresh-vm-bootstrap.sh" "$STAGE/fresh-vm-bootstrap.sh"

# Detect + reclaim port: a stale http.server from a prior run silently
# steals it and the in-VM wget then 404s against the wrong tree,
# surfacing as a confusing rc=8 four steps later.
if ss -tln 2>/dev/null | awk -v p=":$HTTP_PORT" '$4 ~ p {found=1} END {exit !found}'; then
    echo "[bake-enforcing] port $HTTP_PORT already bound; reclaiming..."
    PIDS=$(ss -tlnp 2>/dev/null \
        | awk -v p=":$HTTP_PORT" '$4 ~ p { for(i=1;i<=NF;i++) if (match($i, /pid=([0-9]+)/, m)) print m[1] }' | sort -u)
    [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
    sleep 0.5
fi
(cd "$STAGE" && nohup python3 -m http.server "$HTTP_PORT" \
        --bind 0.0.0.0 ) >/tmp/bake-enforcing-http.log 2>&1 &
HTTP_PID=$!
sleep 1
if ! ss -tln 2>/dev/null | awk -v p=":$HTTP_PORT" '$4 ~ p {found=1} END {exit !found}'; then
    echo "ERROR: http.server failed to bind $HTTP_PORT (see /tmp/bake-enforcing-http.log)" >&2
    tail -5 /tmp/bake-enforcing-http.log >&2 || true
    exit 6
fi

# 3. Push fresh-vm-bootstrap.sh + run it. Long-running; poll for DONE.
echo "[bake-enforcing] running fresh-vm-bootstrap.sh inside $VM (this is the slow step)..."
"$VM_TOOLS/vm-exec" "$VM" \
    "wget -q -O /root/fresh-vm-bootstrap.sh http://10.0.2.2:$HTTP_PORT/fresh-vm-bootstrap.sh && chmod +x /root/fresh-vm-bootstrap.sh"

"$VM_TOOLS/vm-exec" "$VM" \
    "nohup bash /root/fresh-vm-bootstrap.sh >/root/bootstrap.log 2>&1 &" \
    || true

MAX_WAIT=2400
WAITED=0
while [ "$WAITED" -lt "$MAX_WAIT" ]; do
    # fresh-vm-bootstrap.sh emits `[bootstrap] bootstrap complete.` on
    # success. Earlier pattern (`bootstrap.* DONE`) never matched —
    # both this check and the bootstrap's success path drifted apart.
    READY=$("$VM_TOOLS/vm-exec" "$VM" \
            "grep -q 'bootstrap complete' /root/bootstrap.log 2>/dev/null && echo READY" \
            2>/dev/null || true)
    if printf '%s' "$READY" | grep -q READY; then
        break
    fi
    sleep 20
    WAITED=$((WAITED + 20))
    if [ $((WAITED % 120)) -eq 0 ]; then
        echo "[bake-enforcing]   ... ${WAITED}s elapsed; bootstrap.log tail:"
        "$VM_TOOLS/vm-exec" "$VM" \
            "tail -3 /root/bootstrap.log 2>/dev/null || true" \
            2>/dev/null | sed 's/^/    /' || \
            echo "    (qga unresponsive — bootstrap still running, will retry)"
    fi
done

if [ "$WAITED" -ge "$MAX_WAIT" ]; then
    echo "[bake-enforcing] FAIL: bootstrap did not finish within ${MAX_WAIT}s" >&2
    "$VM_TOOLS/vm-exec" "$VM" "tail -20 /root/bootstrap.log" >&2 || true
    exit 4
fi
echo "[bake-enforcing] bootstrap DONE"

# 4. Ensure sshd is enabled+running and clear root password lock.
#    Also drop a small marker so we can sanity-check post-clone.
echo "[bake-enforcing] enabling sshd + dropping enforcing-baked marker..."
"$VM_TOOLS/vm-exec" "$VM" \
    "systemctl enable sshd >/dev/null 2>&1; \
     systemctl restart sshd 2>/dev/null || true; \
     mkdir -p /root/.ssh && chmod 0700 /root/.ssh; \
     echo 'baseweed-enforcing-baked' > /root/.qdistro-enforcing-marker; \
     date -u +'baked %Y-%m-%dT%H:%M:%SZ' >> /root/.qdistro-enforcing-marker; \
     fstrim -av >/dev/null 2>&1 || true; \
     sync"

# 5. Clean shutdown so qcow2 metadata is consistent.
echo "[bake-enforcing] powering off $VM cleanly..."
virsh -c qemu:///session shutdown "$VM" >/dev/null
for i in $(seq 1 60); do
    if [ "$(virsh -c qemu:///session domstate "$VM" 2>/dev/null)" = "shut off" ]; then
        break
    fi
    sleep 2
done
if [ "$(virsh -c qemu:///session domstate "$VM" 2>/dev/null)" != "shut off" ]; then
    echo "[bake-enforcing] WARN: shutdown timed out — forcing destroy" >&2
    virsh -c qemu:///session destroy "$VM" >/dev/null 2>&1 || true
fi

# Stop the host HTTP server now that the VM is offline.
if [ -n "${HTTP_PID:-}" ] && kill -0 "$HTTP_PID" 2>/dev/null; then
    kill "$HTTP_PID" 2>/dev/null || true
fi

SRC_QCOW="$IMG/${VM}.qcow2"
if [ ! -f "$SRC_QCOW" ]; then
    echo "[bake-enforcing] FAIL: expected disk $SRC_QCOW after build" >&2
    exit 5
fi

# 6. Offline edits: SELinux config to enforcing + ssh-inject root key.
#    virt-customize sequencing: ssh-inject FIRST (before edit), since
#    --edit on /etc/selinux/config is permissive-aware. Both run on the
#    cleanly shut-down qcow2.
echo "[bake-enforcing] virt-customize: SELinux config → enforcing + root SSH key..."
TMP_PUB="$(mktemp /tmp/qd-enf-pub-XXXXXX.pub)"
printf '%s\n' "$PUBKEY" >"$TMP_PUB"
trap 'rm -f "$TMP_PUB"' RETURN
virt-customize -a "$SRC_QCOW" \
    --ssh-inject "root:file:$TMP_PUB" \
    --edit '/etc/selinux/config:s/SELINUX=permissive/SELINUX=enforcing/' \
    --edit '/etc/selinux/config:s/SELINUX=disabled/SELINUX=enforcing/' \
    >/dev/null
rm -f "$TMP_PUB"

# 7. Undefine the build VM but keep its disk.
if [ "$KEEP_VM" -ne 1 ]; then
    virsh -c qemu:///session undefine "$VM" --nvram >/dev/null 2>&1 \
        || virsh -c qemu:///session undefine "$VM" >/dev/null 2>&1 \
        || true
fi

# 8. Promote the disk to baseweed-enforcing-baked.qcow2.
if [ -f "$ENFORCING" ] && [ "$FORCE" -eq 1 ]; then
    rm -f "$ENFORCING"
fi
mv "$SRC_QCOW" "$ENFORCING"
chmod 0644 "$ENFORCING"

echo "[bake-enforcing] OK: $ENFORCING ready"
qemu-img info "$ENFORCING" | sed 's/^/    /'
echo
echo "[bake-enforcing] use it via:  $SCRIPT_DIR/clone-baseweed.sh <prefix> --from-enforcing-baked"
