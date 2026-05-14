#!/bin/bash
# Install the admin-user systemd session that runs qdwin + qdshell.
#
# Two user units land in /home/admin/.config/systemd/user/:
#
#   noctalia-session.service  — weston with qdwin-shell.so on the drm
#                               backend (+ pipewire sub-backend for
#                               §6.5 view_stream outputs), claiming
#                               wayland-1.
#   noctalia-shell.service    — quickshell loading the qdshell QML stack
#                               from /usr/share/quickshell/qdshell/.
#
# The unit names are kept as noctalia-* for compatibility with the
# qdwin-noctalia GUI test harness (tests/integration/qdwin-noctalia/
# noctalia-helpers.sh greps these specific names).
#
# Args:
#   $1 — path to qdshell source tree (default /root/qdistro-src/qdshell)
#
# Side effects:
#   - admin added to video / input / render groups (needed for drm
#     backend + libinput).
#   - admin lingering enabled so the user manager runs after
#     autologin completes.
#   - /home/admin/weston.ini written with qdwin-shell.so + drm backend
#     + pipewire sub-backend (num-outputs=2).
#   - qdshell QML copied to /usr/share/quickshell/qdshell/ (system-
#     wide so multiple users could share).
#   - qdshell's compiled QML plugin (libqdistro-qdwin.so + qmldir)
#     copied to /usr/share/qdistro/qml/Qdistro/Qdwin/. Caller is
#     expected to have run `meson compile -C build` under qdshell/
#     so qdshell/build/qml-plugin/libqdistro-qdwin.so exists. Override
#     the source location via $QDSHELL_PLUGIN_BUILD if needed.
#   - Both user units enabled (but not started — caller decides when
#     to start them, typically at next greetd tty3 login).

set -eu

QDSHELL_SRC=${1:-/root/qdistro-src/qdshell}

if [ ! -d "$QDSHELL_SRC" ]; then
    echo "ERROR: qdshell source not found at $QDSHELL_SRC" >&2
    echo "       pass the qdshell/ dir as \$1 or untar qdshell to /root/qdistro-src/qdshell/" >&2
    exit 2
fi

# 1. Groups + linger.
usermod -aG video,input,render admin
loginctl enable-linger admin

# 2. weston.ini: qdwin-shell.so + drm backend so SPICE sees the
# framebuffer in a VM.
#
# Path resolution: qdwin was built with `meson setup build` (no
# --prefix) which defaults to /usr/local. fresh-vm-bootstrap.sh
# passes --prefix=/usr so qdwin-shell.so lands at /usr/lib64/weston/.
# If you're hand-building elsewhere, adjust this path to match
# `meson install --dry-run` output.
#
# renderer=pixman is critical for virtio-gpu VMs without accel3d —
# the GL renderer segfaults on a software-only virtio-gpu. Drop the
# line on hardware with a real GPU to get hardware acceleration.
#
# The `[pipewire]` section enables the pipewire sub-backend so qdwin's
# §6.5 view_stream forwarding (qdwin_shell_v1.subscribe_view_stream)
# has free pipewire outputs to pin views onto. Pair with the
# `--backend=drm-backend.so,pipewire-backend.so` cmdline in the
# noctalia-session unit below. num-outputs=2 gives headroom for
# concurrent forwards.
cat > /home/admin/weston.ini <<'EOF'
[core]
shell=/usr/lib64/weston/qdwin-shell.so
renderer=pixman
modules=

[shell]
locking=false
client=

[output]
name=Virtual-1
mode=1920x1080@60

[pipewire]
num-outputs=2
EOF
chown admin:users /home/admin/weston.ini

# 3. System-wide qdshell QML at /usr/share/quickshell/qdshell/.
install -d -o root -g root -m 0755 /usr/share/quickshell
rm -rf /usr/share/quickshell/qdshell
cp -r "$QDSHELL_SRC" /usr/share/quickshell/qdshell
chown -R root:root /usr/share/quickshell/qdshell

# 3b. Qdistro.Qdwin QML plugin — qdshell's native binding to
# qdwin_shell_v1. Built from qdshell/qml-plugin/ (which reads the
# protocol XML from the qdwin sibling repo at build time). Without
# this, qdshell's Services/Qdwin/Qdwin.qml cannot resolve
# `import Qdistro.Qdwin 1.0` and falls back to the no-binding stubs.
QDSHELL_PLUGIN_BUILD="${QDSHELL_PLUGIN_BUILD:-$QDSHELL_SRC/build}"
if [ -f "$QDSHELL_PLUGIN_BUILD/qml-plugin/libqdistro-qdwin.so" ]; then
    install -d -o root -g root -m 0755 /usr/share/qdistro/qml/Qdistro/Qdwin
    install -m 0755 -o root -g root \
        "$QDSHELL_PLUGIN_BUILD/qml-plugin/libqdistro-qdwin.so" \
        /usr/share/qdistro/qml/Qdistro/Qdwin/libqdistro-qdwin.so
    install -m 0644 -o root -g root \
        "$QDSHELL_SRC/qml-plugin/qmldir" \
        /usr/share/qdistro/qml/Qdistro/Qdwin/qmldir
    echo "qdwin-shell-v1 QML plugin installed: $(ls -la /usr/share/qdistro/qml/Qdistro/Qdwin/libqdistro-qdwin.so | awk '{print $5}') bytes"
else
    echo "WARN: $QDSHELL_PLUGIN_BUILD/qml-plugin/libqdistro-qdwin.so not found —" \
         "qdshell will run without qdwin_shell_v1 binding (Qdwin.qml" \
         "import will fail). Rebuild qdshell with 'meson setup build &&" \
         "meson compile -C build' in $QDSHELL_SRC, then re-run this script."
fi

# 4. User systemd units.
install -d -o admin -g users -m 0755 /home/admin/.config/systemd/user

cat > /home/admin/.config/systemd/user/noctalia-session.service <<'EOF'
[Unit]
Description=qdwin compositor session (libweston + qdwin-shell.so)
After=graphical.target

[Service]
Type=simple
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=WAYLAND_DISPLAY=wayland-1
ExecStart=/usr/bin/weston --backend=drm-backend.so,pipewire-backend.so --config=%h/weston.ini --socket=wayland-1
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

cat > /home/admin/.config/systemd/user/noctalia-shell.service <<'EOF'
[Unit]
Description=qdshell QML on top of qdwin
After=noctalia-session.service
Requires=noctalia-session.service

[Service]
Type=simple
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=WAYLAND_DISPLAY=wayland-1
Environment=QML_DISABLE_DISK_CACHE=1
# Tell qs to look in /usr/share/qdistro/qml for the
# Qdistro.Qdwin QML plugin (libqdistro-qdwin.so installed in step 3b).
# Without this, qdshell's Services/Qdwin/Qdwin.qml cannot resolve
# `import Qdistro.Qdwin 1.0` and the qdwin_shell_v1 binding stays
# unbound.
Environment=QML_IMPORT_PATH=/usr/share/qdistro/qml
ExecStartPre=/bin/sh -c 'while [ ! -e $XDG_RUNTIME_DIR/wayland-1 ]; do sleep 0.5; done'
ExecStart=/usr/bin/dbus-run-session -- /usr/bin/qs -p /usr/share/quickshell/qdshell
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

chown -R admin:users /home/admin/.config/systemd

# 5. Enable (but don't start — caller decides).
runuser -l admin -c 'systemctl --user enable noctalia-session.service noctalia-shell.service' \
    2>&1 || echo "WARN: enable failed (admin user manager not running yet?)"

echo "qdwin session installed."
echo "  start now:    runuser -l admin -c 'systemctl --user start noctalia-shell.service'"
echo "  start at boot: systemctl --user --machine=admin@ start noctalia-shell.service  (after reboot/relogin)"
