#!/bin/bash
# §Phase-7 tier-4 — build the SPICE-capable guest disk image.
#
# Produces /var/lib/libvirt/images/qdistro-tier4-base.qcow2 with:
#   - openSUSE Tumbleweed Minimal-VM Cloud base
#     for selection sync; labwc is a tiny Wayland compositor that
#   - qemu-guest-agent service enabled (so the host can drive guest-
#     interactive console access).
#   - root password from $QDISTRO_VM_PASSWORD, so
#     interactive console attach for manual clipboard verification is
#     trivially scriptable.
#   - qdistro-tier4-clip-set.sh helper installed at /usr/local/bin —
#     guest-side wl-copy wrapper that the host can invoke via qga
#     guest-exec to push a string into the guest clipboard.
#
# Linux-only per spec/00 (memory qdistro_linux_only.md).
#
# Run this once on the host. The bats coverage in
# image is absent so this isn't a hard prereq for the rest of the
# suite.
#
# Build cost: one-time download of ~400 MB (Tumbleweed-Minimal-VM
# Cloud qcow2), then ~60 s of customization. spawn-tier4.sh's per-VM
# linked clones come from this base when TIER4_DISK_BASE points here.
set -euo pipefail

usage() {
    cat <<'EOF'
qdistro-tier4 SPICE guest image builder.

Usage:
  build-guest-image.sh [--force] [--mirror URL] [--dest PATH]

Options:
  --force        Rebuild even if dest already exists.
  --mirror URL   Override Tumbleweed-Minimal-VM download URL.
                 Default: https://download.opensuse.org/tumbleweed/
                          appliances/openSUSE-Tumbleweed-Minimal-VM
                          .x86_64-Cloud.qcow2
  --dest PATH    Output path (default
                 /var/lib/libvirt/images/qdistro-tier4-base.qcow2).

Reqs: virt-customize, qemu-img, wget. zypper install libguestfs
guestfs-tools qemu-tools.
EOF
}

FORCE=0
DEST=/var/lib/libvirt/images/qdistro-tier4-base.qcow2
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

DEST_DIR="$(dirname "$DEST")"
if [ "$(id -u)" -ne 0 ]; then
    if ! [ -w "$DEST_DIR" ] && ! mkdir -p "$DEST_DIR" 2>/dev/null; then
        echo "[tier4-build] non-root run requires --dest under a "\
             "writable directory; $DEST_DIR is not writable. Either" >&2
        echo "  sudo bash $0   # writes default /var/lib/libvirt/images/" >&2
        echo "  $0 --dest \$HOME/.local/share/libvirt/images/qdistro-tier4-base.qcow2" >&2
        exit 2
    fi
fi
for tool in virt-customize qemu-img wget; do
    command -v "$tool" >/dev/null || {
        echo "[tier4-build] missing: $tool" >&2; exit 2; }
done

if [ -f "$DEST" ] && [ "$FORCE" != "1" ]; then
    echo "[tier4-build] $DEST already exists; use --force to rebuild" >&2
    exit 0
fi

WORK="$(mktemp -d /tmp/tier4-build.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

BASE_QCOW="$WORK/base.qcow2"
echo "[tier4-build] downloading base image..."
wget -q --show-progress -O "$BASE_QCOW" "$MIRROR" || {
    echo "[tier4-build] FAIL: download from $MIRROR" >&2
    exit 3
}

# Stage the in-guest helper that pushes a string into the guest
# clipboard via wl-copy. The host invokes this via qga guest-exec
# during the clipboard live test. The script waits briefly for
# first-boot timing can race the test invocation.
HELPER="$WORK/qdistro-tier4-clip-set.sh"
cat >"$HELPER" <<'HELPEOF'
#!/bin/bash
# qdistro-tier4-clip-set — push a string into the guest's wayland
# clipboard. Used by the host-side spec/10 SPICE clipboard live test.
#
#   qdistro-tier4-clip-set.sh <payload>
#
# The guest compositor must be running (its .service file is enabled at
# image build); without it the host can never observe a sync.
# wl-copy needs a running compositor with a wl_seat; we start a
# detached labwc instance if none is up (idempotent — labwc exits
# silently if a wayland-1 socket already binds).
set -uo pipefail
PAYLOAD="${1:?usage: $0 <payload>}"

LOG=/var/log/qdistro-tier4-clip-set.log
exec >>"$LOG" 2>&1
echo "=== $(date -Iseconds): tier4-clip-set ==="


# Need a Wayland compositor for wl-copy. Use labwc against a tty
# session; it'll die silently if we already have one bound.
mkdir -p /run/user/0
export XDG_RUNTIME_DIR=/run/user/0
chmod 0700 /run/user/0
if ! pgrep -x labwc >/dev/null; then
    nohup labwc >/var/log/qdistro-labwc.log 2>&1 </dev/null &
    sleep 1
fi
export WAYLAND_DISPLAY=wayland-0
for _ in 1 2 3 4 5; do
    [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] && break
    sleep 0.5
done

sleep 1

printf '%s' "$PAYLOAD" | wl-copy
echo "wl-copy exit=$?"
HELPEOF
chmod 0755 "$HELPER"

echo "[tier4-build] customizing..."
# Tumbleweed Minimal-VM Cloud image is mutable (not transactional), so
# zypper install works directly via virt-customize.
virt-customize -a "$BASE_QCOW" \
    --run-command 'systemctl enable qemu-guest-agent.service' \
    --run-command 'systemctl enable serial-getty@ttyS0.service' \
    --mkdir /etc/systemd/system/multi-user.target.wants \
    --link /usr/lib/systemd/system/qemu-guest-agent.service:/etc/systemd/system/multi-user.target.wants/qemu-guest-agent.service \
    --copy-in "$HELPER:/usr/local/bin/" \
    --run-command 'chmod +x /usr/local/bin/qdistro-tier4-clip-set.sh' \
    --run-command 'echo "qdistro-tier4-base" >/etc/hostname' \
    --root-password "password:${QDISTRO_VM_PASSWORD:?}" \
    >/dev/null

# Belt-and-braces: openSUSE Tumbleweed-Minimal-VM-Cloud's systemd
# `systemctl enable` above silently no-ops (it respects the preset
# unless --force is passed). Explicit --link below forces the
# multi-user.target.wants symlinks regardless of preset state.

# Sparsify to keep the published base small.
echo "[tier4-build] sparsifying..."
virt-sparsify --in-place "$BASE_QCOW" 2>/dev/null || true

# install -d would chmod the destination directory's permissions,
# which fails when --dest points at /tmp or another system-owned
# directory the caller doesn't own. mkdir -p suffices — the
# destination's existing perms are honoured.
mkdir -p "$(dirname "$DEST")"
mv "$BASE_QCOW" "$DEST"
chmod 0644 "$DEST"
# Only chown when running as root — non-root builds keep ownership
# as the invoking user (read-only for everyone else, which is what
# the bats test wants).
if [ "$(id -u)" -eq 0 ]; then
    chown root:root "$DEST"
fi

echo "[tier4-build] done: $DEST"
ls -lh "$DEST"
