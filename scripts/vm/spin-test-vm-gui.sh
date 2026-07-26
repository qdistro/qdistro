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
# fresh-vm-bootstrap.sh masks greetd (greetd would grab the DRM seat and
# prevent admin's lingering user manager from starting) and stages BOTH the
# qdwin+qdshell session units (qdwin-compositor.service/qdshell.service via
# qdwin-session.target) and the labwc
# stack. greetd stays masked here; this script then selects which session
# admin's user manager leaves running, via QDISTRO_VM_GUI_SESSION:
#
#   labwc (default) — disables the qdwin units and runs labwc+lxqt on wayland-0
#                     for the permissions-gui admin-app scenarios.
#   qdwin           — keeps the production qdwin compositor + qdshell session on
#                     wayland-1 (the one fresh-vm-bootstrap.sh already started),
#                     for the qdwin/qdshell GUI lanes (taskbar isolation menu,
#                     popup-clamp, …). Needs the vendored libweston (staged by
#                     fresh-vm-bootstrap.sh) for the popup clamp/grab paths.
#
# Usage:
#   [QDISTRO_VM_GUI_SESSION=labwc|qdwin] scripts/vm/spin-test-vm-gui.sh [<prefix>]
#
# Output (last line): the VM name, suitable for piping into
# `tests/integration/permissions-gui/` (labwc) or `qdwin-noctalia/` (qdwin)
# runner agents as VMNAME.

set -euo pipefail

VM_PASSWORD='Pa_ssw0rd45'
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${1:-qd-gui}"

# Which graphical session to leave running (see the POSTBOOT block for the full
# contract): `labwc` (default — labwc+lxqt on wayland-0 for permissions-gui) or
# `qdwin` (the production qdwin compositor + qdshell on wayland-1, for the
# qdwin/qdshell GUI lanes). Select with QDISTRO_VM_GUI_SESSION=qdwin.
GUI_SESSION="${QDISTRO_VM_GUI_SESSION:-labwc}"
case "$GUI_SESSION" in
    labwc|qdwin) ;;
    *) echo "[gui-spin] ERROR: QDISTRO_VM_GUI_SESSION='$GUI_SESSION' (want labwc|qdwin)" >&2; exit 2 ;;
esac

# Per-run GOLDEN clone? acquire_vm exports QCI_RUN_GOLDEN_BACKING when cloning a
# worker from the gui golden. On a clone the whole gui layer (work users, SDK,
# launchers, labwc/qdwin install, §8b virtio-gpu, autologin/session config) is
# already BAKED into the golden and the staged source ($SRC) is absent, so the
# POSTBOOT install path is skipped (see the GOLDEN_CLONE fast-path inside it) and
# we only verify the baked session came up.
GOLDEN_CLONE=0
[ -n "${QCI_RUN_GOLDEN_BACKING:-}" ] && GOLDEN_CLONE=1

echo "[gui-spin] step 1/3: spin-test-vm.sh $PREFIX (broker layer)" >&2
# spin-test-vm prints the VM name on the last stdout line. Capture stderr to
# console, stdout to a variable. QCI_SPIN_VERIFY_SESSION=none: the child's
# stage-6 wayland-1 check is qdwin-specific (and a labwc golden clone has no
# wayland-1) — the gui POSTBOOT below does profile-aware verification instead.
VM=$(QCI_SPIN_VERIFY_SESSION=none bash "$SCRIPT_DIR/spin-test-vm.sh" "$PREFIX" 2>&1 | tee /dev/stderr | tail -1)

if ! echo "$VM" | grep -qE "^${PREFIX}-[0-9]+-[0-9]+(-[0-9]+)*$"; then
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

# Which graphical session this VM should expose to scenarios:
#   labwc  (default) — labwc + lxqt on wayland-0, for the permissions-gui
#                      admin-app scenarios (the historical behaviour).
#   qdwin            — the production qdwin compositor + qdshell (Quickshell)
#                      session on wayland-1, for the qdwin/qdshell GUI lanes
#                      (taskbar isolation menu, popup-clamp, etc.). The session
#                      units (qdwin-compositor.service/qdshell.service via
#                      qdwin-session.target — the qdwin+qdshell
#                      units install-qdwin-session-for-vm.sh wires up), the
#                      vendored libweston, and the QML plugin are already staged
#                      by fresh-vm-bootstrap.sh; this profile simply KEEPS that
#                      session up instead of tearing it down for labwc.
SESSION="${QDISTRO_VM_GUI_SESSION:-labwc}"
case "$SESSION" in labwc|qdwin) ;; *)
    echo "[gui-spin] ERROR: QDISTRO_VM_GUI_SESSION='$SESSION' (want labwc|qdwin)"; exit 2 ;;
esac
echo "[gui-spin] gui session profile: $SESSION"

# GOLDEN-CLONE FAST-PATH. Everything below (work/work2 users, SDK, launchers,
# labwc/qdwin install, §8b virtio-gpu dracut/grub, autologin + session config)
# is already BAKED into the gui golden disk, and the staged source ($SRC) does
# NOT exist on a clone — so re-running it would fail and/or duplicate work. A
# clone boots straight into the baked session (getty autologin → labwc, or the
# enabled noctalia user units → qdwin). We only VERIFY, fail-closed and
# profile-aware, that the baked session + the surface scenarios depend on came
# up, then exit. The full-build path (golden build / QCI_NO_GOLDEN) is below.
if [ "${GOLDEN_CLONE:-0}" = 1 ]; then
    echo "[gui-spin] golden clone: skipping install, verifying baked session ($SESSION)"
    if [ "$SESSION" = labwc ]; then
        for _i in $(seq 1 30); do [ -S /run/user/1000/wayland-0 ] && break; sleep 2; done
        if [ ! -S /run/user/1000/wayland-0 ]; then
            echo "[gui-spin] ERROR: wayland-0 missing on labwc golden clone"
            runuser -l admin -c 'systemctl --user --no-pager status 2>&1 | head -30' || true
            journalctl -b --no-pager 2>&1 | tail -40 || true
            exit 1
        fi
        pgrep -x labwc >/dev/null || { echo "[gui-spin] ERROR: labwc not running on clone"; exit 1; }
        echo "[gui-spin] labwc clone session up (wayland-0)"
    else
        for _i in $(seq 1 30); do [ -S /run/user/1000/wayland-1 ] && break; sleep 2; done
        if [ ! -S /run/user/1000/wayland-1 ]; then
            echo "[gui-spin] ERROR: wayland-1 missing on qdwin golden clone"
            runuser -l admin -c 'systemctl --user --no-pager status qdwin-compositor.service qdshell.service 2>&1 | head -40' || true
            exit 1
        fi
        runuser -l admin -c 'systemctl --user is-active qdwin-compositor.service qdshell.service' >/dev/null 2>&1 \
            || { echo "[gui-spin] ERROR: qdwin session units not active on qdwin clone"; exit 1; }
        grep -qx 'renderer=pixman' /home/admin/weston.ini \
            || { echo "[gui-spin] ERROR: qdwin golden clone lost its pixman renderer pin"; exit 1; }
        grep -qx 'mode=1280x800@60' /home/admin/weston.ini \
            || { echo "[gui-spin] ERROR: qdwin golden clone lost its fixed GUI-test output mode"; exit 1; }
        echo "[gui-spin] qdwin clone session up (wayland-1)"
    fi
    # The baked install surface the scenarios require — fail-closed on core bits
    # (codex review: verify, don't ls-and-warn, the things a clone depends on).
    id work >/dev/null 2>&1 && id work2 >/dev/null 2>&1 \
        || { echo "[gui-spin] ERROR: work/work2 users missing in golden"; exit 1; }
    for _f in /usr/local/bin/qdistro-test-permission \
              /usr/local/bin/qdistro-start-admin-app \
              /usr/local/bin/qdistro-start-admin-tui; do
        [ -x "$_f" ] || { echo "[gui-spin] ERROR: baked launcher $_f missing in golden"; exit 1; }
    done
    /usr/bin/python3 -c 'import qdistro_app' 2>/dev/null \
        || { echo "[gui-spin] ERROR: qdistro_app SDK not importable in golden clone"; exit 1; }
    echo "[gui-spin] golden clone verification OK ($SESSION)"
    exit 0
fi

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
echo "work:${VM_PASSWORD}" | chpasswd
echo "work2:${VM_PASSWORD}" | chpasswd

# 2. qdistro-test-permission helper.
install -m 0755 "$SRC/tests/unit/test_permission.py" \
                /usr/local/bin/qdistro-test-permission
PY_SITE=$(/usr/bin/python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
install -d -m 0755 "$PY_SITE/qdistro_app"
for _sdk_py in "$SRC/sdk/qdistro_app"/*.py; do
    install -m 0644 "$_sdk_py" "$PY_SITE/qdistro_app/"
done
/usr/bin/python3 - <<'PY'
import qdistro_app  # noqa: F401
PY

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
# Relay: run the REAL installer rather than open-coding the file drop, so
# the GUI harness exercises the production layout. It lands the module in
# /usr/libexec/qdistro beside the broker modules (baked into the golden
# image), which is what makes the Firefox-containers opt-in gate's
# qdistro_admin_rules import resolve. Open-coding it here is how the
# harness previously ended up one prefix behind the unit's ExecStart.
bash "$SRC/scripts/install/install-user-relay-for-vm.sh" "$SRC/user_relay"
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

# Steps 6-8 below stand up the labwc+lxqt session and are skipped for the
# `qdwin` profile (which keeps the qdwin+qdshell session staged by
# fresh-vm-bootstrap.sh instead — see step 8c). §8b (virtio-gpu DRM) runs for
# BOTH profiles. The body is intentionally left un-indented to keep the diff
# reviewable; it is bracketed by this `if`/`fi`.
if [ "$SESSION" = labwc ]; then
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
fi  # end labwc-only steps 6-8

# 8b. Display-resolution fix — make virtio_gpu the DRM driver instead
#     of the generic vesa/simple framebuffer.
#     Generic — runs for BOTH the labwc and qdwin profiles (qdwin's weston
#     drm-backend also wants virtio_gpu to win over simpledrm).
#
#     The template (create-template-domain.sh, default path) now
#     exposes a bare virtio-gpu-pci device (no VGA-compat shim), so
#     there is no firmware VGA framebuffer for vesadrm/simpledrm to
#     claim. Two guest-side changes make virtio_gpu win deterministically
#     and survive a reboot:
#
#       (a) Force virtio_gpu into the initramfs so it binds the PCI
#           device at the earliest possible point (before any generic
#           framebuffer handoff). dracut's drm module pulls in the
#           generic fb drivers too; listing virtio_gpu explicitly in
#           force_drivers guarantees it's present and probed first.
#       (b) Kernel cmdline: blacklist the generic framebuffer platform
#           driver so that even if firmware did expose a framebuffer,
#           simpledrm doesn't grab it ahead of virtio_gpu. We add
#           `initcall_blacklist=simpledrm_platform_driver_init` only.
#           We do NOT add `nomodeset` (we WANT KMS) and do NOT pin a
#           `video=` mode (the connector name is kernel-version
#           dependent; wlroots sets the real mode once virtio_gpu
#           binds).
#
#     After a reboot of the VM the guest should let
#     `wlr-randr --output Virtual-1 --custom-mode 1280x800@60` succeed
#     (the connector name is whatever wlr-randr lists; virtio_gpu names
#     it Virtual-1 on this image). This is the intended path; verifying
#     it requires a live boot (see the resolution note at the foot of
#     this script).
echo "[gui-spin] applying virtio-gpu DRM + cmdline fix (effective next boot)"
mkdir -p /etc/dracut.conf.d
cat > /etc/dracut.conf.d/90-qdistro-virtio-gpu.conf <<'DRACUT'
# Bind virtio_gpu early so it claims the PCI GPU before the generic
# vesa/simple framebuffer platform driver can. Required for wlroots to
# see modes >640x480 on the qdistro test template (virtio-gpu-pci, no
# VGA shim). See scripts/vm/spin-test-vm-gui.sh §8b.
force_drivers+=" virtio_gpu "
DRACUT

# Append the cmdline token idempotently to the default GRUB config.
# We edit GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub (skipping the
# edit if the token is already present), then regenerate the bootloader
# config below. If grub2-mkconfig is unavailable (rare on this image)
# we fall back to openSUSE's update-bootloader wrapper.
# Keep the token list minimal and well-known to avoid an unbootable
# cmdline: the only token we need is the simpledrm initcall blacklist.
# (A `video=` mode hint was considered but the connector name is
# kernel-version-dependent and a wrong token is silently ignored at
# best, fatal at worst — wlroots sets the real mode anyway once
# virtio_gpu binds, so we don't pin a console mode here.)
GRUB_ADD="initcall_blacklist=simpledrm_platform_driver_init"
if [ -f /etc/default/grub ]; then
    if ! grep -q "initcall_blacklist=simpledrm_platform_driver_init" /etc/default/grub; then
        # Insert the token inside the existing quoted value.
        sed -i "s/^\(GRUB_CMDLINE_LINUX_DEFAULT=\"[^\"]*\)\"/\1 $GRUB_ADD\"/" \
            /etc/default/grub
    fi
fi

# Rebuild initramfs (dracut) and grub.cfg. Both are best-effort: a
# failure here leaves the VM bootable on its current initramfs/cmdline
# (the device is virtio-gpu-pci already; the worst case is the generic
# fb still wins until a successful regen). We log loudly so a CI run
# surfaces it.
if command -v dracut >/dev/null 2>&1; then
    dracut --force --regenerate-all >/dev/null 2>&1 \
        || echo "[gui-spin] WARN: dracut regenerate-all failed"
fi
if command -v grub2-mkconfig >/dev/null 2>&1; then
    grub2-mkconfig -o /boot/grub2/grub.cfg >/dev/null 2>&1 \
        || echo "[gui-spin] WARN: grub2-mkconfig failed"
elif command -v update-bootloader >/dev/null 2>&1; then
    update-bootloader >/dev/null 2>&1 \
        || echo "[gui-spin] WARN: update-bootloader failed"
fi

# greetd stays masked in BOTH profiles (it would grab the DRM seat and block
# admin's lingering user manager — see fresh-vm-bootstrap.sh). The two profiles
# differ only in which session admin's user manager runs.
systemctl mask greetd.service 2>/dev/null || true

if [ "$SESSION" = labwc ]; then
    # labwc: getty@tty1 autologin execs labwc on wayland-0; the qdwin session
    # units would race it for the DRM seat, so disable them.
    systemctl set-default multi-user.target >/dev/null
    systemctl daemon-reload
    systemctl restart getty@tty1.service
    runuser -l admin -c 'systemctl --user disable --now qdwin-session.target qdwin-compositor.service qdshell.service 2>/dev/null' || true

    # Wait up to 30s for admin's wayland-0 socket to appear (labwc up).
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
        if [ -e /run/user/1000/wayland-0 ]; then
            echo "[gui-spin] wayland-0 socket up after ${i}s"
            break
        fi
        sleep 2
    done
else
    # 8c. qdwin profile: keep the qdwin compositor + qdshell session that
    #     fresh-vm-bootstrap.sh staged and started under admin's lingering user
    #     manager (deploy-named units qdwin-compositor.service / qdshell.service,
    #     pulled in by qdwin-session.target — see install-qdwin-session-for-vm.sh).
    #     Ensure the target is enabled + (re)started (idempotent — bootstrap
    #     normally already has wayland-1 up) and wait for the wayland-1 socket.
    # GUI CI has no host-side SPICE/RDP viewer, so it does not need the GL-only
    # virtio cursor-plane path used by an interactive desktop. Pin Pixman in
    # disposable qdwin workers: virtio-gpu's GL/KMS path can begin rejecting
    # every atomic commit when a full-output LOCK-layer surface appears, leaving
    # screenshots black even though the locker and input protocol are healthy.
    # Pixman renders the same guest UI deterministically and keeps every GUI
    # pixel inside the VM. Pin the output to the VM helper's 1280x800 QMP
    # coordinate space too: if Weston switches from the firmware 1280x800 mode
    # to production's 1920x1080 after a test samples the framebuffer, absolute
    # tablet input is rescaled between observation and injection. Fixed mode +
    # renderer are test-profile policy only; production's installer continues
    # to default to renderer=gl and its normal 1920x1080 output.
    if grep -q '^renderer=' /home/admin/weston.ini; then
        sed -i 's/^renderer=.*/renderer=pixman/' /home/admin/weston.ini
    else
        sed -i '/^\[core\]$/a renderer=pixman' /home/admin/weston.ini
    fi
    if grep -q '^mode=' /home/admin/weston.ini; then
        sed -i 's/^mode=.*/mode=1280x800@60/' /home/admin/weston.ini
    else
        sed -i '/^\[output\]$/a mode=1280x800@60' /home/admin/weston.ini
    fi
    grep -qx 'renderer=pixman' /home/admin/weston.ini \
        || { echo "[gui-spin] ERROR: failed to pin qdwin GUI renderer to pixman"; exit 1; }
    grep -qx 'mode=1280x800@60' /home/admin/weston.ini \
        || { echo "[gui-spin] ERROR: failed to pin qdwin GUI output to 1280x800"; exit 1; }

    # Make the compositor's VT unstealable by an injected chord.
    #
    # A GUI scenario that sends Super+Left (qdwin/tests/gui/19-wm-policy.md
    # drives tile-left with it) was switching the VT away from the compositor,
    # which kills the seat: seatd revokes the evdev fds, weston drops DRM
    # master, and since capture is only serviced during a repaint EVERY later
    # screenshot in the scenario fails. Two independent causes, both fixed here
    # because either alone is sufficient and neither needs a golden rebake:
    #
    #  1. The chord reaches the KERNEL console, not just weston. weston shares
    #     tty1 with a stock getty@tty1 (the autologin drop-in above is
    #     labwc-only), and getty's start-time TTY reset reverts the K_OFF that
    #     seatd installed — journals show injected keystrokes landing in
    #     login(1) as `FAILED LOGIN 1 FROM tty1`. With the console keyboard
    #     live, the openSUSE xkb-converted keymap makes Super+Left a console
    #     switch outright: `keycode 125 = Alt` and `alt keycode 105 =
    #     Decr_Console` (/usr/share/kbd/keymaps/xkb/us.map.gz:92,106).
    #     Nothing in the qdwin profile uses tty1 — CI drives the VM over
    #     qemu-guest-agent and serial-getty@ttyS0 — so disable it. That also
    #     stops test keystrokes leaking into a login prompt.
    #
    #  2. Decr_Console had somewhere to land only because logind reserves a VT.
    #     `ReserveVT` defaults to 6 and marks tty6 busy unconditionally, so the
    #     kernel's downward scan (which wraps from 62) stopped at tty6 —
    #     deterministically, which is why every occurrence names tty6.
    #     ReserveVT=0 + NAutoVTs=0 leave tty2-6 unallocated, so the switch
    #     becomes a no-op even if a console keyboard is somehow live.
    #
    # NB: this is NOT what weston.ini's vt-switching=false covers. That closes
    # weston's own Ctrl+Alt+F1..F8 bindings, a separate path this chord never
    # touches. Keep both.
    systemctl disable --now getty@tty1.service >/dev/null 2>&1 || true
    mkdir -p /etc/systemd/logind.conf.d
    cat > /etc/systemd/logind.conf.d/99-qdwin-no-spare-vts.conf <<'EOF2'
# qdwin GUI test profile: no reserved/auto VT for an injected console chord to
# switch to. See spin-test-vm-gui.sh for the full rationale.
[Login]
ReserveVT=0
NAutoVTs=0
EOF2
    systemctl restart systemd-logind >/dev/null 2>&1 || true

    systemctl daemon-reload
    runuser -l admin -c 'systemctl --user enable qdwin-session.target 2>/dev/null' || true
    runuser -l admin -c 'systemctl --user start qdwin-session.target' || true
    # Bootstrap normally started the session before this test-profile layer was
    # applied. Restart the compositor so the new renderer is active in the
    # golden, then reconnect its shell and optional standalone locker peers.
    runuser -l admin -c 'systemctl --user restart qdwin-compositor.service'
    for _i in $(seq 1 30); do
        [ -S /run/user/1000/wayland-1 ] && break
        sleep 0.5
    done
    [ -S /run/user/1000/wayland-1 ] \
        || { echo "[gui-spin] ERROR: qdwin compositor restart did not recreate wayland-1"; exit 1; }
    runuser -l admin -c 'systemctl --user restart qdshell.service'
    runuser -l admin -c 'systemctl --user try-restart qdlocker.service 2>/dev/null' || true

    # Wait up to 60s for admin's wayland-1 socket (qdwin compositor up). The
    # compositor restarts on-failure, so give it longer than the labwc path.
    for i in $(seq 1 30); do
        if [ -S /run/user/1000/wayland-1 ]; then
            echo "[gui-spin] wayland-1 socket up after $((i*2))s (qdwin compositor)"
            break
        fi
        sleep 2
    done
    if [ ! -S /run/user/1000/wayland-1 ]; then
        # Fail CLOSED: the whole point of this profile is to hand back a VM
        # running qdwin+qdshell. If the compositor never came up, returning a
        # VM name would let downstream qdwin/qdshell lanes run against a VM that
        # does not satisfy the requested profile. Dump diagnostics, then abort
        # (this exit propagates through vm-exec to the host spinner's set -e).
        echo "[gui-spin] ERROR: wayland-1 socket did not appear within 60s — qdwin compositor failed to start"
        runuser -l admin -c 'systemctl --user --no-pager status qdwin-session.target qdwin-compositor.service qdshell.service 2>&1 | head -40' || true
        runuser -l admin -c 'journalctl --user -u qdwin-compositor.service -u qdshell.service --no-pager -n 40 2>&1' || true
        exit 1
    fi
fi

# Confirm the surface the scenarios need:
echo "--- post-install state ($SESSION) ---"
id work
id work2
ls -l /usr/local/bin/qdistro-{test-permission,start-admin-app,start-admin-tui}
ls -l /usr/local/bin/qdistro-admin-tui
ls -l /home/admin/qdistro/admin_app/qdistro_admin_app.py
ls -l /home/admin/qdistro/tui/qdistro_admin_tui.py
ls -l /etc/dracut.conf.d/90-qdistro-virtio-gpu.conf 2>&1 \
    || echo "WARN: virtio-gpu dracut conf not installed"
grep -H "simpledrm_platform_driver_init" /etc/default/grub 2>&1 \
    || echo "WARN: simpledrm blacklist not in GRUB cmdline"
if [ "$SESSION" = labwc ]; then
    ls -l /usr/local/bin/startlxqtwayland /usr/local/bin/qdistro-lxqt-session-wrap
    ls -l /home/admin/.config/qterminal.org/qterminal.ini
    ls -l /run/user/1000/wayland-0 2>&1 || echo "WARN: wayland-0 not up"
    ps -ef | grep -E "labwc|lxqt|Xwayland" | grep -v grep | head -5
else
    # qdwin profile: the compositor socket, the vendored libweston the clamp/
    # popup-grab paths need, and the live qs (qdshell) + weston processes.
    ls -l /run/user/1000/wayland-1 2>&1 || echo "WARN: wayland-1 not up"
    ls -ld /usr/libexec/qdistro/qdwin-libweston/lib64 2>&1 \
        || echo "WARN: vendored libweston not installed (popup clamp/grab will be absent)"
    runuser -l admin -c 'systemctl --user is-active qdwin-compositor.service qdshell.service' 2>&1 || true
    grep -qx 'renderer=pixman' /home/admin/weston.ini \
        || { echo "ERROR: qdwin GUI test profile is not using pixman"; exit 1; }
    grep -qx 'mode=1280x800@60' /home/admin/weston.ini \
        || { echo "ERROR: qdwin GUI test profile is not using its fixed 1280x800 mode"; exit 1; }
    ps -ef | grep -E "[w]eston|[q]s -p|quickshell" | head -5 || true
fi
echo "--- done ---"
POSTBOOT
)
$VMEXEC "$VM" "echo $B64 | base64 -d | VM_PASSWORD='$VM_PASSWORD' QDISTRO_VM_GUI_SESSION='$GUI_SESSION' GOLDEN_CLONE='$GOLDEN_CLONE' bash" >&2

# Note on display resolution (FIXED — verify on a live boot):
#   Earlier templates used QXL / virtio-vga (VGA-compat shim), which
#   exposed only a 640×480 vesadrm mode because the kernel's generic
#   framebuffer platform driver grabbed the firmware VGA framebuffer
#   before virtio_gpu could bind. wlr-randr could not raise the mode.
#
#   The fix lands in two halves:
#     1. create-template-domain.sh now defines a bare virtio-gpu-pci
#        device (model='none' + a <qemu:commandline> override) with NO
#        VGA-compat shim — so there is no firmware framebuffer to grab.
#        (Escape hatch: QDISTRO_TEMPLATE_LEGACY_VGA=1 restores the old
#        virtio-vga device.)
#     2. §8b above forces virtio_gpu into the initramfs
#        (/etc/dracut.conf.d/90-qdistro-virtio-gpu.conf) and blacklists
#        the simpledrm platform driver on the kernel cmdline, then
#        regenerates initramfs + grub.cfg. virtio_gpu therefore binds
#        the PCI GPU and presents a real DRM device.
#
#   Expected result after the VM reboots onto the regenerated
#   initramfs/cmdline: wlroots sees a virtio_gpu DRM card and
#       wlr-randr --output Virtual-1 --custom-mode 1280x800@60
#   succeeds (connector name per `wlr-randr` output; virtio_gpu names
#   it Virtual-1 on this image). This cannot be booted headless in CI
#   here; a live boot must confirm the >640×480 mode. The §8b changes
#   take effect on the NEXT boot — the just-bootstrapped VM is still on
#   its original initramfs, so reboot it (`virsh reboot $VM`) before
#   running geometry-sensitive scenarios (e.g. 35).

echo "[gui-spin] step 3/3: ready ($GUI_SESSION session). Scenarios can target VMNAME=$VM" >&2
# Final stdout line: the VM name, matching spin-test-vm.sh's contract.
echo "$VM"
