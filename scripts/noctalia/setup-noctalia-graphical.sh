#!/bin/bash
# Set up the graphical Noctalia visual-test session on a VM:
# - admin added to video + input groups
# - weston.ini switched to drm-backend so the VM console shows real output
# - systemd unit auto-launches `weston + qs noctalia` on graphical.target
# - lingering enabled so user services run after autologin completes
#
# Idempotent — safe to re-run.
set -e

# 1. groups
usermod -aG video,input,render admin
loginctl enable-linger admin

# 2. weston.ini switch
cat > /home/admin/weston.ini << 'EOF'
[core]
shell=/usr/lib64/weston/qdwin-shell.so
modules=

[shell]
locking=false
client=

# Use the drm backend so qdwin renders to virtio-gpu and exposes a
# framebuffer. seat0/tty1 pinned so systemd doesn't fight us.
[output]
name=Virtual-1
mode=1920x1080@60
EOF
chown admin:admin /home/admin/weston.ini

# 3. systemd user unit for weston + noctalia
mkdir -p /home/admin/.config/systemd/user
cat > /home/admin/.config/systemd/user/noctalia-session.service << 'EOF'
[Unit]
Description=qdwin + Noctalia visual-test session
After=graphical.target

[Service]
Type=simple
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=WAYLAND_DISPLAY=wayland-1
ExecStart=/usr/bin/weston --backend=drm-backend.so --config=%h/weston.ini --socket=wayland-1 --tty=2
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

cat > /home/admin/.config/systemd/user/noctalia-shell.service << 'EOF'
[Unit]
Description=Noctalia QML shell on top of qdwin
After=noctalia-session.service
Requires=noctalia-session.service

[Service]
Type=simple
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=WAYLAND_DISPLAY=wayland-1
Environment=QML_DISABLE_DISK_CACHE=1
ExecStartPre=/bin/sh -c 'while [ ! -e $XDG_RUNTIME_DIR/wayland-1 ]; do sleep 0.5; done'
ExecStart=/usr/bin/dbus-run-session -- /usr/bin/qs -p /usr/share/quickshell/noctalia-shell
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
chown -R admin:admin /home/admin/.config

# 4. Enable
runuser -l admin -c 'systemctl --user enable noctalia-session.service noctalia-shell.service' 2>&1
echo ""
echo "=== setup done; now start with: ==="
echo "runuser -l admin -c 'systemctl --user start noctalia-shell.service'"
echo "or reboot."
