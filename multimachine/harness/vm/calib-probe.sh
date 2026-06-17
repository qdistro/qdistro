#!/bin/bash
# VM-B coordinate-CALIBRATION probe (A2, codex impl-21). Brings up the SAME proven
# kiosk-shell weston (DRM head, own VT, pixman) + ydotoold that viewer-stack.sh uses,
# but launches a FULLSCREEN `qdwin-marker-client` with per-seat input telemetry
# INSTEAD of sdl-freerdp. The harness injects `ydotool mousemove --absolute` at known
# viewer pixels and reads this probe's received coords = T_apparatus(p) — the ydotool
# -> uinput -> kiosk-weston-pointer apparatus map, measured INDEPENDENTLY of the
# qdistro-forward / RDP / source path (which are not in the data path here). That is
# what lets the product phase assert an ABSOLUTE landing pixel rather than
# faithful-linear-up-to-a-uniform-scale.
#
# Geometry identity is by construction: this is viewer-stack.sh's byte-for-byte
# weston/seatd/ydotoold recipe (same DRM head, same WxH, same kiosk-shell), so the
# kiosk pointer apparatus measured here is the SAME one the product phase rides. The
# probe MUST be torn down before the product phase's sdl-freerdp maps (phase
# isolation — they must not both compete for fullscreen/focus).
set -uo pipefail
RT=/run/mm-b
SOCK=wayland-b
W=${W:-1280}; H=${H:-800}
GEN=${GEN:-1}; ANIMATE_MS=${ANIMATE_MS:-200}
TELEMETRY=${TELEMETRY:-$RT/calib-probe.json}
LABEL=${LABEL:-calib}
MM=/usr/lib64/libweston-14
WMAP="drm-backend.so=$MM/drm-backend.so;gl-renderer.so=$MM/gl-renderer.so;color-lcms.so=$MM/color-lcms.so;headless-backend.so=$MM/headless-backend.so;pipewire-backend.so=$MM/pipewire-backend.so;rdp-backend.so=$MM/rdp-backend.so;wayland-backend.so=$MM/wayland-backend.so;x11-backend.so=$MM/x11-backend.so;xwayland.so=$MM/xwayland.so"

systemctl stop mm-weston mm-viewer mm-calib 2>/dev/null || true
systemctl reset-failed mm-weston mm-viewer mm-calib 2>/dev/null || true
pkill -f sdl-freerdp 2>/dev/null || true
pkill -f multimachine.viewer 2>/dev/null || true

# Free DRM + seat from the production qdwin session (same as viewer-stack.sh).
systemctl stop greetd-qdwin greetd qdistro-session-manager 2>/dev/null || true
runuser -u admin -- systemctl --user stop noctalia-session noctalia-shell qdlocker 2>/dev/null || true

# seatd: stop the production unit (Restart=always) + start our own unrestricted one.
systemctl stop seatd.service seatd.socket 2>/dev/null || true
systemctl stop mm-seatd 2>/dev/null || true
pkill -x seatd 2>/dev/null || true
rm -f /run/seatd.sock 2>/dev/null || true
systemctl reset-failed mm-seatd 2>/dev/null || true
sleep 1
systemd-run --collect --unit=mm-seatd seatd
for _ in $(seq 1 30); do [ -S /run/seatd.sock ] && break; sleep 0.2; done
[ -S /run/seatd.sock ] && echo "seatd up" || { echo "FAIL: seatd socket missing"; journalctl -u mm-seatd --no-pager|tail -10; exit 6; }

# ydotoold BEFORE weston (so weston enumerates the uinput device at startup) — the
# SAME apparatus the product phase injects through.
YDSOCK=/run/.ydotool_socket
systemctl stop mm-ydotoold 2>/dev/null || true
systemctl reset-failed mm-ydotoold 2>/dev/null || true
systemd-run --collect --unit=mm-ydotoold ydotoold --socket-path=$YDSOCK --socket-perm=0666
for _ in $(seq 1 30); do [ -S "$YDSOCK" ] && break; sleep 0.2; done
[ -S "$YDSOCK" ] && echo "ydotoold up ($YDSOCK)" || echo "WARN: ydotoold socket missing"
sleep 1

rm -rf "$RT"; mkdir -p "$RT"; chmod 0700 "$RT"
cat > "$RT/weston.ini" <<EOF
[core]
shell=kiosk-shell.so
idle-time=0
require-input=false
EOF

# kiosk weston on the DRM head (same recipe/geometry as viewer-stack.sh).
systemd-run --collect --unit=mm-weston \
  --setenv=XDG_RUNTIME_DIR=$RT --setenv=HOME=/root \
  --setenv=LIBSEAT_BACKEND=seatd \
  --setenv="WESTON_MODULE_MAP=$WMAP" \
  weston --backend=drm-backend.so --renderer=pixman \
    --config="$RT/weston.ini" --socket=$SOCK
for _ in $(seq 1 60); do [ -S "$RT/$SOCK" ] && break; sleep 0.2; done
if [ ! -S "$RT/$SOCK" ]; then echo "FAIL: weston socket never appeared"; journalctl -u mm-weston --no-pager|tail -25; exit 7; fi
echo "weston up on $RT/$SOCK"

# Fullscreen marker probe with telemetry — the independent apparatus observer.
systemctl stop mm-calib 2>/dev/null || true
systemctl reset-failed mm-calib 2>/dev/null || true
rm -f "$TELEMETRY" "$TELEMETRY.tmp"
systemd-run --collect --unit=mm-calib \
  --setenv=XDG_RUNTIME_DIR=$RT --setenv=HOME=/root --setenv=WAYLAND_DISPLAY=$SOCK \
  qdwin-marker-client --width $W --height $H --output-id 1 --generation $GEN \
    --frame 0 --animate-ms $ANIMATE_MS --fullscreen --telemetry $TELEMETRY --label $LABEL
# Wait until the probe has mapped + bound a seat (fail-closed on a probe that never
# came up). The kiosk seat exposes a POINTER capability only after the ydotool uinput
# device emits its first event, so warm it up with a throwaway absolute move (the
# harness baselines motion before each real read, so this can't corrupt a sample),
# then confirm the marker now sees a pointer that has actually delivered motion.
export YDOTOOL_SOCKET=$YDSOCK
for _ in $(seq 1 40); do
  systemctl is-active mm-calib >/dev/null 2>&1 || { echo "FAIL: mm-calib exited early"; journalctl -u mm-calib --no-pager|tail -15; exit 8; }
  ydotool mousemove --absolute -x 200 -y 200 2>/dev/null || true
  sleep 0.3
  if grep -q '"has_pointer":1' "$TELEMETRY" 2>/dev/null \
     && grep -q '"pointer_motion":[1-9]' "$TELEMETRY" 2>/dev/null; then break; fi
done
if ! { grep -q '"has_pointer":1' "$TELEMETRY" 2>/dev/null \
       && grep -q '"pointer_motion":[1-9]' "$TELEMETRY" 2>/dev/null; }; then
  echo "FAIL: calib probe never saw injected pointer motion"; echo "--- telemetry ---"; cat "$TELEMETRY" 2>/dev/null; exit 8
fi
echo "--- calib telemetry ---"; cat "$TELEMETRY" 2>/dev/null
echo "CALIB_OK telemetry=$TELEMETRY socket=$RT/$SOCK"
