#!/bin/bash
# R9 viewer: keep one local DRM compositor alive while the RDP thin-client is
# stopped/restarted independently.  The source's remote output is decoded 1:1.
set -euo pipefail

RT=/run/mm-r9-viewer
SOCK=r9-viewer
HOST=${HOST:-10.0.2.2}
PORT=${PORT:-3389}
W=${W:-1280}
H=${H:-800}
MM=/usr/lib64/libweston-14
WMAP="drm-backend.so=$MM/drm-backend.so;gl-renderer.so=$MM/gl-renderer.so;color-lcms.so=$MM/color-lcms.so;headless-backend.so=$MM/headless-backend.so;pipewire-backend.so=$MM/pipewire-backend.so;rdp-backend.so=$MM/rdp-backend.so;wayland-backend.so=$MM/wayland-backend.so;x11-backend.so=$MM/x11-backend.so;xwayland.so=$MM/xwayland.so"

fail() {
    echo "FAIL: $*"
    journalctl -u mm-r9-weston --no-pager -n 50 2>/dev/null || true
    tail -50 "$RT/rdp.log" 2>/dev/null || true
    exit 1
}

systemctl stop mm-r9-rdp mm-r9-weston mm-r9-ydotoold \
    mm-r9-carrier-peer-g90 mm-r9-carrier-peer-g91 2>/dev/null || true
systemctl reset-failed mm-r9-rdp mm-r9-weston mm-r9-ydotoold \
    mm-r9-carrier-peer-g90 mm-r9-carrier-peer-g91 2>/dev/null || true
systemctl stop greetd-qdwin greetd qdistro-session-manager 2>/dev/null || true
runuser -u admin -- systemctl --user stop noctalia-session noctalia-shell \
    qdlocker 2>/dev/null || true
systemctl stop seatd.service seatd.socket mm-seatd 2>/dev/null || true
pkill -9 -f sdl-freerdp 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true
pkill -9 -x seatd 2>/dev/null || true
rm -f /run/seatd.sock /run/.ydotool_socket 2>/dev/null || true
sleep 1
systemd-run --collect --unit=mm-seatd seatd
for _ in $(seq 1 30); do [ -S /run/seatd.sock ] && break; sleep 0.2; done
[ -S /run/seatd.sock ] || fail "seatd socket missing"

systemd-run --collect --unit=mm-r9-ydotoold \
    ydotoold --socket-path=/run/.ydotool_socket --socket-perm=0666
for _ in $(seq 1 30); do [ -S /run/.ydotool_socket ] && break; sleep 0.2; done
[ -S /run/.ydotool_socket ] || fail "ydotool socket missing"

rm -rf "$RT"
install -d -m 0700 "$RT"
cat >"$RT/weston.ini" <<EOF
[core]
shell=kiosk-shell.so
idle-time=0
require-input=false
EOF

systemd-run --collect --unit=mm-r9-weston \
  --setenv=XDG_RUNTIME_DIR=$RT --setenv=HOME=/root \
  --setenv=LIBSEAT_BACKEND=seatd --setenv="WESTON_MODULE_MAP=$WMAP" \
  weston --backend=drm-backend.so --renderer=pixman \
    --config="$RT/weston.ini" --socket=$SOCK
for _ in $(seq 1 80); do [ -S "$RT/$SOCK" ] && break; sleep 0.2; done
[ -S "$RT/$SOCK" ] || fail "viewer weston socket missing"

cat >/run/systemd/system/mm-r9-rdp.service <<EOF
[Unit]
After=mm-r9-weston.service
[Service]
Type=simple
Environment=XDG_RUNTIME_DIR=$RT
Environment=HOME=/root
Environment=WAYLAND_DISPLAY=$SOCK
Environment=SDL_VIDEODRIVER=wayland
Environment=SDL_RENDER_DRIVER=software
ExecStart=/usr/bin/sdl-freerdp /v:127.0.0.1:3390 /u:r9 /p:r9 /scale:100 /cert:ignore /gfx:AVC444:off,AVC420:off /size:${W}x${H} /f /log-level:DEBUG
StandardOutput=append:$RT/rdp.log
StandardError=append:$RT/rdp.log
Restart=no
EOF
cat >/run/systemd/system/mm-r9-local-panel.service <<EOF
[Unit]
Description=R9 fixture local-desktop panel owner
[Service]
Type=oneshot
ExecStart=/usr/bin/true
RemainAfterExit=yes
EOF
systemctl daemon-reload
systemctl start mm-r9-local-panel.service
touch "$RT/ready"
echo "R9_VIEWER_READY socket=$RT/$SOCK"
