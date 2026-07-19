#!/bin/bash
# VM-B decoder stack (5b): minimal weston (DRM backend, own VT) + sdl-freerdp as
# a FULLSCREEN wayland client = the decoded-remote output on VM-B's head.
# Captured host-side via `virsh screenshot` (QMP) — independent of the guest
# agent, so a DRM/VT-induced agent hiccup never blocks the capture.
set -uo pipefail   # pipefail surfaces in-pipe failures; cleanup is tolerated
                   # (|| true). Caller treats the final VMB_SETUP_OK token as success.
RT=/run/mm-b
SOCK=wayland-b
HOST=${HOST:-10.0.2.2}; PORT=${PORT:-5555}; OTP=${OTP:?need OTP}
W=${W:-1280}; H=${H:-800}; TTY=${TTY:-2}
MM=/usr/lib64/libweston-16
WMAP="drm-backend.so=$MM/drm-backend.so;gl-renderer.so=$MM/gl-renderer.so;color-lcms.so=$MM/color-lcms.so;headless-backend.so=$MM/headless-backend.so;pipewire-backend.so=$MM/pipewire-backend.so;rdp-backend.so=$MM/rdp-backend.so;wayland-backend.so=$MM/wayland-backend.so;x11-backend.so=$MM/x11-backend.so;xwayland.so=$MM/xwayland.so"

systemctl stop mm-weston mm-viewer 2>/dev/null || true
systemctl reset-failed mm-weston mm-viewer 2>/dev/null || true   # allow unit reuse on relaunch
pkill -f sdl-freerdp 2>/dev/null || true

# Free DRM + the seat from the production qdwin session (session-3 foot-gun): a
# STANDARD spun VM runs greetd-qdwin + the admin noctalia session holding DRM
# master, plus a system `seatd -g seat` that REJECTS root. Stop them all (tolerant).
systemctl stop greetd-qdwin greetd qdistro-session-manager 2>/dev/null || true
runuser -u admin -- systemctl --user stop noctalia-session noctalia-shell qdlocker 2>/dev/null || true

# seatd: ALWAYS (re)start our own unrestricted instance, idempotently (session-4
# parity with viewer-stack.sh). The production `seatd.service` runs `seatd -g seat`
# (rejects root → libseat "Broken pipe") with **Restart=always RestartSec=1**, so a
# bare `pkill` just respawns it and it re-owns /run/seatd.sock, racing our weston on
# a RELAUNCH. So STOP THE UNIT (overrides Restart=always) — not just the process —
# then drop the socket and start fresh. Clean-stop our own prior mm-seatd +
# reset-failed so the unit name is reusable.
systemctl stop seatd.service seatd.socket 2>/dev/null || true   # production seatd -g seat (Restart=always)
systemctl stop mm-seatd 2>/dev/null || true
pkill -x seatd 2>/dev/null || true
rm -f /run/seatd.sock 2>/dev/null || true
systemctl reset-failed mm-seatd 2>/dev/null || true
sleep 1
systemd-run --collect --unit=mm-seatd seatd
for _ in $(seq 1 30); do [ -S /run/seatd.sock ] && break; sleep 0.2; done
[ -S /run/seatd.sock ] && echo "seatd up" || { echo "FAIL: seatd socket missing"; journalctl -u mm-seatd --no-pager|tail -10; exit 6; }
rm -rf "$RT"; mkdir -p "$RT"; chmod 0700 "$RT"

# Minimal weston.ini: a single DRM output at WxH, no panel/background chrome.
# kiosk-shell places every toplevel FULLSCREEN at the output origin (0,0) with no
# centering/decoration — so the decoded RDP surface lands 1:1 at (0,0), no -12px
# placement offset (desktop-shell centered it and clipped the marker quiet zone).
cat > "$RT/weston.ini" <<EOF
[core]
shell=kiosk-shell.so
idle-time=0
require-input=false
EOF

# 1) weston on the DRM head, own VT, pixman (no GL deps on virtio-gpu).
systemd-run --collect --unit=mm-weston \
  --setenv=XDG_RUNTIME_DIR=$RT --setenv=HOME=/root \
  --setenv=LIBSEAT_BACKEND=seatd \
  --setenv="WESTON_MODULE_MAP=$WMAP" \
  weston --backend=drm-backend.so --renderer=pixman \
    --config="$RT/weston.ini" --socket=$SOCK
for _ in $(seq 1 60); do [ -S "$RT/$SOCK" ] && break; sleep 0.2; done
if [ ! -S "$RT/$SOCK" ]; then echo "FAIL: weston socket never appeared"; journalctl -u mm-weston --no-pager|tail -25; exit 7; fi
echo "weston up on $RT/$SOCK"

# 2) sdl-freerdp as a fullscreen wayland client (no client scaling).
systemd-run --collect --unit=mm-viewer \
  --setenv=XDG_RUNTIME_DIR=$RT --setenv=HOME=/root \
  --setenv=WAYLAND_DISPLAY=$SOCK --setenv=SDL_VIDEODRIVER=wayland \
  bash -c "sdl-freerdp /v:$HOST:$PORT /u:mm /p:\"$OTP\" /scale:100 /cert:ignore /size:${W}x${H} /f > $RT/viewer.log 2>&1"

# 3) Wait for the decoded channels to come up.
for _ in $(seq 1 40); do
  grep -q 'Loading Dynamic Virtual Channel rdpgfx' "$RT/viewer.log" 2>/dev/null && break
  systemctl is-active mm-viewer >/dev/null 2>&1 || { echo "FAIL: viewer exited early"; tail -20 "$RT/viewer.log"; exit 8; }
  sleep 0.3
done
sleep 2
echo "--- viewer.log tail ---"; tail -6 "$RT/viewer.log"
echo "VMB_SETUP_OK socket=$RT/$SOCK"
