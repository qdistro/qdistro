#!/usr/bin/env bash
# qdistro-session-provision — the post-package step that makes the
# packaged session operable on an installed system.
#
# The qdistro-session RPM ships the declarative payload (greetd config,
# user units, templates). Per-machine state the upstream install scripts
# establish is performed here, once, by the installer/post step
# (Agama `post` script or a manual run after `zypper in qdistro-desktop`).
# Mirrors, in order:
#   - install-qdwin-session-for-vm.sh  (seat group, admin groups, linger,
#                                        ~admin/weston.ini + xwayland probe)
#   - image/config.sh                  (_greeter groups)
#   - qdistro-bootstrap.sh             (greetd enable + graphical.target)
#
# Run as root. Idempotent. admin (uid 1000) must already exist.
set -euo pipefail

if [ "$(id -u)" != 0 ]; then
    echo "qdistro-session-provision: must run as root" >&2
    exit 2
fi
if ! id admin >/dev/null 2>&1; then
    echo "qdistro-session-provision: user 'admin' missing — create it first" >&2
    exit 2
fi

# `seat` lets libseat's seatd backend open a seat for weston under admin's
# lingering user manager (no logind seat of its own). The seatd RPM ships
# no group, so create it when absent — same as the install script.
getent group seat >/dev/null || groupadd -r seat
usermod -aG video,input,render,seat admin
# Linger: the flag file is what `loginctl enable-linger` creates and is what
# logind reads at boot — write it directly so this also works inside a
# chroot (Agama post scripts, image builds), where loginctl reaches the
# HOST's logind and fails with "No such process" for a user that only
# exists in the target. On a live root also call loginctl for the
# immediate effect; ignore its failure.
install -d -m 0755 /var/lib/systemd/linger
touch /var/lib/systemd/linger/admin
loginctl enable-linger admin 2>/dev/null || true

# _greeter system user — greetd's [default_session].user convention; runs
# the qdgreeter UI without admin privileges. Created here the same way
# qdistro-bootstrap.sh/image/config.sh do: NON-LOGIN (nologin), NON-HOME.
# (The distro's system-user-greeter provides a different `greeter` user.)
if ! getent passwd _greeter >/dev/null; then
    useradd --system --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin _greeter
else
    usermod --shell /usr/sbin/nologin --home /nonexistent _greeter
fi
# EGLFS reads input devices directly; qdgreeter handles Ctrl+Alt+Fx VT
# switching itself. Skip groups that don't exist on the distro; a failing
# usermod on an existing group is a real error and aborts.
for g in video render input tty; do
    if getent group "$g" >/dev/null; then
        usermod -aG "$g" _greeter
    fi
done

# ~admin/weston.ini from the packaged template — the compositor unit runs
# `weston --config=%h/weston.ini`. XWayland probe mirrors upstream:
# prefer the vendored module (the stripped vendored src never ships one;
# kept for parity), else the distro libweston-16 module; flip
# xwayland=false -> true when a module exists.
install -m 0644 -o admin -g "$(id -gn admin)" \
    /usr/share/qdistro/weston.ini /home/admin/weston.ini
for _cand in /usr/libexec/qdistro/qdwin-libweston/lib64/libweston-16/xwayland.so \
             /usr/lib64/libweston-16/xwayland.so; do
    if [ -f "$_cand" ]; then
        sed -i 's|^xwayland=false$|xwayland=true|' /home/admin/weston.ini
        break
    fi
done

# Boot into the greeter on the next boot; seatd backs libseat for the
# compositor running under admin's lingering user manager.
# A masked greetd (e.g. the ci kiwi profile masks it so admin's user
# manager starts the compositor) must be unmasked before enable.
systemctl unmask greetd.service 2>/dev/null || true
systemctl enable greetd.service
systemctl enable seatd.service
systemctl set-default graphical.target

# Keep the compositor's VT exclusively the compositor's — a getty taking
# it reverts seatd's K_OFF and leaks locked-screen keystrokes into the
# kernel console (see harden-compositor-vt's header). Upstream treats a
# failure here as fatal to the install; so do we (set -e). Inside a
# chroot (Agama post, image builds) pass --offline — the helper
# corroborates and refuses it on a live root. Detection covers Agama's
# chroot, which shares /run with the live system (so /run/systemd/system
# EXISTS and loginctl reaches the host logind): being chrooted is
# offline, systemd-or-not.
_offline=0
if [ ! -d /run/systemd/system ]; then
    _offline=1
elif [ "$(stat -c %d:%i /)" != "$(stat -c %d:%i /proc/1/root/. 2>/dev/null)" ]; then
    _offline=1
fi
_vt_args=()
if [ "$_offline" = 1 ]; then
    _vt_args=(--offline)
fi
/usr/libexec/qdistro/harden-compositor-vt "${_vt_args[@]}" /etc/greetd/config.toml

echo "qdistro-session-provision: done — boot reaches greetd -> qdgreeter -> qdwin session"
