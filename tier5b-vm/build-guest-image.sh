#!/bin/bash
# §Phase-7 tier-5b (per-app VM-windowed) — build a per-app guest disk
# image targeting a single Wayland-native application.
#
# Tier-5b differs from tier-5 in goal, not transport:
#   - tier-5  : session-grain VM (one VM publishes a full guest session
#               via waypipe-vsock; the user gets one host toplevel).
#   - tier-5b : per-app-grain VM (one VM publishes a single, specific
#               GUI app; the user gets a seamless host toplevel per
#               app instance).
#
# Probe-verdict (plan2/research/qdwin-nested-over-vsock/12-verdict.md
# 2026-05-17): publisher shape is `direct` — waypipe-server runs in the
# guest with the target app as its only client. ~80% of the code-path
# is the same as tier-5; this file forks tier-5's
# build-guest-image.sh and slims the install set for one app.
#
# Produces (default):
#   /var/lib/libvirt/images/qdistro-tier5b-<app>-base.qcow2
#
# The disk contains:
#   - openSUSE Tumbleweed Minimal-VM Cloud base
#   - waypipe + wayland-utils
#   - qemu-guest-agent (host drives publisher via guest-exec)
#   - the target app package (default MozillaFirefox)
#   - /usr/local/bin/qdistro-tier5b-publisher.sh (forked from tier-5,
#     enforces "single, named app" — refuses arbitrary argv)
#
# Linux-only per spec/00.
#
# Build cost: one-time download of ~400MB (Tumbleweed Minimal-VM
# cloud qcow2) + ~30-60s of customization per app variant.
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
#   workflows that log in over the serial console with the fixed qdistro test VM password.
#   Set QDISTRO_GUEST_BAKE_DEBUG_PASSWORD=0 for a HARDENED / production
#   build: no root password is baked and no test VM password is required.
#   When the flag is 1 the virt-customize argument set is byte-identical to
#   the historical behavior, so any deterministic output is unchanged unless
#   the operator explicitly opts into the hardened path.

set -euo pipefail

# Opt-in debug-password gate (default on; see header).
QDISTRO_GUEST_BAKE_DEBUG_PASSWORD="${QDISTRO_GUEST_BAKE_DEBUG_PASSWORD:-1}"

usage() {
    cat <<'EOF'
qdistro-tier5b per-app guest image builder.

Usage:
  build-guest-image.sh [--force] [--mirror URL] [--dest PATH]
                       [--app APP] [--pkg PKG]

Options:
  --force        Rebuild even if dest already exists.
  --mirror URL   Override Tumbleweed-Minimal-VM download URL.
                 Default: https://download.opensuse.org/tumbleweed/appliances/
                          openSUSE-Tumbleweed-Minimal-VM.x86_64-Cloud.qcow2
  --dest PATH    Output path. Default
                 /var/lib/libvirt/images/qdistro-tier5b-<app>-base.qcow2
  --app  APP     Target app name (used to derive default dest, the
                 publisher's allowed-binary list, and the launcher
                 metadata). Default: firefox
  --pkg  PKG     Zypper package name(s) to install for the app, comma
                 list. Default depends on --app:
                   firefox -> MozillaFirefox

Reqs:
  virt-customize, qemu-img, wget. zypper install libguestfs guestfs-tools qemu-tools.

Env:
  QDISTRO_GUEST_BAKE_DEBUG_PASSWORD
                       1 (default) bakes the fixed debug root password;
                       0 = hardened build, no password baked. Either way
                       the output image is 0640 root:root.
EOF
}

FORCE=0
DEST=""
MIRROR=https://download.opensuse.org/tumbleweed/appliances/openSUSE-Tumbleweed-Minimal-VM.x86_64-Cloud.qcow2
APP=firefox
PKG=""

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)   usage; exit 0;;
        --force)     FORCE=1; shift;;
        --mirror)    shift; MIRROR="${1:?--mirror requires URL}"; shift;;
        --dest)      shift; DEST="${1:?--dest requires PATH}"; shift;;
        --app)       shift; APP="${1:?--app requires APP}"; shift;;
        --pkg)       shift; PKG="${1:?--pkg requires PKG}"; shift;;
        *) echo "unknown arg: $1" >&2; usage >&2; exit 2;;
    esac
done

# Resolve per-app defaults.
case "$APP" in
    firefox)
        APP_BIN="firefox"
        : "${PKG:=MozillaFirefox}"
        ;;
    *)
        APP_BIN="$APP"
        : "${PKG:=$APP}"
        ;;
esac

if [ -z "$DEST" ]; then
    DEST="/var/lib/libvirt/images/qdistro-tier5b-${APP}-base.qcow2"
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "[tier5b-build] must run as root (writes /var/lib/libvirt/images/)" >&2
    exit 2
fi
for tool in virt-customize qemu-img wget; do
    command -v "$tool" >/dev/null || { echo "[tier5b-build] missing: $tool" >&2; exit 2; }
done
if [ -f "$DEST" ] && [ "$FORCE" != "1" ]; then
    echo "[tier5b-build] $DEST already exists; use --force to rebuild" >&2
    exit 0
fi

WORK="$(mktemp -d /tmp/tier5b-build.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

BASE_QCOW="$WORK/base.qcow2"
echo "[tier5b-build] downloading base image..."
wget -q --show-progress -O "$BASE_QCOW" "$MIRROR" || {
    echo "[tier5b-build] FAIL: download from $MIRROR" >&2
    exit 3
}

# Stage the per-app publisher. Unlike tier-5's publisher (which accepts
# arbitrary argv), tier-5b's publisher hard-codes the allowed app
# binary at image-build time so guest-exec from the host cannot be
# coerced into running a different binary.
#
# Correctness HIGH-3 (P05b): we cp the in-tree source-of-truth
# publisher and sed-patch the APP_BIN default in place. The prior
# inline here-doc baked a separate copy that silently diverged from
# the canonical script under qdistro-tier5b-publisher.sh.
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_PUBLISHER="$SRC_DIR/qdistro-tier5b-publisher.sh"
PUBLISHER="$WORK/qdistro-tier5b-publisher.sh"
if [ ! -f "$SRC_PUBLISHER" ]; then
    echo "[tier5b-build] FAIL: in-tree publisher source not found at $SRC_PUBLISHER" >&2
    exit 4
fi
cp "$SRC_PUBLISHER" "$PUBLISHER"
# Patch the APP_BIN default: replace the `${QDISTRO_TIER5B_APP_BIN:-firefox}`
# fallback with the build-time literal so the baked image can't be
# coerced into running a different binary via env.
sed -i -E "s|^APP_BIN=\"\\\$\\{QDISTRO_TIER5B_APP_BIN:-[^}]*\\}\"|APP_BIN=\"$APP_BIN\"|" "$PUBLISHER"
if ! grep -qE "^APP_BIN=\"$APP_BIN\"\$" "$PUBLISHER"; then
    echo "[tier5b-build] FAIL: APP_BIN substitution missed in baked publisher (expected APP_BIN=\"$APP_BIN\")" >&2
    grep -nE '^APP_BIN=' "$PUBLISHER" >&2 || true
    exit 4
fi
chmod 0755 "$PUBLISHER"

# Build the (optional) root-password argument. Default bakes the debug
# password; the hardened path (flag=0) bakes none.
PW_ARGS=()
if [ "$QDISTRO_GUEST_BAKE_DEBUG_PASSWORD" = "1" ]; then
    PW_ARGS=(--root-password 'password:Pa_ssw0rd45')
    echo "[tier5b-build] baking debug root password (QDISTRO_GUEST_BAKE_DEBUG_PASSWORD=1)"
else
    echo "[tier5b-build] HARDENED build: no root password baked (QDISTRO_GUEST_BAKE_DEBUG_PASSWORD=0)"
fi

echo "[tier5b-build] customizing for app=$APP pkg=$PKG..."
# shellcheck disable=SC2086
virt-customize -a "$BASE_QCOW" \
    --install "waypipe,wayland-utils,qemu-guest-agent,kbd,alsa-utils,$PKG" \
    --run-command 'systemctl enable qemu-guest-agent.service' \
    --run-command 'systemctl enable serial-getty@ttyS0.service' \
    --copy-in "$PUBLISHER:/usr/local/bin/" \
    --run-command 'chmod +x /usr/local/bin/qdistro-tier5b-publisher.sh' \
    --run-command "echo \"qdistro-tier5b-$APP-base\" >/etc/hostname" \
    "${PW_ARGS[@]}" \
    --run-command 'modprobe vsock; modprobe vhost_vsock || true' \
    >/dev/null

# Sparsify.
echo "[tier5b-build] sparsifying..."
virt-sparsify --in-place "$BASE_QCOW" 2>/dev/null || true

# Move into place.
install -d "$(dirname "$DEST")"
mv "$BASE_QCOW" "$DEST"
# Base image is sensitive (may contain a baked debug password); keep it
# non-world-readable (0640 root:root), mirroring the Tier-4 overlay.
chmod 0640 "$DEST"
chown root:root "$DEST"

echo "[tier5b-build] done: $DEST"
ls -lh "$DEST"
