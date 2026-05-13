#!/bin/bash
# §Phase-7 tier-5-Linux — build the per-app guest disk image.
#
# Produces /var/lib/libvirt/images/qdistro-tier5-base.qcow2 with:
#   - openSUSE Tumbleweed Minimal-VM Cloud base
#   - waypipe + wayland-utils preinstalled
#   - qemu-guest-agent service enabled (so the host can drive guest-
#     exec to launch waypipe-server inside the guest)
#   - cloud-init datasource configured for libvirt's per-VM seed iso
#     (per-VM hostname / vsock CID etc. baked in by spawn-tier5.sh)
#   - a small `qdistro-tier5-publisher.sh` helper that the host calls
#     via qga `guest-exec` to start a waypipe-server on a given vsock
#     port and exec the user app.
#
# Linux-only per spec/00 (memory qdistro_linux_only.md).
#
# Run this once on the host (or inside the test VM if you want to
# exercise --vm mode end-to-end inside bats). The bats coverage in
# phase7-tier5-vm skips gracefully when the base image is absent, so
# this isn't a hard prereq for the rest of the suite.
#
# Build cost: one-time download of ~400MB (Tumbleweed-Minimal-VM
# cloud qcow2), then ~30s of customization. Linked clones per VM
# come from spawn-tier5.sh at runtime.
set -euo pipefail

usage() {
    cat <<'EOF'
qdistro-tier5 guest image builder.

Usage:
  build-guest-image.sh [--force] [--mirror URL] [--dest PATH]

Options:
  --force        Rebuild even if dest already exists.
  --mirror URL   Override Tumbleweed-Minimal-VM download URL.
                 Default: https://download.opensuse.org/tumbleweed/appliances/
                          openSUSE-Tumbleweed-Minimal-VM.x86_64-Cloud.qcow2
  --dest PATH    Output path (default /var/lib/libvirt/images/qdistro-tier5-base.qcow2).

Reqs:
  virt-customize, qemu-img, wget. zypper install libguestfs guestfs-tools qemu-tools.
EOF
}

FORCE=0
DEST=/var/lib/libvirt/images/qdistro-tier5-base.qcow2
MIRROR=https://download.opensuse.org/tumbleweed/appliances/openSUSE-Tumbleweed-Minimal-VM.x86_64-Cloud.qcow2

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)   usage; exit 0;;
        --force)     FORCE=1; shift;;
        --mirror)    shift; MIRROR="${1:?--mirror requires URL}"; shift;;
        --dest)      shift; DEST="${1:?--dest requires PATH}"; shift;;
        *) echo "unknown arg: $1" >&2; usage >&2; exit 2;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "[tier5-build] must run as root (writes /var/lib/libvirt/images/)" >&2
    exit 2
fi
for tool in virt-customize qemu-img wget; do
    command -v "$tool" >/dev/null || { echo "[tier5-build] missing: $tool" >&2; exit 2; }
done

if [ -f "$DEST" ] && [ "$FORCE" != "1" ]; then
    echo "[tier5-build] $DEST already exists; use --force to rebuild" >&2
    exit 0
fi

WORK="$(mktemp -d /tmp/tier5-build.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

BASE_QCOW="$WORK/base.qcow2"
echo "[tier5-build] downloading base image..."
wget -q --show-progress -O "$BASE_QCOW" "$MIRROR" || {
    echo "[tier5-build] FAIL: download from $MIRROR" >&2
    exit 3
}

# Stage the publisher helper that the host triggers via qga.
PUBLISHER="$WORK/qdistro-tier5-publisher.sh"
cat >"$PUBLISHER" <<'PUBEOF'
#!/bin/bash
# qdistro-tier5-publisher — runs inside the tier-5 guest VM. The host
# invokes us via qemu-guest-agent guest-exec with:
#   qdistro-tier5-publisher.sh <vsock_port> <app> [args...]
# We exec waypipe-server in vsock-server mode on the given port,
# binding it to vsock CID=2 (= host) so that the host's waypipe-client
# (listening on the guest's CID) connects in.
#
# Per `waypipe(1)`: when --vsock is set with `-s [s]CID:port`, the
# `s` prefix marks server-mode listening (binds CID:port). For tier-5
# we want the guest-side waypipe to *connect out* to the host, so it
# uses `-s 2:port` (no `s`), and the host-side runs `--vsock --server`
# mode with `-s s<guest_cid>:port` listening. Wait — that depends on
# direction: waypipe-server waits for the inner app, waypipe-client
# accepts the wayland connection. We're matching the loopback shape:
# host-side waypipe-client listens (on the guest's CID); the guest-side
# waypipe-server connects out to vsock CID=2 (host) on the chosen
# port. Symmetry: host-side `client -s s<host_cid>:port` ↔ guest-side
# `server -s 2:port`.
set -uo pipefail
PORT="${1:?usage: $0 <port> <app> [args...]}"
shift

# qga delivers stdin/stdout via captured pipes. waypipe-server logs to
# stderr; redirect to /var/log so the host can fetch on demand.
LOG=/var/log/qdistro-tier5-publisher.log
exec >>"$LOG" 2>&1
echo "=== $(date -Iseconds): tier5-publisher port=$PORT app='$*' ==="

# Wait briefly for vsock module to be live (modprobe is async on first
# boot of cloud images). Idempotent.
modprobe vhost_vsock 2>/dev/null || true
modprobe vsock 2>/dev/null || true
for _ in 1 2 3 4 5; do
    [ -e /dev/vsock ] && break
    sleep 0.5
done

# guest-side waypipe-server: connects out to host CID=2 on $PORT.
exec waypipe --vsock -s "2:$PORT" server -- "$@"
PUBEOF
chmod 0755 "$PUBLISHER"

echo "[tier5-build] customizing..."
# Tumbleweed Minimal-VM Cloud image is mutable (not transactional), so
# zypper install works directly via virt-customize.
virt-customize -a "$BASE_QCOW" \
    --install waypipe,wayland-utils,qemu-guest-agent,kbd,alsa-utils \
    --run-command 'systemctl enable qemu-guest-agent.service' \
    --run-command 'systemctl enable serial-getty@ttyS0.service' \
    --copy-in "$PUBLISHER:/usr/local/bin/" \
    --run-command 'chmod +x /usr/local/bin/qdistro-tier5-publisher.sh' \
    --run-command 'echo "qdistro-tier5-base" >/etc/hostname' \
    --root-password "password:${QDISTRO_VM_PASSWORD:?}" \
    --run-command 'modprobe vsock; modprobe vhost_vsock || true' \
    >/dev/null

# Sparsify to keep the published base small.
echo "[tier5-build] sparsifying..."
virt-sparsify --in-place "$BASE_QCOW" 2>/dev/null || true

# Move into place.
install -d "$(dirname "$DEST")"
mv "$BASE_QCOW" "$DEST"
chmod 0644 "$DEST"
chown root:root "$DEST"

# Make sure libvirt's qemu user can read it (mode 0644 is fine; on
# qemu:///session as the admin user there's no qemu user — the admin
# uid reads directly).
echo "[tier5-build] done: $DEST"
ls -lh "$DEST"
