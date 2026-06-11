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

# Sudoers policy is profile-gated. This config.sh bakes a RELEASE image by
# default, which must NOT ship `admin ALL=(ALL) NOPASSWD: ALL` — a baked-in
# passwordless-root rule on every shipped disk is exactly the escape hatch the
# hardening review flagged. Cross-uid privileged actions on a release image go
# through qsu / the broker's scoped approval; admin keeps password-required
# sudo via wheel membership. Set QDISTRO_PROFILE=dev when baking a disposable
# developer image to restore the passwordless rule.
QDISTRO_IMAGE_PROFILE="${QDISTRO_PROFILE:-release}"
if [ "$QDISTRO_IMAGE_PROFILE" = dev ]; then
    install -m 0440 /dev/stdin /etc/sudoers.d/99-admin <<<'admin ALL=(ALL) NOPASSWD: ALL'
    echo "[qdistro-image] WARN: dev profile — baked passwordless sudoers (admin NOPASSWD: ALL); NOT for release"
else
    rm -f /etc/sudoers.d/99-admin
    echo "[qdistro-image] hardened profile ($QDISTRO_IMAGE_PROFILE): no passwordless sudoers baked (admin uses password-required sudo; cross-uid via qsu/broker)"
fi

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
    "scripts/install/install-media-for-vm.sh           $QD/media"
    "scripts/install/install-browser-bridge-for-vm.sh  $QD/browser_bridge"
    "scripts/install/install-portal-backend-for-vm.sh  $QD"
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
for pol in selinux/broker selinux/pwd selinux/session_manager selinux/tier1; do
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
    useradd --system --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin _greeter || true
else
    usermod --shell /usr/sbin/nologin --home /nonexistent _greeter 2>/dev/null || true
fi
for g in video render input tty; do
    getent group "$g" >/dev/null && usermod -aG "$g" _greeter || true
done

install -m 0644 "$QD/deploy/greetd-config.toml"          /etc/greetd/config.toml
install -m 0644 "$QD/deploy/greetd-config-fallback.toml" /etc/greetd/config-fallback.toml
install -m 0644 "$QD/deploy/greetd-fallback.service"     /etc/systemd/system/greetd-fallback.service

# systemd hardening drop-in for the distro-packaged greetd.service.
if [ -f "$QD/deploy/greetd-hardening.conf" ]; then
    install -d -m 0755 /etc/systemd/system/greetd.service.d
    install -m 0644 "$QD/deploy/greetd-hardening.conf" \
        /etc/systemd/system/greetd.service.d/10-qdistro-hardening.conf
fi
install -m 0755 "$QD/deploy/qdwin-session-launcher.sh"   /usr/local/bin/qdwin-session-launcher

# qdgreeter (boot greeter) + qdlocker (screen locker) are part of the
# production boot/session path:
#   - greetd-config.toml execs /usr/bin/qdgreeter (finding #19).
#   - qdwin-session.target Wants= qdlocker.service (finding #16).
# Earlier this config.sh assumed both were on $PATH from a "sibling
# pip-install step" that the image build never ran, so greetd booted to a
# missing greeter. Build them from the synced overlay here (build.sh now
# rsyncs qdgreeter/ + qdlocker/). --prefix=/usr lands the qdgreeter /
# qdlocker entry points in /usr/bin so greetd and the session units find
# them. --no-deps: the Python runtime deps (PyQt6, dbus_next, python-pam,
# pywayland) are RPM-installed via config.xml.
for pyapp in qdgreeter qdlocker; do
    if [ -f "$SRC/$pyapp/pyproject.toml" ]; then
        echo "[qdistro-image] pip installing $pyapp -> /usr ..."
        python3 -m pip install --break-system-packages --no-deps \
            --prefix=/usr "$SRC/$pyapp" \
            || echo "[qdistro-image]   WARN: pip install $pyapp failed"
    else
        echo "[qdistro-image]   WARN: $SRC/$pyapp not synced — $pyapp binary will be missing"
    fi
done

# REQUIRED gate: greetd is enabled to exec /usr/bin/qdgreeter, so a
# missing greeter binary is a hard image-build failure, not a silent
# fallback to a black login (finding #19).
if [ ! -x /usr/bin/qdgreeter ]; then
    echo "[qdistro-image] FATAL: /usr/bin/qdgreeter missing after pip install;" \
         "greetd would boot to a non-existent greeter. Aborting build." >&2
    exit 1
fi
echo "[qdistro-image] /usr/bin/qdgreeter present: $(command -v qdgreeter)"

# Production session units (qdwin-session.target + qdwin-compositor.service
# + qdshell.service) and the screen locker (qdlocker.service). The greeter
# launcher does `systemctl --user start qdwin-session.target`, so these
# MUST be installed and enabled for the admin user — the legacy
# install-qdwin-session-for-vm.sh above only writes the noctalia-* units,
# which the greeter never starts (findings #15, #16). Install both unit
# sets so the boot path the greeter drives actually resolves; the noctalia
# units stay for the GUI test harness that greps their names.
ADMIN_USER_UNITS=/home/admin/.config/systemd/user
install -d -o admin -g users -m 0755 "$ADMIN_USER_UNITS"
install -d -o admin -g users -m 0755 "$ADMIN_USER_UNITS/qdwin-session.target.wants"
for unit in qdwin-session.target qdwin-compositor.service qdshell.service; do
    install -m 0644 "$QD/deploy/$unit" "$ADMIN_USER_UNITS/$unit"
done
# qdlocker.service ships from the qdlocker repo (synced as $SRC/qdlocker).
# The upstream unit hardcodes ExecStart=/usr/local/bin/qdlocker, but the
# image pip-installs qdlocker with --prefix=/usr (above), which lands the
# console_script at /usr/bin/qdlocker. Nothing creates /usr/local/bin/qdlocker
# in the image, so copying the unit verbatim makes qdlocker.service die
# 203/EXEC at boot — the locker never starts. Render the unit through the
# SAME sed rewrite the from-source bootstrap uses so ExecStart matches the
# installed binary path (finding #16). Keep this rewrite as the authoritative
# fix even if the canonical unit is later made path-robust.
if [ -f "$SRC/qdlocker/systemd/qdlocker.service" ]; then
    sed 's|ExecStart=/usr/local/bin/qdlocker|ExecStart=/usr/bin/qdlocker|g' \
        "$SRC/qdlocker/systemd/qdlocker.service" \
        > "$ADMIN_USER_UNITS/qdlocker.service"
    chmod 0644 "$ADMIN_USER_UNITS/qdlocker.service"
    # Fail-closed: the rewritten ExecStart must point at the binary the image
    # actually installed. If the path does not resolve to an executable, the
    # unit would 203/EXEC at boot — abort the build rather than ship a locker
    # that silently never starts.
    locker_exec="$(sed -n 's|^ExecStart=\([^ ]*\).*|\1|p' \
        "$ADMIN_USER_UNITS/qdlocker.service" | head -n1)"
    if [ -z "$locker_exec" ] || [ ! -x "$locker_exec" ]; then
        echo "[qdistro-image] FATAL: qdlocker.service ExecStart ($locker_exec)" \
             "is not an executable in the image; qdlocker.service would" \
             "203/EXEC at boot. Aborting build." >&2
        exit 1
    fi
    echo "[qdistro-image] qdlocker.service ExecStart -> $locker_exec (rewritten from /usr/local/bin)"
else
    echo "[qdistro-image]   WARN: qdlocker.service not synced — locker absent from session"
fi
# Wire qdshell + qdlocker into qdwin-session.target.wants/ so the target
# pulls them in. (The target unit also declares Wants= for both, but the
# .wants symlinks are how `systemctl enable` would normally materialize
# that; we write them directly because there is no live user manager in
# the kiwi chroot.) qdwin-session.target itself is NOT enabled under
# default.target — the greeter's qdwin-session-launcher starts it
# explicitly (`systemctl --user start qdwin-session.target`) after PAM
# auth, which is the authoritative session-start path.
for unit in qdshell.service qdlocker.service; do
    [ -f "$ADMIN_USER_UNITS/$unit" ] || continue
    ln -sf "../$unit" "$ADMIN_USER_UNITS/qdwin-session.target.wants/$unit"
done

# Unify on ONE production session: the greeter launches
# qdwin-session.target, NOT the legacy noctalia-* units. The legacy
# installer enabled noctalia-session/-shell under default.target.wants,
# which would auto-start a SECOND compositor at login and race
# qdwin-session.target for the wayland-1 socket. Remove those auto-start
# symlinks so only the greeter-driven qdwin-session.target brings up the
# desktop. The noctalia unit FILES stay on disk for the GUI test harness
# that greps their names; they are simply not auto-enabled (finding #15).
for u in noctalia-session.service noctalia-shell.service; do
    rm -f "$ADMIN_USER_UNITS/default.target.wants/$u"
done
chown -R admin:users /home/admin/.config/systemd 2>/dev/null || true
echo "[qdistro-image] qdwin-session.target installed; qdshell + qdlocker wired into it; noctalia auto-start disabled"

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
