#!/bin/bash
# §P10 tier-4-guest — build the nested-qdwin guest disk image.
#
# Produces /var/lib/libvirt/images/qdistro-tier4-guest.qcow2 with:
#   - openSUSE Tumbleweed Minimal-VM Cloud base
#   - libweston + qdwin built with role=guest (strips locker /
#     nested-manager; keeps qdwin_shell_v1 so the bystander can
#     enumerate inner toplevels)
#   - waypipe (guest end of the vsock display path)
#   - RDP publisher runtime: socat, PipeWire/FreeRDP runtime libraries,
#     and qdistro-forward (guest per-view RDP proxy)
#   - virtiofs guest-mount tooling (the kernel virtiofs driver ships
#     with the stock Tumbleweed kernel; user-space tools live in the
#     `virtiofsd` host pkg, none needed on the guest)
#   - alsa-utils (for virtio-snd `aplay -l` enumeration in s109)
#   - weston-terminal (the smoke wl_client we use to verify a guest-
#     side toplevel renders out via waypipe)
#   - qemu-guest-agent (so the host can drive aplay / ls /host /
#     publisher-launch via guest-exec)
#   - systemd user unit qdwin-guest-session.service that runs
#     `weston --backend=headless-backend.so --shell=qdwin-shell.so`
#     (the role=guest libweston launch shape — no DRM master, no
#     KMS, no real outputs; libweston-vendored's headless backend is
#     what handles the no-real-display case)
#   - systemd user unit qdistro-tier4-publisher.service that runs
#     /usr/local/bin/qdistro-tier4-publisher.sh after qdwin-guest-
#     session is up
#   - /etc/fstab line for the host-shared virtiofs mount on /host
#
# Spec: plan2/tasks/P10-tier4-guest-image-nested-qdwin.md §Phase D.
#
# Linux-only per spec/00.
#
# Build cost: one-time Tumbleweed download ~400MB, then ~60-120s of
# customization (qdwin compile + virt-customize). The s109 fast-gate
# skips the bake; the async path runs it once and caches.

set -euo pipefail

usage() {
    cat <<'EOF'
qdistro tier-4-guest image builder.

Usage:
  build-guest-image.sh [--force] [--mirror URL] [--dest PATH]
                       [--qdwin-src PATH] [--qdistro-src PATH]

Options:
  --force        Rebuild even if dest already exists.
  --mirror URL   Override Tumbleweed-Minimal-VM download URL.
                 Default: https://download.opensuse.org/tumbleweed/appliances/
                          openSUSE-Tumbleweed-Minimal-VM.x86_64-Cloud.qcow2
  --dest PATH    Output path
                 (default /var/lib/libvirt/images/qdistro-tier4-guest.qcow2).
  --qdwin-src P  Path to the qdwin source checkout to compile with
                 role=guest. Default: ../../qdwin relative to this script.
  --qdistro-src P
                 Path to the qdistro source checkout used to compile
                 qdistro-forward. Default: this repository root.

Reqs:
  virt-customize, qemu-img, wget, meson, ninja, libweston-devel,
  wayland-protocols-devel, wayland-devel, libxcursor-devel,
  freerdp-devel, winpr-devel, pipewire-devel.

Env:
  QDISTRO_VM_PASSWORD  Root password baked into the image (mandatory).
EOF
}

FORCE=0
DEST=/var/lib/libvirt/images/qdistro-tier4-guest.qcow2
MIRROR=https://download.opensuse.org/tumbleweed/appliances/openSUSE-Tumbleweed-Minimal-VM.x86_64-Cloud.qcow2
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
QDWIN_SRC="$(cd "$SCRIPT_DIR/../../qdwin" 2>/dev/null && pwd || echo '')"
QDISTRO_SRC="$(cd "$SCRIPT_DIR/.." && pwd)"

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)      usage; exit 0;;
        --force)        FORCE=1; shift;;
        --mirror)       shift; MIRROR="${1:?--mirror requires URL}"; shift;;
        --dest)         shift; DEST="${1:?--dest requires PATH}"; shift;;
        --qdwin-src)    shift; QDWIN_SRC="${1:?--qdwin-src requires PATH}"; shift;;
        --qdistro-src)  shift; QDISTRO_SRC="${1:?--qdistro-src requires PATH}"; shift;;
        *) echo "unknown arg: $1" >&2; usage >&2; exit 2;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "[tier4-guest-build] must run as root (writes /var/lib/libvirt/images/)" >&2
    exit 2
fi
for tool in virt-customize qemu-img wget meson ninja; do
    command -v "$tool" >/dev/null || {
        echo "[tier4-guest-build] missing tool: $tool" >&2
        exit 2
    }
done
if [ -z "${QDISTRO_VM_PASSWORD:-}" ]; then
    echo "[tier4-guest-build] FAIL: set QDISTRO_VM_PASSWORD env var (root pw for the guest)" >&2
    exit 2
fi
if [ -z "$QDWIN_SRC" ] || [ ! -f "$QDWIN_SRC/meson.build" ]; then
    echo "[tier4-guest-build] FAIL: --qdwin-src '$QDWIN_SRC' does not contain a meson.build" >&2
    exit 2
fi
if [ -z "$QDISTRO_SRC" ] || [ ! -f "$QDISTRO_SRC/daemons/meson.build" ]; then
    echo "[tier4-guest-build] FAIL: --qdistro-src '$QDISTRO_SRC' does not contain daemons/meson.build" >&2
    exit 2
fi

if [ -f "$DEST" ] && [ "$FORCE" != "1" ]; then
    echo "[tier4-guest-build] $DEST already exists; use --force to rebuild" >&2
    exit 0
fi

WORK="$(mktemp -d /tmp/tier4-guest-build.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# --- Step 1: compile qdwin with role=guest on the *host*, then we
# copy the .so into the guest image. Compiling inside virt-customize
# would pull a full toolchain into the guest, ballooning the image.
QDWIN_BUILD="$WORK/qdwin-build"
echo "[tier4-guest-build] compiling qdwin with role=guest..."
meson setup "$QDWIN_BUILD" "$QDWIN_SRC" -Drole=guest --buildtype=release \
    >"$WORK/meson-setup.log" 2>&1 || {
    echo "[tier4-guest-build] FAIL: meson setup role=guest failed (see $WORK/meson-setup.log)" >&2
    cat "$WORK/meson-setup.log" >&2
    exit 3
}
meson compile -C "$QDWIN_BUILD" qdwin-shell qdwin-bystander \
    >"$WORK/meson-compile.log" 2>&1 || {
    echo "[tier4-guest-build] FAIL: meson compile failed (see $WORK/meson-compile.log)" >&2
    cat "$WORK/meson-compile.log" >&2
    exit 3
}
QDWIN_SO="$QDWIN_BUILD/qdwin-shell.so"
QDWIN_BYSTANDER="$QDWIN_BUILD/qdwin-bystander"
[ -f "$QDWIN_SO" ] || { echo "[tier4-guest-build] FAIL: qdwin-shell.so missing post-compile" >&2; exit 3; }
[ -x "$QDWIN_BYSTANDER" ] || { echo "[tier4-guest-build] FAIL: qdwin-bystander missing post-compile" >&2; exit 3; }

# qdistro-forward is the RDP-mode per-view proxy. Build it on the host
# and copy the binary into the image so the guest does not need a
# compiler toolchain.
QDISTRO_DAEMONS_BUILD="$WORK/qdistro-daemons-build"
echo "[tier4-guest-build] compiling qdistro-forward..."
meson setup "$QDISTRO_DAEMONS_BUILD" "$QDISTRO_SRC/daemons" --prefix=/usr --buildtype=release \
    >"$WORK/daemons-meson-setup.log" 2>&1 || {
    echo "[tier4-guest-build] FAIL: meson setup for qdistro-forward failed (see $WORK/daemons-meson-setup.log)" >&2
    cat "$WORK/daemons-meson-setup.log" >&2
    exit 3
}
meson compile -C "$QDISTRO_DAEMONS_BUILD" qdistro-forward \
    >"$WORK/daemons-meson-compile.log" 2>&1 || {
    echo "[tier4-guest-build] FAIL: meson compile qdistro-forward failed (see $WORK/daemons-meson-compile.log)" >&2
    cat "$WORK/daemons-meson-compile.log" >&2
    exit 3
}
QDISTRO_FORWARD="$QDISTRO_DAEMONS_BUILD/qdistro-forward"
[ -x "$QDISTRO_FORWARD" ] || {
    echo "[tier4-guest-build] FAIL: qdistro-forward missing post-compile; install FreeRDP3, WinPR3, PipeWire, and Wayland development packages" >&2
    exit 3
}

# --- Step 2: download base
BASE_QCOW="$WORK/base.qcow2"
echo "[tier4-guest-build] downloading base image..."
wget -q --show-progress -O "$BASE_QCOW" "$MIRROR" || {
    echo "[tier4-guest-build] FAIL: download from $MIRROR" >&2
    exit 3
}

# --- Step 3: stage in-tree publisher + systemd units
PUBLISHER_SRC="$SCRIPT_DIR/qdistro-tier4-publisher.sh"
[ -f "$PUBLISHER_SRC" ] || {
    echo "[tier4-guest-build] FAIL: in-tree publisher missing at $PUBLISHER_SRC" >&2
    exit 4
}
cp "$PUBLISHER_SRC" "$WORK/qdistro-tier4-publisher.sh"
chmod 0755 "$WORK/qdistro-tier4-publisher.sh"

cat >"$WORK/tier4-weston.ini" <<'EOF'
[core]
shell=qdwin-shell.so
backend=headless-backend.so,pipewire-backend.so

[pipewire]
num-outputs=8

[shell]
locking=false
EOF

cat >"$WORK/qdistro-pipewire-admin.service" <<'UNIT'
[Unit]
Description=qdistro tier-4 admin PipeWire daemon
After=user@1000.service
Wants=user@1000.service

[Service]
Type=simple
User=admin
Environment=XDG_RUNTIME_DIR=/run/user/1000
ExecStartPre=/usr/bin/mkdir -p /run/user/1000
ExecStartPre=/usr/bin/chown admin:admin /run/user/1000
ExecStartPre=/usr/bin/chmod 0700 /run/user/1000
ExecStart=/usr/bin/pipewire
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT

# qdwin-guest-session.service — runs weston with role=guest qdwin
# plugin under the admin user. headless-backend is what lets weston
# come up without a real DRM device; the bystander wraps inner
# toplevels and waypipe-server ferries them out.
cat >"$WORK/qdwin-guest-session.service" <<'UNIT'
[Unit]
Description=qdistro tier-4-guest nested qdwin session
# §P10 fix-pass H-B: depend on the *instance* user@1000.service, not
# the bare template `user@.service` (which is unresolvable). The
# guest's admin user is uid 1000 (fixed by build-guest-image.sh's
# `useradd -m -u 1000 admin`).
After=user@1000.service qdistro-pipewire-admin.service
Wants=user@1000.service qdistro-pipewire-admin.service

[Service]
Type=simple
User=admin
Environment=XDG_RUNTIME_DIR=/run/user/1000
# §P10 fix-pass H-B (operator-mandated simplest fix): /run is tmpfs and
# is empty on every boot. systemd-logind would normally create
# /run/user/1000 via pam_systemd at the first interactive login, but
# this is a headless system unit with no PAM session. mkdir at unit
# start guarantees the dir exists before weston tries to bind
# wayland-0 under it.
# TODO: loginctl enable-linger admin (proper fix — lets user@1000.service
#       start at boot and own /run/user/1000 the systemd way). Deferred
#       to keep the P10 prototype's startup path minimal.
ExecStartPre=/usr/bin/mkdir -p /run/user/1000
ExecStartPre=/usr/bin/chown admin:admin /run/user/1000
ExecStartPre=/usr/bin/chmod 0700 /run/user/1000
ExecStart=/usr/bin/weston --config=/etc/qdistro/tier4-weston.ini \
    --socket=wayland-0
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT

# qdistro-tier4-publisher.service — binds vsock after qdwin-guest is
# up. Reads the vsock port from /etc/qdistro/tier4-port (written by
# the host-side spawn-tier4 via cloud-init / virsh set-user-data;
# defaults to 7777 for the s109 smoke).
cat >"$WORK/qdistro-tier4-publisher.service" <<'UNIT'
[Unit]
Description=qdistro tier-4 nested-qdwin vsock publisher
After=qdwin-guest-session.service
Requires=qdwin-guest-session.service

[Service]
Type=simple
User=admin
Environment=XDG_RUNTIME_DIR=/run/user/1000
EnvironmentFile=-/etc/qdistro/tier4.env
# §P10 fix-pass H-B: paired with qdwin-guest-session.service — ensure
# /run/user/1000 exists for the admin user before the publisher tries
# to use it as XDG_RUNTIME_DIR for its $SOCK_DIR resolution.
# TODO: loginctl enable-linger admin (proper fix; see qdwin-guest-session.service).
ExecStartPre=/usr/bin/mkdir -p /run/user/1000
ExecStart=/bin/sh -c '/usr/local/bin/qdistro-tier4-publisher.sh "${TIER4_PORT:-7777}"'
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT

# Default env file with the port placeholder. spawn-tier4 (P11) will
# overwrite /etc/qdistro/tier4.env via cloud-init.
cat >"$WORK/tier4.env" <<'EOF'
# qdistro tier-4-guest publisher env. spawn-tier4 overwrites this.
TIER4_PORT=7777
EOF

cat >"$WORK/tier4-publisher.conf" <<'EOF'
# qdistro tier-4 guest publisher config.
# The default path remains waypipe. Set the alias below to rdp for the
# guest RDP publisher path.
# QDISTRO_TIER4_STREAMING_METHOD=rdp
# QDISTRO_TIER4_RDP_SUBSCRIBE=last
# QDISTRO_TIER4_RDP_PEER_LABEL=tier4-rdp
# QDISTRO_TIER4_RDP_CREDS=/run/qdistro-tier4-rdp.env
EOF

# /etc/fstab line for the virtiofs host-share. The mount tag
# `qdistro-host` must match the <filesystem> source in
# domain-template.xml.
cat >"$WORK/fstab.host-mount" <<'EOF'
qdistro-host /host virtiofs nofail,_netdev 0 0
EOF

# §P10 fix-pass SF3: write the root password to a mode-0600 temp file
# and pass it to virt-customize as `--root-password file:<path>`.
# Avoids leaking the plaintext via argv → /proc/PID/cmdline AND, more
# importantly, avoids leaking it into virt-customize stderr (which
# s109 tees to /tmp/s109-bake.log and dumps to stderr on bake failure;
# CI logs would otherwise capture `--root-password password:<value>`).
PW_FILE="$WORK/root-pw"
(umask 077 && printf '%s\n' "${QDISTRO_VM_PASSWORD:?}" >"$PW_FILE")
chmod 0600 "$PW_FILE"
# trap already covers $WORK, but be explicit: scrub the password file
# on exit even if the trap is somehow bypassed.
trap 'shred -u "$PW_FILE" 2>/dev/null || rm -f "$PW_FILE"; rm -rf "$WORK"' EXIT

echo "[tier4-guest-build] customizing..."
# Tumbleweed Minimal-VM Cloud image is mutable; zypper install works
# directly via virt-customize. Package set per task §Phase D.
#
# §P10 fix-pass SF4 (correctness): virtiofsd-client is the spec'd
# package per P10-task §Phase D. The kernel virtiofs driver alone
# handles `mount -t virtiofs`, but virtiofsd-client ships diagnostic
# helpers (e.g. virtiofsd-list-mounts) that the operator runbook
# expects. Adding it now to match the spec's explicit list and avoid
# silent divergence.
virt-customize -a "$BASE_QCOW" \
    --install "weston,libweston-14,waypipe,qemu-guest-agent,kbd,alsa-utils,weston-terminal,virtiofsd-client,socat,pipewire,freerdp,freerdp-server" \
    --copy-in "$QDWIN_SO:/usr/lib64/weston/" \
    --copy-in "$QDWIN_BYSTANDER:/usr/bin/" \
    --run-command 'chmod +x /usr/bin/qdwin-bystander' \
    --copy-in "$QDISTRO_FORWARD:/usr/bin/" \
    --run-command 'chmod +x /usr/bin/qdistro-forward' \
    --copy-in "$WORK/qdistro-tier4-publisher.sh:/usr/local/bin/" \
    --run-command 'chmod +x /usr/local/bin/qdistro-tier4-publisher.sh' \
    --copy-in "$WORK/qdwin-guest-session.service:/etc/systemd/system/" \
    --copy-in "$WORK/qdistro-pipewire-admin.service:/etc/systemd/system/" \
    --copy-in "$WORK/qdistro-tier4-publisher.service:/etc/systemd/system/" \
    --run-command 'mkdir -p /etc/qdistro' \
    --copy-in "$WORK/tier4.env:/etc/qdistro/" \
    --copy-in "$WORK/tier4-weston.ini:/etc/qdistro/" \
    --copy-in "$WORK/tier4-publisher.conf:/etc/qdistro/" \
    --run-command 'test -x /usr/bin/socat || test -x /usr/local/bin/socat' \
    --run-command 'test -x /usr/bin/qdistro-forward' \
    --run-command 'mkdir -p /host' \
    --run-command 'chmod 0644 /etc/fstab' \
    --append-line "/etc/fstab:$(cat "$WORK/fstab.host-mount")" \
    --run-command 'useradd -m -u 1000 admin || true' \
    --run-command 'mkdir -p /run/user/1000 && chown admin:admin /run/user/1000' \
    --run-command 'systemctl enable qemu-guest-agent.service' \
    --run-command 'systemctl enable serial-getty@ttyS0.service' \
    --run-command 'systemctl enable qdistro-pipewire-admin.service' \
    --run-command 'systemctl enable qdwin-guest-session.service' \
    --run-command 'systemctl enable qdistro-tier4-publisher.service' \
    --run-command 'echo "qdistro-tier4-guest" >/etc/hostname' \
    --root-password "file:$PW_FILE" \
    --run-command 'modprobe vsock; modprobe vhost_vsock || true' \
    >/dev/null

# Scrub the password file immediately after virt-customize finishes
# (in addition to the EXIT trap). Defence-in-depth against the file
# being readable to any concurrent process during the bake window.
shred -u "$PW_FILE" 2>/dev/null || rm -f "$PW_FILE"

# Sparsify the result so the published base disk stays small.
echo "[tier4-guest-build] sparsifying..."
virt-sparsify --in-place "$BASE_QCOW" 2>/dev/null || true

install -d "$(dirname "$DEST")"
mv "$BASE_QCOW" "$DEST"
chmod 0644 "$DEST"
chown root:root "$DEST"

echo "[tier4-guest-build] done: $DEST"
ls -lh "$DEST"
