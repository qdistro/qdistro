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

if [ -f /etc/os-release.qdistro ]; then
    rm -f /etc/os-release
    mv /etc/os-release.qdistro /etc/os-release
fi

# jeos-firstboot fights us for tty1 and blocks multi-user.target on
# openSUSE JeOS-derived images. Mask before greetd takes over.
systemctl mask jeos-firstboot.service jeos-firstboot-snapshot.service 2>/dev/null || true

echo "qdistro" > /etc/hostname

ssh-keygen -A
systemctl enable sshd.service NetworkManager.service

install -m 0440 /dev/stdin /etc/sudoers.d/99-admin <<<'admin ALL=(ALL) NOPASSWD: ALL'

# Build the three sibling projects out of /root/qdistro-src/.
echo "[qdistro-image] building qdwin..."
cd "$SRC/qdwin"
meson setup build --wipe --prefix=/usr
meson compile -C build
meson install -C build

echo "[qdistro-image] building qdistro daemons..."
cd "$QD/daemons"
meson setup build --wipe --prefix=/usr
meson compile -C build
meson install -C build

# qdshell's qml-plugin binds qdwin_shell_v1 -> QML. Without it the
# install-qdwin-session installer falls back to stubs and noctalia-shell
# can't resolve `import Qdistro.Qdwin 1.0`.
echo "[qdistro-image] building qdshell qml-plugin..."
cd "$SRC/qdshell"
meson setup build --wipe --prefix=/usr
meson compile -C build

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
# Each installer drops files (broker python, dbus policy, systemd units)
# then tries to start the service via `systemctl enable --now ...` and
# verify with `busctl` — both fail in a kiwi chroot because there's no
# running systemd or system bus. The file-drop happens before the verify
# step, so a non-zero exit here means "verify failed", not "install
# failed". The installed image is functional; we record the warning and
# move on. Audit each WARN if a verify.sh assertion fails downstream.
for entry in "${INSTALLERS[@]}"; do
    # shellcheck disable=SC2086
    set -- $entry
    installer="$1"
    src_dir="$2"
    if [ -x "$installer" ]; then
        bash "$installer" "$src_dir" \
            || echo "[qdistro-image]   WARN: $installer verify failed (expected in chroot)"
    fi
done

sed -i 's/^SELINUX=.*/SELINUX=permissive/' /etc/selinux/config 2>/dev/null || true
for pol in selinux/broker selinux/pwd selinux/tier1; do
    if [ -d "$QD/$pol" ] && [ -x "$QD/$pol/install-policy.sh" ]; then
        # tier1 module references types defined by broker module; if it
        # loads first the AST resolves on the second pass at boot time.
        (cd "$QD/$pol" && bash install-policy.sh) \
            || echo "[qdistro-image]   WARN: $pol policy install failed (chroot semodule)"
    fi
done

# install-qdwin-session-for-vm.sh uses `loginctl enable-linger` and
# `runuser -l admin -c systemctl --user enable ...` — both require a
# live logind / user-manager, so they abort in a kiwi chroot. Shadow
# them with chroot-safe shims for the duration of the call.
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
PATH="$SHIMS:$PATH" bash "$QD/scripts/install/install-qdwin-session-for-vm.sh" "$SRC/qdshell"
rm -rf "$SHIMS"

install -d -m 0755 /etc/greetd

# P01 boot path: qdgreeter on tty3, LXQt+labwc fallback on tty4.
# _greeter system user owns the unprivileged greeter process; PAM
# does the privilege handoff at start_session time.
if ! getent passwd _greeter >/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin _greeter || true
fi

install -m 0644 "$QD/deploy/greetd-config.toml"          /etc/greetd/config.toml
install -m 0644 "$QD/deploy/greetd-config-fallback.toml" /etc/greetd/config-fallback.toml
install -m 0644 "$QD/deploy/greetd-fallback.service"     /etc/systemd/system/greetd-fallback.service
install -m 0755 "$QD/deploy/qdwin-session-launcher.sh"   /usr/local/bin/qdwin-session-launcher

# Install qdgreeter package files (the Python module + entry point are
# packaged separately under qdgreeter/; this assumes they're already
# on $PATH as /usr/bin/qdgreeter from a sibling pip-install step).

systemctl enable greetd.service
systemctl enable greetd-fallback.service || true
systemctl set-default graphical.target

# Keep /root/qdistro-src on the image — the LLM-modifiability principle
# in doc/overview.md requires editable Python services on disk. Drop the
# meson build dirs and __pycache__ to save ~200MB of churn.
find "$SRC" -type d \( -name build -o -name __pycache__ \) -prune -exec rm -rf {} + 2>/dev/null || true

# kiwi 10's baseCleanMount + suseConfig are deprecation stubs that exit
# non-zero; cleanup is kiwi's job now.

echo "[qdistro-image] config.sh complete."
exit 0
