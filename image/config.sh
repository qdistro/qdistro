#!/bin/bash
# qdistro kiwi config script.
#
# Runs inside the image chroot AFTER packages install, BEFORE the
# rootfs is packed into the OEM disk image.
#
# Layout assumed (build.sh rsyncs these in via the `root/` overlay):
#   /root/qdistro-src/qdistro/
#   /root/qdistro-src/qdwin/
#   /root/qdistro-src/qdshell/
#
# This is the same /root/qdistro-src layout that
# qdistro/scripts/vm/fresh-vm-bootstrap.sh expects, so we reuse the
# project's own installers (install-broker-for-qdwin.sh etc.) verbatim
# rather than re-implement them.

set -euxo pipefail

. /.kconfig
. /.profile

echo "[qdistro-image] kiwi config.sh: $kiwi_iname-$kiwi_iversion"

SRC=/root/qdistro-src
QD="$SRC/qdistro"

#-- 0. Branding ---------------------------------------------------------------
# Override openSUSE's /etc/os-release with the qdistro one we baked into
# the overlay (root/etc/os-release.qdistro).
if [ -f /etc/os-release.qdistro ]; then
    rm -f /etc/os-release
    mv /etc/os-release.qdistro /etc/os-release
fi

#-- 1. Defensive masking (mirrors fresh-vm-bootstrap.sh §0) -------------------
# jeos-firstboot fights us for tty1 and blocks multi-user.target on
# openSUSE images. Mask it before greetd takes over.
systemctl mask jeos-firstboot.service jeos-firstboot-snapshot.service 2>/dev/null || true

#-- 2. Run-level / hostname ---------------------------------------------------
echo "qdistro" > /etc/hostname

#-- 3. SSH host keys + enable sshd (for VM smoke-testing) ---------------------
ssh-keygen -A
# Best-effort enables: seatd ships no service unit on Tumbleweed (greetd
# starts it on demand), some other units may not be present yet.
for u in sshd.service NetworkManager.service auditd.service ; do
    systemctl enable "$u" || echo "[qdistro-image] WARN: enable $u failed (unit missing?)"
done

#-- 4. Sudo for admin ---------------------------------------------------------
install -m 0440 /dev/stdin /etc/sudoers.d/99-admin <<<'admin ALL=(ALL) NOPASSWD: ALL'

#-- 5. Build qdwin (libweston shell plugin) ----------------------------------
echo "[qdistro-image] building qdwin..."
cd "$SRC/qdwin"
meson setup build --wipe --prefix=/usr
meson compile -C build
meson install -C build

#-- 6. Build qdistro C daemons (audisp / forward / nested-pixelfeed / etc) ----
echo "[qdistro-image] building qdistro daemons..."
cd "$QD/daemons"
meson setup build --wipe --prefix=/usr
meson compile -C build
meson install -C build

#-- 7. Build qdshell qml-plugin (Qdistro.Qdwin binding) -----------------------
echo "[qdistro-image] building qdshell qml-plugin..."
cd "$SRC/qdshell"
meson setup build --wipe --prefix=/usr || echo "[qdistro-image] WARN: qdshell meson setup failed; install-qdwin-session will use stubs"
meson compile -C build || true

#-- 8. Install Python services + dbus policies + systemd units ----------------
echo "[qdistro-image] installing python modules + units..."
cd "$QD"
INSTALLERS=(
    "scripts/install/install-broker-for-qdwin.sh       $QD/broker"
    "scripts/install/install-polkit-agent-for-vm.sh    $QD/polkit"
    "scripts/install/install-pwd-for-vm.sh             $QD/pwd"
    "scripts/install/install-qsu-for-vm.sh             $QD/qsu"
    "scripts/install/install-browser-bridge-for-vm.sh  $QD/browser_bridge"
    "scripts/install/install-phone-for-vm.sh           $QD/phone"
    "scripts/install/install-print-proxy-for-vm.sh     $QD/print"
    "scripts/install/install-recall-for-vm.sh          $QD"
    "scripts/install/install-snapshots-for-vm.sh       $QD/snapshots"
)
for entry in "${INSTALLERS[@]}"; do
    # shellcheck disable=SC2086
    set -- $entry
    installer="$1"
    src_dir="$2"
    if [ -x "$installer" ]; then
        echo "[qdistro-image]   running $(basename "$installer") <- $src_dir"
        bash "$installer" "$src_dir" || echo "[qdistro-image]   WARN: $installer failed; continuing"
    fi
done

#-- 9. SELinux policy modules (permissive) ------------------------------------
sed -i 's/^SELINUX=.*/SELINUX=permissive/' /etc/selinux/config 2>/dev/null || true
for pol in selinux/broker selinux/pwd selinux/tier1; do
    if [ -d "$QD/$pol" ] && [ -x "$QD/$pol/install-policy.sh" ]; then
        (cd "$QD/$pol" && bash install-policy.sh) || echo "[qdistro-image]   WARN: $pol policy install failed"
    fi
done

#-- 10. qdwin user session for admin (weston + qdshell user units) ------------
# install-qdwin-session-for-vm.sh uses `loginctl enable-linger` and
# `runuser -l admin -c systemctl --user enable ...` — both of which
# require a live system bus / user-manager and so abort in a kiwi chroot.
# Shadow them with chroot-safe shims for the duration of the call.
echo "[qdistro-image] installing qdwin session (with chroot shims)..."
SHIMS="$(mktemp -d)"
cat > "$SHIMS/loginctl" <<'SHIM'
#!/bin/bash
# kiwi-chroot shim: emulate `loginctl enable-linger <user>` via the
# on-disk marker file instead of poking the (absent) logind socket.
case "${1:-}" in
    enable-linger)
        install -d -m 0755 /var/lib/systemd/linger
        : > "/var/lib/systemd/linger/${2:-admin}"
        ;;
    *) exit 0 ;;
esac
SHIM
cat > "$SHIMS/runuser" <<'SHIM'
#!/bin/bash
# kiwi-chroot shim: the upstream call is
#   runuser -l admin -c 'systemctl --user enable noctalia-session.service noctalia-shell.service'
# We emulate it by writing the symlinks directly under admin's
# default.target.wants.
target=/home/admin/.config/systemd/user/default.target.wants
mkdir -p "$target"
for u in noctalia-session.service noctalia-shell.service ; do
    src="/home/admin/.config/systemd/user/$u"
    [ -f "$src" ] || continue
    ln -sf "../$u" "$target/$u"
done
chown -R admin:users /home/admin/.config/systemd 2>/dev/null || true
SHIM
chmod +x "$SHIMS"/loginctl "$SHIMS"/runuser
PATH="$SHIMS:$PATH" bash "$QD/scripts/install/install-qdwin-session-for-vm.sh" "$SRC/qdshell" \
    || echo "[qdistro-image] WARN: qdwin-session install still failed (see log above)"
rm -rf "$SHIMS"

#-- 11. greetd autologin ------------------------------------------------------
install -d -m 0755 /etc/greetd
cat > /etc/greetd/config.toml <<'EOF'
[terminal]
vt = 1

[default_session]
command = "agreety --cmd /bin/bash"
user = "greeter"

[initial_session]
command = "/bin/bash --login"
user = "admin"
EOF
systemctl enable greetd.service
systemctl set-default graphical.target

#-- 12. Cleanup ---------------------------------------------------------------
# Keep /root/qdistro-src on the image — design philosophy is
# "LLM-modifiable, inspect-and-edit Python everywhere." Drop only the
# build artifacts that bloat the image.
find "$SRC" -type d -name build -prune -exec rm -rf {} + 2>/dev/null || true
find "$SRC" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "$SRC" -type d -name .git -prune -exec rm -rf {} + 2>/dev/null || true

# Note: baseCleanMount and suseConfig are removed in kiwi 10; they
# now exit non-zero as deprecation stubs. Cleanup is kiwi's job.

echo "[qdistro-image] config.sh complete."
exit 0
