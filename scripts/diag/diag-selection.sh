#!/bin/bash
# Diagnostic: spawn weston + qdshell + wl-copy and check whether
# qdwin's selection_signal listener fires.
mkdir -p /home/admin/.config
cat >/home/admin/.config/weston-diag2.ini <<EOF
[core]
shell=qdwin-shell.so
backend=rdp-backend.so
require-outputs=any
idle-time=0
renderer=pixman
[shell]
locking=false
[output]
name=rdp
mode=1280x720
EOF
chown admin:admin /home/admin/.config/weston-diag2.ini
pkill -9 -x weston 2>/dev/null
pkill -9 -f wl-copy 2>/dev/null
pkill -9 -f sdl-freerdp 2>/dev/null
pkill -9 -f qdistro-test-window 2>/dev/null
sleep 1
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    QDWIN_ALLOWED_UID=1000 \
    nohup weston --config=/home/admin/.config/weston-diag2.ini \
        --rdp-tls-cert=/home/admin/qdwin-rdp/rdp.crt \
        --rdp-tls-key=/home/admin/qdwin-rdp/rdp.key \
        --log=/tmp/diag2-weston.log >/dev/null 2>&1 &
WPID=$!
sleep 4
chmod 0666 /run/user/1000/wayland-1 2>/dev/null
runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 60 sdl-freerdp /v:127.0.0.1:3389 /cert:ignore /u:p /p:p \
    >/tmp/diag2-sdl.log 2>&1 &
sleep 3

# Spawn a window so wl-copy has a focus context.
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
    nohup qdistro-test-window --title "diag-source" \
    >/tmp/diag2-win.log 2>&1 &
sleep 2

# Try wl-copy with WAYLAND_DEBUG.
echo -n "diag-payload" | runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 WAYLAND_DEBUG=1 \
    timeout 4 wl-copy --foreground 2>/tmp/diag2-wlc.log || echo "wl-copy exit=$?"
sleep 2
echo "--- wl-copy WAYLAND_DEBUG (last 40) ---"
tail -40 /tmp/diag2-wlc.log

echo "--- weston log: anything qdwin/selection ---"
grep -iE "qdwin|selection" /tmp/diag2-weston.log | head -30
echo
echo "--- wl-copy log ---"
cat /tmp/diag2-win.log
pkill -9 -x weston 2>/dev/null
pkill -9 -f sdl-freerdp 2>/dev/null
pkill -9 -f qdistro-test-window 2>/dev/null
pkill -9 -f wl-copy 2>/dev/null
