#!/bin/bash
# build-baseweed-from-scratch.sh — produce baseweed-admin.qcow2 from
# the upstream openSUSE Tumbleweed Minimal-VM Cloud image, fully
# automated, no hand-installed artifacts.
#
# Output: $IMG/baseweed-admin.qcow2 — a 25 GB qcow2 with:
#   - admin user uid 1000, password ${QDISTRO_VM_PASSWORD}, sudo NOPASSWD
#   - qemu-guest-agent enabled (so vm-exec works first boot)
#   - SELinux permissive (per memory selinux_permissive_required.md)
#   - cloud-init AND jeos-firstboot masked (we own first-boot config;
#     without the jeos-firstboot mask its wizard runs on tty1 and
#     blocks `systemctl reload user@*.service` triggered by every
#     package post-transaction — symptom is fresh-vm-bootstrap.sh
#     hanging indefinitely on its first `zypper install`)
#   - root password set so console rescue is possible
#
# Used as the BASE input to build-baked-baseweed.sh, replacing the
# hand-built baseweed.qcow2 artifact.
#
# Usage:
#   QDISTRO_VM_PASSWORD=xxx ./build-baseweed-from-scratch.sh [--force]
#
# Time budget: ~5–10 min (one-time download + resize + customize).

set -euo pipefail

FORCE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "ERROR: unknown arg '$1'" >&2; exit 2 ;;
    esac
done

: "${QDISTRO_VM_PASSWORD:?must export QDISTRO_VM_PASSWORD}"

IMG="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
CACHE_DIR="$HOME/.cache/qdistro"
CLOUD_URL="https://download.opensuse.org/tumbleweed/appliances/openSUSE-Tumbleweed-Minimal-VM.x86_64-Cloud.qcow2"
CLOUD_CACHE="$CACHE_DIR/tumbleweed-cloud-base.qcow2"
DEST="$IMG/baseweed-admin.qcow2"
PARTIAL="$DEST.partial"

if [ -f "$DEST" ] && [ "$FORCE" -ne 1 ]; then
    echo "$DEST already exists. Use --force to rebuild." >&2
    qemu-img info "$DEST" | sed 's/^/  /'
    exit 0
fi

for tool in virt-customize virt-resize qemu-img wget virt-sparsify; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "ERROR: $tool not on PATH (need libguestfs + guestfs-tools)" >&2
        exit 3
    }
done

install -d "$CACHE_DIR" "$IMG"

if [ ! -s "$CLOUD_CACHE" ]; then
    echo "[scratch] downloading Tumbleweed Minimal-VM cloud image..."
    wget -q --show-progress -O "$CLOUD_CACHE.partial" "$CLOUD_URL"
    mv "$CLOUD_CACHE.partial" "$CLOUD_CACHE"
else
    echo "[scratch] cloud base already cached ($(du -h "$CLOUD_CACHE" | cut -f1))"
fi

rm -f "$PARTIAL"
echo "[scratch] resizing cloud image to 25 GB..."
qemu-img create -f qcow2 "$PARTIAL" 25G >/dev/null
virt-resize --quiet --expand /dev/sda3 "$CLOUD_CACHE" "$PARTIAL" >/dev/null || {
    # Tumbleweed cloud layout may use /dev/sda1 (single root) on some
    # builds. Fall back to auto-expand.
    rm -f "$PARTIAL"
    qemu-img create -f qcow2 "$PARTIAL" 25G >/dev/null
    virt-resize --quiet "$CLOUD_CACHE" "$PARTIAL"
}

echo "[scratch] customizing: admin user + qga + SELinux + cloud-init mask..."
virt-customize \
    --memsize 2048 \
    --smp 2 \
    -a "$PARTIAL" \
    --root-password "password:${QDISTRO_VM_PASSWORD}" \
    --run-command 'getent passwd admin >/dev/null || useradd -m -u 1000 -U -s /bin/bash admin' \
    --run-command 'getent group wheel >/dev/null && usermod -aG wheel admin || true' \
    --run-command "echo 'admin:${QDISTRO_VM_PASSWORD}' | chpasswd" \
    --run-command "echo 'admin ALL=(ALL) NOPASSWD: ALL' >/etc/sudoers.d/99-admin && chmod 0440 /etc/sudoers.d/99-admin" \
    --run-command 'zypper -n --no-gpg-checks refresh' \
    --run-command 'zypper -n install --no-recommends qemu-guest-agent' \
    --run-command 'systemctl enable qemu-guest-agent.service' \
    --run-command 'systemctl enable serial-getty@ttyS0.service' \
    --run-command 'sed -i "s/^SELINUX=.*/SELINUX=permissive/" /etc/selinux/config 2>/dev/null || true' \
    --run-command 'systemctl mask cloud-init.service cloud-init-local.service cloud-config.service cloud-final.service cloud-init.target 2>/dev/null || true' \
    --run-command 'systemctl mask jeos-firstboot.service 2>/dev/null || true' \
    --run-command 'echo baseweed-admin >/etc/hostname' \
    --run-command 'zypper clean -a' \
    --run-command 'rm -rf /var/cache/zypp/* /tmp/* /var/tmp/* 2>/dev/null; true'

virt-sparsify --in-place "$PARTIAL" 2>/dev/null || true
mv "$PARTIAL" "$DEST"

echo "[scratch] done: $DEST"
qemu-img info "$DEST" | sed 's/^/  /'
