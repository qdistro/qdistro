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

set -euo pipefail

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
  QDISTRO_VM_PASSWORD  Root password baked into the image (mandatory;
                       same as tier-5).
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
if [ -z "${QDISTRO_VM_PASSWORD:-}" ]; then
    echo "[tier5b-build] FAIL: set QDISTRO_VM_PASSWORD env var (root pw for the guest)" >&2
    exit 2
fi

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
PUBLISHER="$WORK/qdistro-tier5b-publisher.sh"
cat >"$PUBLISHER" <<PUBEOF
#!/bin/bash
# qdistro-tier5b-publisher — runs inside the tier-5b per-app guest VM.
# Invoked by the host via qemu-guest-agent guest-exec:
#
#   qdistro-tier5b-publisher.sh <vsock_port> [extra_args...]
#
# Unlike tier-5's session-grain publisher, tier-5b is pinned to a
# single binary at image-build time. The first guest-exec arg is the
# vsock port; the rest are appended to the baked binary's argv. This
# means a compromised host cannot ask the guest to run an arbitrary
# command — the guest publisher refuses anything but a port + extra
# args for the one binary it was built for.
#
# Per \`waypipe(1)\`: when --vsock is set with \`-s [s]CID:port\`, the
# guest-side waypipe-server connects out to vsock CID=2 (the host)
# on \$PORT. Symmetry: host-side \`client -s s<host_cid>:port\` listens,
# guest-side \`server -s 2:port\` connects.
set -uo pipefail
PORT="\${1:?usage: \$0 <port> [extra args...]}"
shift

# Hard-coded by build-guest-image.sh:
APP_BIN="$APP_BIN"

LOG=/var/log/qdistro-tier5b-publisher.log
exec >>"\$LOG" 2>&1
echo "=== \$(date -Iseconds): tier5b-publisher port=\$PORT bin=\$APP_BIN extra='\$*' ==="

# Wait briefly for vsock module to be live.
modprobe vhost_vsock 2>/dev/null || true
modprobe vsock 2>/dev/null || true
for _ in 1 2 3 4 5; do
    [ -e /dev/vsock ] && break
    sleep 0.5
done

# MOZ_ENABLE_WAYLAND=1 — Firefox: native Wayland, no XWayland needed.
# Per probe verdict §"When direct is not sufficient", X11-needing apps
# are out of scope for the MVP.
if [ "\$APP_BIN" = "firefox" ]; then
    export MOZ_ENABLE_WAYLAND=1
fi

# guest-side waypipe-server: connects out to host CID=2 on \$PORT and
# execs the baked binary. Single-app discipline: extra args are
# appended to the baked binary, never substitute it.
exec waypipe --vsock -s "2:\$PORT" server -- "\$APP_BIN" "\$@"
PUBEOF
chmod 0755 "$PUBLISHER"

echo "[tier5b-build] customizing for app=$APP pkg=$PKG..."
# shellcheck disable=SC2086
virt-customize -a "$BASE_QCOW" \
    --install "waypipe,wayland-utils,qemu-guest-agent,kbd,alsa-utils,$PKG" \
    --run-command 'systemctl enable qemu-guest-agent.service' \
    --run-command 'systemctl enable serial-getty@ttyS0.service' \
    --copy-in "$PUBLISHER:/usr/local/bin/" \
    --run-command 'chmod +x /usr/local/bin/qdistro-tier5b-publisher.sh' \
    --run-command "echo \"qdistro-tier5b-$APP-base\" >/etc/hostname" \
    --root-password "password:${QDISTRO_VM_PASSWORD:?}" \
    --run-command 'modprobe vsock; modprobe vhost_vsock || true' \
    >/dev/null

# Sparsify.
echo "[tier5b-build] sparsifying..."
virt-sparsify --in-place "$BASE_QCOW" 2>/dev/null || true

# Move into place.
install -d "$(dirname "$DEST")"
mv "$BASE_QCOW" "$DEST"
chmod 0644 "$DEST"
chown root:root "$DEST"

echo "[tier5b-build] done: $DEST"
ls -lh "$DEST"
