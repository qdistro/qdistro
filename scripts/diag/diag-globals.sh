#!/bin/bash
# Diagnostic: confirm wl_data_device_manager + primary_selection are
# advertised by qdwin-shell.so. Boots a minimal weston, queries
# wayland-info, prints + tears down.
mkdir -p /home/admin/.config
cat >/home/admin/.config/weston-diag.ini <<EOF
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
chown admin:admin /home/admin/.config/weston-diag.ini
pkill -9 -x weston 2>/dev/null
sleep 1
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    QDWIN_ALLOWED_UID=1000 \
    nohup weston --config=/home/admin/.config/weston-diag.ini \
        --rdp-tls-cert=/home/admin/qdwin-rdp/rdp.crt \
        --rdp-tls-key=/home/admin/qdwin-rdp/rdp.key \
        --log=/tmp/diag-weston.log >/dev/null 2>&1 &
sleep 4
chmod 0666 /run/user/1000/wayland-1 2>/dev/null
echo "--- wayland-info clipboard globals ---"
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
    timeout 5 wayland-info 2>&1 | grep -iE "data_device|primary_selection|qdwin_shell|security_context" | head
echo "--- weston log lines: selection/data ---"
grep -iE "data_device|selection|wl_seat" /tmp/diag-weston.log | head -10
pkill -9 -x weston 2>/dev/null
