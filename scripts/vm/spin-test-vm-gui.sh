#!/bin/bash
# spin-test-vm-gui.sh — like spin-test-vm.sh, plus the post-bootstrap layer
# the GUI scenarios depend on. ONE profile: the product session (qdwin
# compositor + qdshell on wayland-1) that fresh-vm-bootstrap.sh stages and
# starts under admin's lingering user manager; greetd stays masked (it would
# grab the DRM seat and keep that user manager from starting).
#
#   - /usr/local/bin/qdistro-test-permission helper + the qdistro_app SDK.
#   - The admin approvals app as the image installs it
#     (install-admin-app-for-vm.sh: /usr/local/bin/qdistro-admin-approval-app,
#     the shipped native-Wayland launcher /usr/local/bin/qdistro-start-admin-app
#     and the desktop entry). The admin TUI is the one fresh-vm-bootstrap.sh
#     installed (install-admin-cli-for-vm.sh), verified intact here; the TUI
#     scenarios host it in foot, a core test client of every golden.
#   - The test-profile pins: pixman renderer, fixed 1280x800 output, no spare
#     VTs, the patterned test wallpaper, virtio-gpu as the DRM driver.
#   - The work/work2 silo fixtures are NOT baked: scenarios that need them
#     provision them at Setup (qdwin_admin_lane_setup --silos), because Active
#     silos change what the qdlocker scenarios' lock screen shows.
#
# Usage:
#   scripts/vm/spin-test-vm-gui.sh [<prefix>]
#
# Output (last line): the VM name, suitable for the GUI scenario runners as
# VMNAME.

set -euo pipefail

VM_PASSWORD='Pa_ssw0rd45'
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${1:-qd-gui}"

# Per-run GOLDEN clone? acquire_vm exports QCI_RUN_GOLDEN_BACKING when cloning a
# worker from the gui golden. On a clone the whole gui layer (SDK, admin app,
# test-profile pins, §8b virtio-gpu, session config) is already BAKED into the
# golden, so the POSTBOOT install path is skipped (see the GOLDEN_CLONE
# fast-path inside it) and we only verify the baked session came up.
GOLDEN_CLONE=0
[ -n "${QCI_RUN_GOLDEN_BACKING:-}" ] && GOLDEN_CLONE=1

echo "[gui-spin] step 1/3: spin-test-vm.sh $PREFIX (broker layer)" >&2
# spin-test-vm prints the VM name on the last stdout line. Capture stderr to
# console, stdout to a variable. Its stage 6 verifies the qdwin session
# (wayland-1); the POSTBOOT below verifies the GUI-specific layer.
VM=$(bash "$SCRIPT_DIR/spin-test-vm.sh" "$PREFIX" 2>&1 | tee /dev/stderr | tail -1)

if ! echo "$VM" | grep -qE "^${PREFIX}-[0-9]+-[0-9]+(-[0-9]+)*$"; then
    echo "[gui-spin] could not parse VM name from spin-test-vm.sh output: '$VM'" >&2
    exit 1
fi
echo "[gui-spin] broker VM ready: $VM" >&2

VMEXEC="$SCRIPT_DIR/vm-exec"

echo "[gui-spin] step 2/3: layering the GUI scenario prereqs on $VM" >&2

# Base64 the whole post-bootstrap so embedded quotes / heredocs don't
# trip vm-exec's JSON quoting.
B64=$(base64 -w0 <<'POSTBOOT'
set -eu

SRC=/root/qdistro-src

# The installed admin TUI must be the installer's: the entry symlink pointing at
# the root-owned module (never a wrapper written through the symlink -- the
# retired dev-copy step here did exactly that and stripped the TUI of the
# broker's authority shape), and Textual/Rich importable. Fatal: every TUI
# scenario runs this installed TUI, so a broken one is a bake failure.
verify_installed_tui() {
    local lib=/usr/local/lib/qdistro/admin-tui/qdistro_admin_tui.py first
    [ "$(readlink /usr/local/bin/qdistro-admin-tui)" = "$lib" ] \
        || { echo "[gui-spin] ERROR: /usr/local/bin/qdistro-admin-tui is not the installer's symlink to $lib"; exit 1; }
    [ -f "$lib" ] && [ ! -L "$lib" ] && [ "$(stat -c %u "$lib")" = 0 ] \
        || { echo "[gui-spin] ERROR: $lib is not a root-owned regular file"; exit 1; }
    IFS= read -r first < "$lib" || first=""
    [ "$first" = '#!/usr/bin/python3 -I' ] && grep -q '^def _reexec_for_authority' "$lib" \
        || { echo "[gui-spin] ERROR: $lib is not the installed TUI module (first line: $first)"; exit 1; }
    if [ -f "$SRC/tui/qdistro_admin_tui.py" ]; then
        cmp -s "$lib" "$SRC/tui/qdistro_admin_tui.py" \
            || { echo "[gui-spin] ERROR: $lib differs from $SRC/tui/qdistro_admin_tui.py"; exit 1; }
    fi
    /usr/bin/python3 -I -c 'import textual, rich' \
        || { echo "[gui-spin] ERROR: Textual/Rich do not import under /usr/bin/python3 -I"; exit 1; }
    command -v foot >/dev/null 2>&1 \
        || { echo "[gui-spin] ERROR: foot (core test client hosting the TUI scenarios) missing"; exit 1; }
}

# GOLDEN-CLONE FAST-PATH. Everything below (SDK, admin app, test-profile pins,
# §8b virtio-gpu dracut/grub, session config) is already BAKED into the gui
# golden disk, so re-running it would duplicate work. A clone boots straight
# into the baked qdwin session. We only VERIFY, fail-closed, that the session
# and the surface scenarios depend on came up, then exit. The full-build path
# (golden build / QCI_NO_GOLDEN) is below.
if [ "${GOLDEN_CLONE:-0}" = 1 ]; then
    echo "[gui-spin] golden clone: skipping install, verifying the baked qdwin session"
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
    # The baked install surface the scenarios require — fail-closed on core bits
    # (codex review: verify, don't ls-and-warn, the things a clone depends on).
    for _f in /usr/local/bin/qdistro-test-permission \
              /usr/local/bin/qdistro-start-admin-app; do
        [ -x "$_f" ] || { echo "[gui-spin] ERROR: baked launcher $_f missing in golden"; exit 1; }
    done
    # The scenarios run the SHIPPED native-Wayland admin app launcher and app.
    grep -qx 'export QT_QPA_PLATFORM=wayland' /usr/local/bin/qdistro-start-admin-app \
        || { echo "[gui-spin] ERROR: qdwin golden's qdistro-start-admin-app is not the shipped Wayland launcher"; exit 1; }
    [ -f /usr/local/bin/qdistro-admin-approval-app ] \
        || { echo "[gui-spin] ERROR: qdwin golden lacks /usr/local/bin/qdistro-admin-approval-app"; exit 1; }
    verify_installed_tui
    /usr/bin/python3 -c 'import qdistro_app' 2>/dev/null \
        || { echo "[gui-spin] ERROR: qdistro_app SDK not importable in golden clone"; exit 1; }
    echo "[gui-spin] golden clone verification OK (qdwin)"
    exit 0
fi

# 1. work + work2 silo fixtures are NOT baked into the golden, on purpose.
#
# Registered ACTIVE silos are not invisible the way the old plain accounts
# were: qdshell's SiloEgress service normalises egress=None to a
# legacy/host row and lists Active legacy silos, and the qdwin LOCK SCREEN
# renders a network-egress indicator from exactly that list. All qdlocker
# GUI scenarios run on this golden, so baking the fixtures here would change
# what those screens show. The permissions-gui scenarios that need work/work2
# provision them at Setup, with scripts/vm/lib/gui-silo-fixtures.sh, on
# their own disposable clone (qdwin_admin_lane_setup --silos).

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

# 3. The admin approvals app as the image installs it: the real installer lays
# down the app at /usr/local/bin/qdistro-admin-approval-app (the path the
# broker trusts), the native-Wayland launcher (deploy/start-admin-app-wayland.sh)
# and the desktop entry, exactly as image/config.sh does.
bash "$SRC/scripts/install/install-admin-app-for-vm.sh" "$SRC/admin_app" \
    || { echo "[gui-spin] ERROR: install-admin-app-for-vm.sh failed"; exit 1; }
grep -qx 'export QT_QPA_PLATFORM=wayland' /usr/local/bin/qdistro-start-admin-app \
    || { echo "[gui-spin] ERROR: /usr/local/bin/qdistro-start-admin-app is not" \
              "the shipped native-Wayland launcher"; exit 1; }
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

# 4. The admin TUI is NOT reinstalled here: fresh-vm-bootstrap.sh ran the
# image's installer (install-admin-cli-for-vm.sh). Verify it is intact.
verify_installed_tui

# DESKTOP WALLPAPER: install_test_wallpaper renders the patterned test
# wallpaper to /home/admin/Pictures/Wallpapers/qdistro-test-pattern.png, which
# qdshell shows (wallpaper directory default). The body keeps its 4-space
# indent: make-test-wallpaper.sh and the gate tests extract the generator by
# that exact line shape.
install_test_wallpaper() {
    # DESKTOP WALLPAPER: make the ordinary idle desktop visibly non-flat.
    #
    # qdshell ships `wallpaper.enabled: true` with an EMPTY `directory`
    # (Commons/Settings.qml), defaulting to ~/Pictures/Wallpapers, and nothing
    # here used to populate it -- so the test desktop background was BLACK. A
    # healthy idle desktop and a dead compositor then produced the same frame,
    # which is why this failure class kept being diagnosed indirectly
    # (36b1ce6 "the 'app' screenshots were actually the qdlocker lock screen",
    # 3dcd6bd, qdlocker 14c19a5, qdshell 4885738e8, qdwin 6e6d860). With a
    # patterned background the ordinary idle desktop is no longer FLAT, so it
    # is distinguishable from the uniform-black signature instead of identical
    # to it.
    #
    # THIS IS NOT A LIVENESS PROOF. A structured frame shows the pattern was
    # rendered at some point; a frozen framebuffer keeps its last contents, and
    # that is exactly the state seen in gui-20260919T072913Z (three retries,
    # byte-identical). Repeat-capture comparison stays the liveness test. Nor
    # does it mean every refused frame is a fault: a fullscreen uniform app, a
    # lock view, or DPMS-off can all legitimately produce a flat frame.
    #
    # Generated in-guest with stdlib python3 only. ImageMagick is a HOST
    # dependency of the capture gate and is not guaranteed in the VM, and a
    # binary asset in the repo cannot be reviewed in a diff. The pattern (plain
    # diagonal + orthogonal grid, no text, no fonts) is the same one
    # scripts/vm/assets/make-test-wallpaper.sh documents and regenerates (that
    # script extracts and runs THIS generator rather than reimplementing it;
    # the sigma figures below are measured by the gate tests):
    # full-frame sigma ~0.098 against a FRAME_FLAT_SIGMA of 0.01. The pattern
    # is 64px-periodic in both axes, so any axis-aligned, unscaled 64x64 crop
    # contains each pair of residues exactly once and therefore has the
    # histogram of ONE COMPLETE PERIOD -- which is only APPROXIMATELY the whole
    # frame's, because 800 is 12.5 periods, not an integer: a tile is
    # 496/4096 rule pixels (0.121094) against the frame's 123400/1024000
    # (0.120508). Close enough that a crop and the frame read the same to the
    # gate, which is why a window covering most of the screen leaves the
    # remainder just as legible. 64px is the period, a design
    # choice; it is NOT a measured claim about how much desktop a real
    # scenario leaves uncovered, and a remnant smaller than one period or a
    # scaled/filtered capture carries no such guarantee. A solid colour would
    # NOT do: qdshell's solidColor default (#1a1a2e) measures sigma 0 and the
    # gate would refuse a healthy desktop as FLAT.
    install -d -m 0755 -o admin -g users /home/admin/Pictures/Wallpapers
    runuser -u admin -- python3 - /home/admin/Pictures/Wallpapers/qdistro-test-pattern.png <<'WPEOF'
import struct, zlib, sys

W, H, T = 1280, 800, 64
BASE = (0x18, 0x20, 0x32)
RULE = (0x5a, 0x6d, 0x99)

def on_rule(x, y):
    u, v = x % T, y % T
    return (abs(u - v) < 2 or abs(u + v - (T - 1)) < 2
            or abs(v - T // 2) < 1 or abs(u - T // 2) < 1)

rows = bytearray()
for y in range(H):
    rows.append(0)
    for x in range(W):
        rows += bytes(RULE if on_rule(x, y) else BASE)

def chunk(tag, data):
    c = tag + data
    return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

open(sys.argv[1], "wb").write(
    b"\x89PNG\r\n\x1a\n"
    + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    + chunk(b"IEND", b""))
WPEOF
    chown admin:users /home/admin/Pictures/Wallpapers/qdistro-test-pattern.png 2>/dev/null || true
}

# 8b. Display-resolution fix — make virtio_gpu the DRM driver instead
#     of the generic vesa/simple framebuffer.
#     qdwin's weston drm-backend wants virtio_gpu to win over simpledrm.
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

# greetd stays masked (it would grab the DRM seat and block admin's lingering
# user manager — see fresh-vm-bootstrap.sh).
systemctl mask greetd.service 2>/dev/null || true

# 8c. Keep the qdwin compositor + qdshell session that fresh-vm-bootstrap.sh
#     staged and started under admin's lingering user manager (deploy-named
#     units qdwin-compositor.service / qdshell.service, pulled in by
#     qdwin-session.target — see install-qdwin-session-for-vm.sh). Ensure the
#     target is enabled + (re)started (idempotent — bootstrap normally already
#     has wayland-1 up) and wait for the wayland-1 socket.
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
#     tty1 with a stock getty@tty1, and getty's start-time TTY reset reverts the K_OFF that
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

# DESKTOP WALLPAPER: see install_test_wallpaper above (qdshell reads
# ~/Pictures/Wallpapers, where it installs the pattern).
install_test_wallpaper

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
# compositor restarts on-failure, so give it time.
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

# Confirm the surface the scenarios need:
echo "--- post-install state (qdwin) ---"
ls -l /usr/local/bin/qdistro-test-permission /usr/local/bin/qdistro-start-admin-app \
      /usr/local/bin/qdistro-admin-approval-app
ls -l /usr/local/bin/qdistro-admin-tui /usr/local/lib/qdistro/admin-tui/qdistro_admin_tui.py
ls -l /etc/dracut.conf.d/90-qdistro-virtio-gpu.conf 2>&1 \
    || echo "WARN: virtio-gpu dracut conf not installed"
grep -H "simpledrm_platform_driver_init" /etc/default/grub 2>&1 \
    || echo "WARN: simpledrm blacklist not in GRUB cmdline"
# The compositor socket, the vendored libweston the clamp/popup-grab paths
# need, and the live qs (qdshell) + weston processes.
ls -l /run/user/1000/wayland-1 2>&1 || echo "WARN: wayland-1 not up"
ls -ld /usr/libexec/qdistro/qdwin-libweston/lib64 2>&1 \
    || echo "WARN: vendored libweston not installed (popup clamp/grab will be absent)"
runuser -l admin -c 'systemctl --user is-active qdwin-compositor.service qdshell.service' 2>&1 || true
grep -qx 'renderer=pixman' /home/admin/weston.ini \
    || { echo "ERROR: qdwin GUI test profile is not using pixman"; exit 1; }
grep -qx 'mode=1280x800@60' /home/admin/weston.ini \
    || { echo "ERROR: qdwin GUI test profile is not using its fixed 1280x800 mode"; exit 1; }
ps -ef | grep -E "[w]eston|[q]s -p|quickshell" | head -5 || true
echo "--- done ---"
POSTBOOT
)
$VMEXEC "$VM" "echo $B64 | base64 -d | VM_PASSWORD='$VM_PASSWORD' GOLDEN_CLONE='$GOLDEN_CLONE' bash" >&2

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
#   running geometry-sensitive scenarios.

echo "[gui-spin] step 3/3: ready (qdwin session). Scenarios can target VMNAME=$VM" >&2
# Final stdout line: the VM name, matching spin-test-vm.sh's contract.
echo "$VM"
