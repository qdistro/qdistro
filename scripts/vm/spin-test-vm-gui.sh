#!/bin/bash
# spin-test-vm-gui.sh — like spin-test-vm.sh, plus the post-bootstrap
# layer that the permissions-gui scenarios depend on:
#
#   - work (uid 2000) + work2 (uid 3000) users with linger.
#   - /usr/local/bin/qdistro-test-permission helper.
#   - /usr/local/bin/qdistro-start-admin-app and -tui launchers.
#   - PyQt admin app source under /home/admin/qdistro/admin_app/.
#   - Textual admin TUI source under /home/admin/qdistro/tui/.
#   - qterminal.ini pinned to 1200×700 so TUI scenarios don't truncate.
#   - The admin user's qdwin/qdshell session started so DISPLAY=:0
#     XWayland is reachable for `vm-gui screenshot` / `xdotool`.
#
# fresh-vm-bootstrap.sh masks greetd (greetd would grab the DRM seat
# and prevent admin's user manager from starting). This script keeps
# greetd masked and starts the noctalia-shell user unit directly via
# admin's systemd user manager, which is the documented manual
# follow-up to fresh-vm-bootstrap.sh.
#
# Usage:
#   QDISTRO_VM_PASSWORD=<pw> scripts/vm/spin-test-vm-gui.sh [<prefix>]
#
# Output (last line): the VM name, suitable for piping into
# `tests/integration/permissions-gui/` runner agents as VMNAME.

set -euo pipefail

: "${QDISTRO_VM_PASSWORD:?must export QDISTRO_VM_PASSWORD}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${1:-qd-gui}"

echo "[gui-spin] step 1/3: spin-test-vm.sh $PREFIX (broker layer)" >&2
# spin-test-vm prints the VM name on the last stdout line. Capture
# stderr to console, stdout to a variable.
VM=$(QDISTRO_VM_PASSWORD="$QDISTRO_VM_PASSWORD" \
     bash "$SCRIPT_DIR/spin-test-vm.sh" "$PREFIX" 2>&1 | tee /dev/stderr | tail -1)

if ! echo "$VM" | grep -qE "^${PREFIX}-[0-9]+-[0-9]+$"; then
    echo "[gui-spin] could not parse VM name from spin-test-vm.sh output: '$VM'" >&2
    exit 1
fi
echo "[gui-spin] broker VM ready: $VM" >&2

VMEXEC="$SCRIPT_DIR/vm-exec"

echo "[gui-spin] step 2/3: layering permissions-gui prereqs on $VM" >&2

# Base64 the whole post-bootstrap so embedded quotes / heredocs don't
# trip vm-exec's JSON quoting.
B64=$(base64 -w0 <<'POSTBOOT'
set -eu

SRC=/root/qdistro-src/qdistro

# 1. work + work2 users (uids 2000 / 3000), linger so their session
#    buses come up.
if ! id work >/dev/null 2>&1; then
    useradd -m -u 2000 -U -s /bin/bash work
fi
if ! id work2 >/dev/null 2>&1; then
    useradd -m -u 3000 -U -s /bin/bash work2
fi
loginctl enable-linger work
loginctl enable-linger work2

# Set the test password so scenarios that need work to authenticate
# (e.g. via polkit) can. Match what bake-baseweed sets for admin.
echo "work:${QDISTRO_VM_PASSWORD:-kruger}" | chpasswd
echo "work2:${QDISTRO_VM_PASSWORD:-kruger}" | chpasswd

# 2. qdistro-test-permission helper.
install -m 0755 "$SRC/tests/unit/test_permission.py" \
                /usr/local/bin/qdistro-test-permission

# 3. start-admin-app / start-admin-tui launchers.
install -m 0755 "$SRC/deploy/start-admin-app.sh" \
                /usr/local/bin/qdistro-start-admin-app
install -m 0755 "$SRC/deploy/start-admin-tui.sh" \
                /usr/local/bin/qdistro-start-admin-tui
install -m 0755 "$SRC/deploy/start-user-app.sh" \
                /usr/local/bin/qdistro-start-user-app
install -m 0755 "$SRC/cli/qdistro_approvals.py" \
                /usr/local/sbin/qdistro-approvals
install -d -o root -g root -m 0755 /usr/local/lib/qdistro/stubs
install -m 0644 "$SRC/user_relay/qdistro_user_relay.py" \
                /usr/local/lib/qdistro/qdistro_user_relay.py
install -m 0644 "$SRC/user_relay/org.qdistro.UserRelay.conf" \
                /etc/dbus-1/system.d/org.qdistro.UserRelay.conf
install -m 0644 "$SRC/user_relay/qdistro-user-relay@.service" \
                /etc/systemd/system/qdistro-user-relay@.service
install -d -o root -g root -m 0755 /etc/systemd/user
install -m 0644 "$SRC/user_relay/qdistro-user-relay.service" \
                /etc/systemd/user/qdistro-user-relay.service
install -m 0644 "$SRC/stubs/qstub_notepad.py" \
                /usr/local/lib/qdistro/stubs/qstub_notepad.py
install -m 0644 "$SRC/stubs/qstub-notepad.service" \
                /etc/systemd/user/qstub-notepad.service
systemctl reload dbus-broker.service 2>/dev/null \
    || systemctl reload dbus.service 2>/dev/null \
    || true
systemctl daemon-reload

# 4. PyQt admin app and Textual TUI sources under /home/admin/qdistro/.
install -d -o admin -g users -m 0755 /home/admin/qdistro
install -d -o admin -g users -m 0755 /home/admin/qdistro/admin_app
install -m 0644 -o admin -g users \
        "$SRC/admin_app/qdistro_admin_app.py" \
        /home/admin/qdistro/admin_app/qdistro_admin_app.py
install -d -o admin -g users -m 0755 /home/admin/qdistro/tui
# Explicit file list — adding a new module to tui/ requires updating this
# script. Globbing was previously used but masked missing modules silently
# (nullglob would have installed nothing); naming each file makes the
# install loudly fail if a source file goes missing.
for _tui_src in __init__.py broker_client.py silo_colors.py qdistro_admin_tui.py; do
    install -m 0644 -o admin -g users \
            "$SRC/tui/$_tui_src" \
            "/home/admin/qdistro/tui/$_tui_src"
done
# Wrapper hard-codes the install location instead of using getent — the
# script always installs to /home/admin/qdistro/tui/ in this VM, and the
# getent dance only obscured the contract.
cat > /usr/local/bin/qdistro-admin-tui <<'TUIWRAP'
#!/bin/bash
TUI_DIR=/home/admin/qdistro/tui
export PYTHONPATH="$TUI_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec python3 "$TUI_DIR/qdistro_admin_tui.py" "$@"
TUIWRAP
chmod 0755 /usr/local/bin/qdistro-admin-tui

# 5. qterminal.ini geometry pinned to 1200×700 — AGENTS.md  notes
#    this is required so the TUI's header subtitle and full footer
#    fit without truncation.
install -d -o admin -g users -m 0755 /home/admin/.config/qterminal.org
cat > /home/admin/.config/qterminal.org/qterminal.ini <<'INI'
[MainWindow]
size=@Size(1200 700)
INI
chown admin:users /home/admin/.config/qterminal.org/qterminal.ini

# 6. Install labwc + lxqt + XWayland + qterminal + xdotool + fonts.
#    The permissions-gui scenarios assume `labwc + lxqt on tty3 as
#    user admin, autologged via greetd` (AGENTS.md), not the
#    qdwin-shell session that install-qdwin-session-for-vm.sh wires
#    up. We layer labwc on top — the qdwin user units stay enabled
#    but inactive (they bind wayland-1; labwc binds wayland-0).
#
#    Discovered installs (each missing on baseweed-baked, learned
#    the hard way when labwc errored out):
#      - labwc, lxqt-session, lxqt-labwc-session (config files)
#      - xwayland (provides /usr/bin/Xwayland)
#      - dejavu-fonts noto-sans-fonts (labwc aborts on no fonts)
zypper -n install labwc lxqt-session lxqt-labwc-session \
    qterminal xdotool xhost xwayland git \
    python313-rich python313-textual python313-mistune \
    dejavu-fonts google-noto-sans-fonts \
    perl-Net-DBus \
    >/dev/null 2>&1 || \
    echo "[gui-spin] WARN: zypper install of GUI stack failed"
fc-cache -f >/dev/null 2>&1 || true

# Install the qdistro labwc-startup override + session wrapper.
install -m 0755 "$SRC/deploy/qdistro-startlxqtwayland.sh" \
                /usr/local/bin/startlxqtwayland
install -m 0755 "$SRC/deploy/qdistro-lxqt-session-wrap.sh" \
                /usr/local/bin/qdistro-lxqt-session-wrap

# 7. Env-setup wrapper. Greetd's initial_session doesn't run a
#    full PAM stack, so XDG_RUNTIME_DIR is unset; labwc also wants
#    WLR_RENDERER_ALLOW_SOFTWARE in software-only VMs.
cat > /usr/local/bin/greetd-labwc-wrap <<'WRAP'
#!/bin/bash
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
mkdir -p "$XDG_RUNTIME_DIR" 2>/dev/null
chmod 0700 "$XDG_RUNTIME_DIR" 2>/dev/null
export XDG_SESSION_TYPE=wayland
export WLR_RENDERER_ALLOW_SOFTWARE=1
export WLR_NO_HARDWARE_CURSORS=1
exec /usr/local/bin/startlxqtwayland
WRAP
chmod +x /usr/local/bin/greetd-labwc-wrap

# 8. greetd's initial_session has historically failed on this image
#    (greeter session never reaches a seat — investigation pending,
#    `todo/permissions-gui-vm-bootstrap.md`). Workaround: use a
#    plain agetty autologin on tty1 + admin's .bash_profile that
#    execs labwc. This gives admin a real PAM/logind session with
#    a seat and labwc starts cleanly.
mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<'EOF2'
[Service]
ExecStart=
ExecStart=-/sbin/agetty -o '-p -f -- \\u' --noclear --autologin admin %I $TERM
EOF2

cat > /home/admin/.bash_profile <<'EOF2'
# Auto-exec labwc when logged in on tty1 (test-VM autologin).
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec /usr/local/bin/greetd-labwc-wrap
fi
EOF2
chown admin:users /home/admin/.bash_profile

# Make sure greetd stays out of the way and getty@tty1 wins.
systemctl mask greetd.service 2>/dev/null || true
systemctl set-default multi-user.target >/dev/null
systemctl daemon-reload
systemctl restart getty@tty1.service

# Disable the qdwin session units — they'd race labwc on tty1.
runuser -l admin -c 'systemctl --user disable --now noctalia-session.service noctalia-shell.service 2>/dev/null' || true

# Wait up to 30s for admin's wayland-0 socket to appear (labwc up).
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if [ -e /run/user/1000/wayland-0 ]; then
        echo "[gui-spin] wayland-0 socket up after ${i}s"
        break
    fi
    sleep 2
done

# Confirm the surface the scenarios need:
echo "--- post-install state ---"
id work
id work2
ls -l /usr/local/bin/qdistro-{test-permission,start-admin-app,start-admin-tui}
ls -l /usr/local/bin/qdistro-admin-tui
ls -l /usr/local/bin/startlxqtwayland /usr/local/bin/qdistro-lxqt-session-wrap
ls -l /home/admin/qdistro/admin_app/qdistro_admin_app.py
ls -l /home/admin/qdistro/tui/qdistro_admin_tui.py
ls -l /home/admin/.config/qterminal.org/qterminal.ini
ls -l /run/user/1000/wayland-0 2>&1 || echo "WARN: wayland-0 not up"
ps -ef | grep -E "labwc|lxqt|Xwayland" | grep -v grep | head -5
echo "--- done ---"
POSTBOOT
)
$VMEXEC "$VM" "echo $B64 | base64 -d | QDISTRO_VM_PASSWORD='$QDISTRO_VM_PASSWORD' bash" >&2

# Note on display resolution: the QXL video device in the template
# (create-template-domain.sh) exposes only a 640×480 mode via vesafb.
# Scenarios assuming a larger display (e.g. 35 — TUI + Qt admin app
# side-by-side, qterminal pinned to 1200×700 per AGENTS.md ) won't
# render correctly. Attempted fix (swap QXL → virtio-vga with
# xres/yres) was abandoned because:
#   - libvirt's <video><model type='virtio'/></video> maps to
#     qemu's virtio-vga (with VGA-compat shim); the kernel's
#     vesa-framebuffer driver grabs the framebuffer before
#     virtio_gpu can claim the PCI device, so wlroots still
#     sees only 640×480 via vesadrm.
#   - Switching to virtio-gpu-pci (no VGA shim) would need
#     deeper changes to the template + kernel cmdline + possibly
#     initramfs.
# Tracked in `todo/permissions-gui-vm-bootstrap.md` §"VM display
# resolution gap"; scenarios that need larger geometry stay
# platform-blocked.

echo "[gui-spin] step 3/3: ready. Scenarios can target VMNAME=$VM" >&2
# Final stdout line: the VM name, matching spin-test-vm.sh's contract.
echo "$VM"
