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
# Base-image hardening (guest-image-perms track)
# ----------------------------------------------
# - The published base qcow2 is written 0640 root:root (NOT world-readable),
#   mirroring the Tier-4 per-VM overlay precedent. On qemu:///session the
#   admin uid reads it directly; on qemu:///system add the qemu user to the
#   image's group if it must read without root.
# - Baking a debug root password is OPT-IN via the environment flag
#       QDISTRO_GUEST_BAKE_DEBUG_PASSWORD  (default 1)
#   DEFAULT = 1 (bake the password) to preserve the existing test-VM
#   workflows that log in over the serial console with QDISTRO_VM_PASSWORD.
#   Set QDISTRO_GUEST_BAKE_DEBUG_PASSWORD=0 for a HARDENED / production
#   build: no root password is baked and QDISTRO_VM_PASSWORD is not required.
#   When the flag is 1 the virt-customize argument set is byte-identical to
#   the historical behavior, so any deterministic output is unchanged unless
#   the operator explicitly opts into the hardened path.
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

# Opt-in debug-password gate (default on; see header).
QDISTRO_GUEST_BAKE_DEBUG_PASSWORD="${QDISTRO_GUEST_BAKE_DEBUG_PASSWORD:-1}"

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

Env:
  QDISTRO_VM_PASSWORD  Root password baked into the image. Mandatory unless
                       QDISTRO_GUEST_BAKE_DEBUG_PASSWORD=0.
  QDISTRO_GUEST_BAKE_DEBUG_PASSWORD
                       1 (default) bakes the debug root password from
                       QDISTRO_VM_PASSWORD; 0 = hardened build, no password
                       baked. Either way the output image is 0640 root:root.
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
for tool in virt-customize virt-resize qemu-img wget; do
    command -v "$tool" >/dev/null || { echo "[tier5-build] missing: $tool" >&2; exit 2; }
done

if [ -f "$DEST" ] && [ "$FORCE" != "1" ]; then
    echo "[tier5-build] $DEST already exists; use --force to rebuild" >&2
    exit 0
fi

WORK="$(mktemp -d /tmp/tier5-build.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

BASE_QCOW="$WORK/base.qcow2"
RESIZED_QCOW="$WORK/base-resized.qcow2"
echo "[tier5-build] downloading base image..."
wget -q --show-progress -O "$BASE_QCOW" "$MIRROR" || {
    echo "[tier5-build] FAIL: download from $MIRROR" >&2
    exit 3
}

echo "[tier5-build] expanding base image..."
qemu-img create -f qcow2 "$RESIZED_QCOW" "${TIER5_BASE_SIZE:-5G}" >/dev/null
virt-resize --expand /dev/sda3 "$BASE_QCOW" "$RESIZED_QCOW" >/dev/null
BASE_QCOW="$RESIZED_QCOW"
virt-customize -a "$BASE_QCOW" --run-command 'xfs_growfs /' >/dev/null

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

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/qdistro-tier5-runtime}"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 0700 "$XDG_RUNTIME_DIR"
setenforce 0 2>/dev/null || true

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

# Build the (optional) root-password argument. Default bakes the debug
# password; the hardened path (flag=0) bakes none.
PW_ARGS=()
if [ "$QDISTRO_GUEST_BAKE_DEBUG_PASSWORD" = "1" ]; then
    PW_ARGS=(--root-password "password:${QDISTRO_VM_PASSWORD:?QDISTRO_VM_PASSWORD must be set (or set QDISTRO_GUEST_BAKE_DEBUG_PASSWORD=0)}")
    echo "[tier5-build] baking debug root password (QDISTRO_GUEST_BAKE_DEBUG_PASSWORD=1)"
else
    echo "[tier5-build] HARDENED build: no root password baked (QDISTRO_GUEST_BAKE_DEBUG_PASSWORD=0)"
fi

echo "[tier5-build] customizing..."
# Tumbleweed Minimal-VM Cloud image is mutable (not transactional), so
# zypper install works directly via virt-customize.
virt-customize -a "$BASE_QCOW" \
    --install waypipe,wayland-utils,qemu-guest-agent,kbd,alsa-utils,weston,MozillaFirefox,baobab,gnome-text-editor,nautilus,gnome-calculator,libqt5-qtwayland,qt6-wayland,kcalc,dolphin,konsole,kate,fontconfig,dejavu-fonts,liberation-fonts,cantarell-fonts,google-noto-sans-fonts,google-noto-serif-fonts,google-noto-sans-mono-fonts,google-noto-coloremoji-fonts,google-noto-sans-cjk-fonts \
    --run-command 'systemctl enable qemu-guest-agent.service' \
    --run-command 'systemctl enable serial-getty@ttyS0.service' \
    --run-command 'fc-cache -f || true' \
    --run-command 'if [ -f /etc/selinux/config ]; then sed -i "s/^SELINUX=.*/SELINUX=permissive/" /etc/selinux/config; fi' \
    --run-command 'rm -f /.autorelabel' \
    --copy-in "$PUBLISHER:/usr/local/bin/" \
    --run-command 'chmod +x /usr/local/bin/qdistro-tier5-publisher.sh' \
    --run-command 'echo "qdistro-tier5-base" >/etc/hostname' \
    "${PW_ARGS[@]}" \
    --run-command 'modprobe vsock; modprobe vhost_vsock || true' \
    >/dev/null

# Sparsify to keep the published base small.
echo "[tier5-build] sparsifying..."
virt-sparsify --in-place "$BASE_QCOW" 2>/dev/null || true

# Move into place.
install -d "$(dirname "$DEST")"
mv "$BASE_QCOW" "$DEST"
# Base image is sensitive (may contain a baked debug password); keep it
# non-world-readable (0640 root:root), mirroring the Tier-4 overlay.
chmod 0640 "$DEST"
chown root:root "$DEST"

# On qemu:///session as the admin user there's no qemu user — the admin
# uid reads directly. On qemu:///system, add the qemu user to the image's
# group if it must read without root (0640 is not world-readable).
echo "[tier5-build] done: $DEST"
ls -lh "$DEST"
