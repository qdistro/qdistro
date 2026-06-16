#!/bin/bash
# VM-B managed-viewer stack (Phase-1 scenario-2, codex impl-9 Q2): seatd + the
# proven kiosk-shell weston (DRM head, own VT) + the REAL `mm-viewer-launch`
# (python3 -m multimachine.viewer) as a managed Wayland client. The viewer
# connects to the host-served JSON-lines control side-channel, and on Announce
# launches `sdl-freerdp` FULLSCREEN itself — so the captured surface IS the
# viewer-managed toplevel (not decoder-stack.sh's hardcoded sdl-freerdp). Captured
# host-side via `virsh screenshot` (QMP), identical geometry to decoder-stack.sh.
#
# Differs from decoder-stack.sh ONLY in step 2: mm-viewer-launch replaces the bare
# sdl-freerdp. Weston/seatd/kiosk setup is byte-for-byte the proven recipe.
set -uo pipefail   # pipefail surfaces in-pipe failures; cleanup is tolerated
                   # (|| true). Caller treats the final VMB_VIEWER_OK token as success.
RT=/run/mm-b
SOCK=wayland-b
CONTROL_HOST=${CONTROL_HOST:-10.0.2.2}; CONTROL_PORT=${CONTROL_PORT:-5556}
RDP_HOST=${RDP_HOST:-10.0.2.2}; RDP_PORT=${RDP_PORT:-5555}
GEN=${GEN:?need GEN}; OTP=${OTP:?need OTP}
W=${W:-1280}; H=${H:-800}; RDP_USER=${RDP_USER:-mm}
MMDIR=${MMDIR:-/tmp/mm}                         # PYTHONPATH holding multimachine/
STATUS_FILE=${STATUS_FILE:-$RT/viewer-status.json}
MM=/usr/lib64/libweston-14
WMAP="drm-backend.so=$MM/drm-backend.so;gl-renderer.so=$MM/gl-renderer.so;color-lcms.so=$MM/color-lcms.so;headless-backend.so=$MM/headless-backend.so;pipewire-backend.so=$MM/pipewire-backend.so;rdp-backend.so=$MM/rdp-backend.so;wayland-backend.so=$MM/wayland-backend.so;x11-backend.so=$MM/x11-backend.so;xwayland.so=$MM/xwayland.so"

systemctl stop mm-weston mm-viewer 2>/dev/null || true
pkill -f sdl-freerdp 2>/dev/null || true
pkill -f multimachine.viewer 2>/dev/null || true
sleep 1

# seatd (same as decoder-stack): the seat weston's drm backend needs with no
# logind graphical session.
if [ ! -S /run/seatd.sock ]; then
  systemd-run --collect --unit=mm-seatd seatd
  for _ in $(seq 1 30); do [ -S /run/seatd.sock ] && break; sleep 0.2; done
fi
[ -S /run/seatd.sock ] && echo "seatd up" || { echo "FAIL: seatd socket missing"; exit 6; }
rm -rf "$RT"; mkdir -p "$RT"; chmod 0700 "$RT"

cat > "$RT/weston.ini" <<EOF
[core]
shell=kiosk-shell.so
idle-time=0
require-input=false
EOF

# 1) weston on the DRM head, own VT, pixman (proven recipe).
systemd-run --collect --unit=mm-weston \
  --setenv=XDG_RUNTIME_DIR=$RT --setenv=HOME=/root \
  --setenv=LIBSEAT_BACKEND=seatd \
  --setenv="WESTON_MODULE_MAP=$WMAP" \
  weston --backend=drm-backend.so --renderer=pixman \
    --config="$RT/weston.ini" --socket=$SOCK
for _ in $(seq 1 60); do [ -S "$RT/$SOCK" ] && break; sleep 0.2; done
if [ ! -S "$RT/$SOCK" ]; then echo "FAIL: weston socket never appeared"; journalctl -u mm-weston --no-pager|tail -25; exit 7; fi
echo "weston up on $RT/$SOCK"

# 2) the REAL mm-viewer-launch as a managed Wayland client. It blocks reading the
#    control side-channel; on Announce it launches sdl-freerdp /f itself. SDL env
#    is inherited by that child so it maps onto the kiosk weston as a fullscreen
#    toplevel at origin 0,0.
command -v python3 >/dev/null || { echo "FAIL: python3 missing on VM-B"; exit 5; }
[ -f "$MMDIR/multimachine/viewer.py" ] || { echo "FAIL: multimachine pkg not at $MMDIR"; exit 5; }
systemd-run --collect --unit=mm-viewer \
  --setenv=XDG_RUNTIME_DIR=$RT --setenv=HOME=/root \
  --setenv=WAYLAND_DISPLAY=$SOCK --setenv=SDL_VIDEODRIVER=wayland \
  --setenv=PYTHONPATH=$MMDIR \
  python3 -m multimachine.viewer \
    --control-host "$CONTROL_HOST" --control-port "$CONTROL_PORT" \
    --rdp-host "$RDP_HOST" --rdp-port "$RDP_PORT" \
    --generation "$GEN" --otp "$OTP" --size "${W}x${H}" \
    --fullscreen --rdp-user "$RDP_USER" \
    --status-file "$STATUS_FILE" --decoder-log "$RT/freerdp.log"

# 3) Confirm the viewer process is up + connected to the control channel. It does
#    NOT decode until the host sends Announce, so we only assert the unit is live
#    and has written its initial status (idle/connected).
for _ in $(seq 1 40); do
  systemctl is-active mm-viewer >/dev/null 2>&1 || { echo "FAIL: mm-viewer exited early"; journalctl -u mm-viewer --no-pager|tail -25; exit 8; }
  [ -f "$STATUS_FILE" ] && break
  sleep 0.3
done
[ -f "$STATUS_FILE" ] || { echo "FAIL: viewer wrote no status file"; journalctl -u mm-viewer --no-pager|tail -25; exit 8; }
echo "--- viewer status ---"; cat "$STATUS_FILE"
echo "VMB_VIEWER_OK socket=$RT/$SOCK status=$STATUS_FILE"
