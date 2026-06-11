#!/bin/bash
# build-baked-baseweed.sh — produce a "baked" baseweed overlay disk
# pre-populated with all qdwin/qdshell zypper deps so future
# fresh-clone test runs skip the multi-minute install-deps step.
#
# Strategy (rewritten 2026-05-01 — task(108)):
#   1. Create a qcow2 overlay backed by baseweed-admin.qcow2 (built
#      from scratch by build-baseweed-from-scratch.sh from the
#      upstream Tumbleweed Minimal-VM Cloud image).
#   2. Run virt-customize against the overlay to install all deps
#      AT REST (no boot, no qga). libguestfs runs zypper inside its
#      own appliance VM, mounting the overlay's filesystem; the
#      install lives entirely on the writable overlay layer.
#   3. Bake in the tier-5 per-app guest base disk (host-side
#      virt-customize → upload into the overlay) so phase7-tier5-*
#      tests pass without per-clone downloads.
#   4. Sparsify + rename to baseweed-baked.qcow2.
#
# Why the rewrite: the previous design booted a clone VM and drove
# install-deps.sh through qemu-guest-agent (qga). The package list
# includes qga itself; mid-install zypper upgrades qga, the OLD qga's
# preun scriptlet stops the qga service, systemd reaps the entire
# qga process tree (including the in-flight zypper), the bake hangs
# at MAX_WAIT. This is architectural — no amount of timeout-bumping
# fixes it. virt-customize sidesteps the whole loop by running
# zypper inside libguestfs's appliance, which has no qga involvement.
#
# Failure mode of the previous approach is documented in:
#
# Usage:
#   ./build-baked-baseweed.sh                # builds, errors if baked exists
#   ./build-baked-baseweed.sh --force        # rebuild even if baked exists
#   ./build-baked-baseweed.sh --image-dir <dir>  # override $IMG
#
# Time budget: ~10–25 min on a warm zypper cache, longer on a cold
# one. Mostly the zypper download + install. virt-customize itself
# adds ~30–60 s of overhead (appliance boot + mount).
#
set -euo pipefail

FORCE=0
IMG_DIR_OVERRIDE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --force)      FORCE=1; shift ;;
        --image-dir)  IMG_DIR_OVERRIDE="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,40p' "$0"; exit 0 ;;
        *)
            echo "ERROR: unknown arg '$1'" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSITOR_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
IMG="${IMG_DIR_OVERRIDE:-${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}}"
TIER5_BUILD_GUEST="$SCRIPT_DIR/../../tier5-vm/build-guest-image.sh"

BASE="$IMG/baseweed-admin.qcow2"
BAKED="$IMG/baseweed-baked.qcow2"
PARTIAL="$IMG/baseweed-baked.qcow2.partial"

if [ ! -f "$BASE" ]; then
    echo "ERROR: $BASE not found." >&2
    echo "       Build it first with: QDISTRO_VM_PASSWORD=... \\" >&2
    echo "         $SCRIPT_DIR/build-baseweed-from-scratch.sh" >&2
    exit 1
fi
if [ -f "$BAKED" ] && [ "$FORCE" -ne 1 ]; then
    echo "$BAKED already exists. Use --force to rebuild." >&2
    echo "qemu-img info:"
    qemu-img info "$BAKED" | sed 's/^/    /'
    exit 0
fi

if ! command -v virt-customize >/dev/null 2>&1; then
    echo "ERROR: virt-customize not found on host (install libguestfs + guestfs-tools)" >&2
    exit 3
fi

# Pull the package list from install-deps.sh — single source of truth.
# install-deps.sh detects the source-vs-execute mode and just exposes
# QDISTRO_PKGS without invoking zypper.
# shellcheck disable=SC1091
. "$SCRIPT_DIR/install-deps.sh"
if [ "${#QDISTRO_PKGS[@]}" -eq 0 ]; then
    echo "ERROR: install-deps.sh produced an empty package list" >&2
    exit 4
fi
PKG_CSV="$(printf '%s,' "${QDISTRO_PKGS[@]}")"
PKG_CSV="${PKG_CSV%,}"
echo "[bake] ${#QDISTRO_PKGS[@]} packages to install"

# 1. Fresh overlay backed by baseweed. We work on .partial and only
#    promote to baseweed-baked.qcow2 at the very end so a partial run
#    doesn't poison future fresh clones.
rm -f "$PARTIAL"
qemu-img create -f qcow2 -F qcow2 -b "$BASE" "$PARTIAL" >/dev/null

cleanup_partial() {
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "[bake] FAILED (rc=$rc); leaving $PARTIAL for inspection." >&2
    fi
    return $rc
}
trap cleanup_partial EXIT

# 2. virt-customize: refresh, install, clean. Single call so libguestfs
#    boots its appliance once. zypper runs against the GUEST root via
#    libguestfs; no qga, no in-VM init system. The appliance gets
#    network through libguestfs's slirp backend so zypper can reach
#    download.opensuse.org.
echo "[bake] virt-customize: refresh + install ${#QDISTRO_PKGS[@]} packages..."
echo "[bake]   (this is the slow step — typically 10–25 min)"
# Notes:
# - Use --memsize 4096 to give zypper enough RAM (default 768M is
#   tight on a Tumbleweed install set this large).
# - --smp 4 speeds up rpm scriptlet execution.
# - libguestfs auto-handles SELinux relabel internally; the
#   --selinux-relabel flag exists but is documented as compat-only.
# - We pass our own zypper invocation rather than --install <list>
#   because we want explicit --no-recommends parity with the
#   install-deps.sh runtime path.
# - The GRUB console/text-payload reset mirrors
#   build-baseweed-from-scratch.sh: the shim-free virtio-gpu-pci in the
#   template domain has no VGA BIOS, so GRUB's default gfxterm hangs
#   mode-setting it and the kernel never loads (qci vm-smoke timed out
#   in vm_provision). Re-applied here so a baked rebuild from an older
#   admin image without the fix still boots.
virt-customize \
    --memsize 4096 \
    --smp 4 \
    -a "$PARTIAL" \
    --run-command 'zypper -n --no-gpg-checks refresh' \
    --run-command 'rpm -q kernel-default-base >/dev/null 2>&1 && zypper -n remove kernel-default-base || true' \
    --run-command "zypper -n install --no-recommends ${PKG_CSV//,/ }" \
    --run-command 'systemctl mask jeos-firstboot.service jeos-firstboot-snapshot.service 2>/dev/null || true' \
    --run-command 'systemctl mask greetd.service 2>/dev/null || true' \
    --run-command 'sed -i -e "s/^GRUB_TERMINAL_OUTPUT=.*/GRUB_TERMINAL_OUTPUT=\"console\"/" /etc/default/grub; grep -q "^GRUB_TERMINAL_OUTPUT=" /etc/default/grub || echo "GRUB_TERMINAL_OUTPUT=\"console\"" >>/etc/default/grub' \
    --run-command 'sed -i -e "s/^GRUB_GFXPAYLOAD_LINUX=.*/GRUB_GFXPAYLOAD_LINUX=\"text\"/" /etc/default/grub; grep -q "^GRUB_GFXPAYLOAD_LINUX=" /etc/default/grub || echo "GRUB_GFXPAYLOAD_LINUX=\"text\"" >>/etc/default/grub' \
    --run-command 'grub2-mkconfig -o /boot/grub2/grub.cfg' \
    --run-command 'zypper clean -a' \
    --run-command 'rm -rf /var/cache/zypp/* /tmp/* /var/tmp/* 2>/dev/null; true' \
    --run-command 'journalctl --vacuum-time=1s 2>/dev/null; true'

# 3. Bake the tier-5 per-app guest base disk INTO the overlay so
#    phase7-tier5-vm + phase7-tier5-audio always PASS on a --from-baked
#    clone (no per-test ~400 MB download).
#
# Strategy unchanged from the previous version:
# - Customize the cloud image on host (cached at ~/.cache/qdistro/).
# - virt-customize --upload it into the overlay's filesystem.
#
# Skip with QDWIN_SKIP_TIER5_BAKE=1 for a smaller baked image (the
# default tier-5 SKIP is acceptable for that path; see spec/29 §7).
if [ "${QDWIN_SKIP_TIER5_BAKE:-0}" != "1" ]; then
    CACHE_DIR="$HOME/.cache/qdistro"
    CLOUD_CACHE="$CACHE_DIR/tier5-base-cloud.qcow2"
    BAKED_CACHE="$CACHE_DIR/tier5-base-customized.qcow2"
    CLOUD_URL="https://download.opensuse.org/tumbleweed/appliances/openSUSE-Tumbleweed-Minimal-VM.x86_64-Cloud.qcow2"
    install -d "$CACHE_DIR"
    if [ ! -s "$CLOUD_CACHE" ]; then
        echo "[bake] one-time host download of tier-5 cloud base → $CLOUD_CACHE"
        wget -q --show-progress -O "$CLOUD_CACHE.partial" "$CLOUD_URL" \
            && mv "$CLOUD_CACHE.partial" "$CLOUD_CACHE"
        if [ ! -s "$CLOUD_CACHE" ]; then
            echo "[bake] WARN: cloud download failed; tier-5 bake skipped" >&2
            rm -f "$CLOUD_CACHE.partial"
        fi
    else
        echo "[bake] tier-5 cloud base already cached ($(du -h "$CLOUD_CACHE" | cut -f1))"
    fi
    if [ -s "$CLOUD_CACHE" ]; then
        if [ ! -s "$BAKED_CACHE" ]; then
            echo "[bake] customizing tier-5 base on host (waypipe + qga + publisher)..."
            cp --reflink=auto "$CLOUD_CACHE" "$BAKED_CACHE.partial"
            PUBLISHER_TMP="$(mktemp /tmp/qd-pub-XXXXXX.sh)"
            if [ ! -f "$TIER5_BUILD_GUEST" ]; then
                echo "[bake] FAIL: tier-5 builder not found at $TIER5_BUILD_GUEST" >&2
                rm -f "$PUBLISHER_TMP" "$BAKED_CACHE.partial"
                exit 5
            fi
            sed -n '/cat >"\$PUBLISHER" <<'\''PUBEOF'\''/,/^PUBEOF$/p' \
                "$TIER5_BUILD_GUEST" \
                | sed '1d;$d' >"$PUBLISHER_TMP"
            chmod +x "$PUBLISHER_TMP"
            virt-customize -a "$BAKED_CACHE.partial" \
                --run-command 'zypper -n --no-gpg-checks refresh' \
                --run-command 'zypper -n install --no-recommends waypipe wayland-utils qemu-guest-agent kbd alsa-utils' \
                --run-command 'systemctl enable qemu-guest-agent.service' \
                --run-command 'systemctl enable serial-getty@ttyS0.service' \
                --copy-in "$PUBLISHER_TMP:/usr/local/bin/" \
                --run-command "mv /usr/local/bin/$(basename "$PUBLISHER_TMP") /usr/local/bin/qdistro-tier5-publisher.sh" \
                --run-command 'chmod +x /usr/local/bin/qdistro-tier5-publisher.sh' \
                --run-command 'echo "qdistro-tier5-base" >/etc/hostname' \
                --root-password "password:${QDISTRO_VM_PASSWORD:?}" \
                --run-command 'modprobe vsock; modprobe vhost_vsock || true' \
                --run-command 'zypper clean -a' \
                >/dev/null
            rm -f "$PUBLISHER_TMP"
            virt-sparsify --in-place "$BAKED_CACHE.partial" 2>/dev/null || true
            mv "$BAKED_CACHE.partial" "$BAKED_CACHE"
            echo "[bake] tier-5 customized base cached → $BAKED_CACHE ($(du -h "$BAKED_CACHE" | cut -f1))"
        else
            echo "[bake] tier-5 customized base already cached ($(du -h "$BAKED_CACHE" | cut -f1))"
        fi
        echo "[bake] uploading tier-5 base into baked overlay..."
        virt-customize -a "$PARTIAL" \
            --mkdir /var/lib/libvirt/images \
            --upload "$BAKED_CACHE:/var/lib/libvirt/images/qdistro-tier5-base.qcow2" \
            --run-command 'chmod 0644 /var/lib/libvirt/images/qdistro-tier5-base.qcow2' \
            >/dev/null
        echo "[bake] tier-5 base uploaded into baked overlay"
    fi
fi

# 4. Sparsify in-place to reclaim zero blocks libguestfs left behind.
echo "[bake] sparsifying overlay..."
virt-sparsify --in-place "$PARTIAL" 2>/dev/null || true

# 5. Promote.
if [ -f "$BAKED" ] && [ "$FORCE" -eq 1 ]; then
    rm -f "$BAKED"
fi
mv "$PARTIAL" "$BAKED"
chmod 0644 "$BAKED"

echo "[bake] OK: $BAKED ready"
qemu-img info "$BAKED" | sed 's/^/    /'
echo
echo "[bake] use it via:  $SCRIPT_DIR/clone-baseweed.sh <prefix> --from-baked"
