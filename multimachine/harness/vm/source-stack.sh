#!/bin/bash
# Dedicated headless-qdwin source stack on VM-A (admin uid 1000, where pipewire
# is live) for the decoded-remote capture. Private wayland socket wayland-mm —
# a separate compositor, unaffected by the production session's lock/shell.
# Run as: runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000
#         DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus bash mm-vma-setup.sh
set -uo pipefail   # pipefail surfaces in-pipe failures; cleanup lines are
                   # explicitly tolerated (|| true). The caller treats the final
                   # SETUP_OK token (not exit code alone) as the success signal.
export XDG_RUNTIME_DIR=/run/user/1000
SOCK=wayland-mm
W=${W:-1280}; H=${H:-800}; GEN=${GEN:-20}; RELAY_PORT=${RELAY_PORT:-5555}
RUN() { systemd-run --user --collect "$@"; }

# Clean prior run.
systemctl --user stop mm-qdwin mm-marker mm-bystander mm-relay 2>/dev/null || true
sleep 1
rm -f "$XDG_RUNTIME_DIR/$SOCK" "$XDG_RUNTIME_DIR/$SOCK.lock" 2>/dev/null

# Custom weston.ini with a real pipewire output pool (the per-view capture
# pulls from it). Headless ignores the DRM [output]; pixman renderer.
cat > "$XDG_RUNTIME_DIR/mm-weston.ini" <<EOF
[core]
shell=/usr/lib64/weston/qdwin-shell.so
idle-time=0
[shell]
locking=false
[pipewire]
num-outputs=4
EOF

# 1) Headless qdwin on a private socket. WESTON_MODULE_MAP lets qdwin-shell load
#    pipewire-backend.so internally (the per-view capture pool) — same as prod.
MM="/usr/lib64/libweston-14"
WMAP="drm-backend.so=$MM/drm-backend.so;gl-renderer.so=$MM/gl-renderer.so;color-lcms.so=$MM/color-lcms.so;headless-backend.so=$MM/headless-backend.so;pipewire-backend.so=$MM/pipewire-backend.so;rdp-backend.so=$MM/rdp-backend.so;wayland-backend.so=$MM/wayland-backend.so;x11-backend.so=$MM/x11-backend.so;xwayland.so=$MM/xwayland.so"
RUN --unit=mm-qdwin --setenv=QDWIN_ALLOWED_UID=1000 --setenv=QDWIN_ALLOWED_LOCKER_ANY=1 \
  --setenv="WESTON_MODULE_MAP=$WMAP" \
  weston --backend=headless-backend.so --backend=pipewire-backend.so --renderer=pixman \
    --config="$XDG_RUNTIME_DIR/mm-weston.ini" \
    --width=$W --height=$H --socket=$SOCK >/dev/null 2>&1
for _ in $(seq 1 50); do [ -S "$XDG_RUNTIME_DIR/$SOCK" ] && break; sleep 0.2; done
if [ ! -S "$XDG_RUNTIME_DIR/$SOCK" ]; then echo "FAIL: qdwin socket never appeared"; journalctl --user -u mm-qdwin --no-pager|tail -20; exit 7; fi
echo "qdwin up on $XDG_RUNTIME_DIR/$SOCK"

# 2) Bystander first (so --subscribe last catches the marker's toplevel_added).
rm -f "$XDG_RUNTIME_DIR/bystander.out"
RUN --unit=mm-bystander --setenv=WAYLAND_DISPLAY=$SOCK \
  bash -c "qdwin-bystander --subscribe last > $XDG_RUNTIME_DIR/bystander.out 2>&1"
sleep 1.5

# 3) Marker (source toplevel) WxH, animating.
FS_ARG=""; [ "${FS:-0}" = 1 ] && FS_ARG="--fullscreen"
RUN --unit=mm-marker --setenv=WAYLAND_DISPLAY=$SOCK \
  qdwin-marker-client --width $W --height $H --output-id 1 --generation $GEN --frame 0 --animate-ms 200 $FS_ARG

# 4) Discover the approved RDP port.
RDP_PORT=""
for _ in $(seq 1 50); do
  RDP_PORT=$(grep -m1 '^RDP_PORT=' "$XDG_RUNTIME_DIR/bystander.out" 2>/dev/null | cut -d= -f2 | tr -dc '0-9')
  [ -n "$RDP_PORT" ] && break; sleep 0.3
done
if [ -z "$RDP_PORT" ]; then echo "FAIL: no RDP_PORT approved"; echo "--- bystander.out ---"; cat "$XDG_RUNTIME_DIR/bystander.out"; exit 8; fi

# 5) Fixed-port relay so the SLIRP hostfwd targets a stable port.
RUN --unit=mm-relay socat TCP-LISTEN:$RELAY_PORT,reuseaddr,fork TCP:127.0.0.1:$RDP_PORT
sleep 0.5
echo "--- bystander.out ---"; cat "$XDG_RUNTIME_DIR/bystander.out"
echo "SETUP_OK RDP_PORT=$RDP_PORT RELAY_PORT=$RELAY_PORT"
