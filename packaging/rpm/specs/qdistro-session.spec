Name:           qdistro-session
Version:        0.1.0
Release:        1%{?dist}
Summary:        qdistro session glue — greetd config, user units, launcher
License:        GPL-3.0-or-later
URL:            https://github.com/qdistro/qdistro
Source:         %{name}-%{version}.tar.gz
# Post-package provisioning step (users/groups/linger, ~admin/weston.ini,
# greetd enable) — run once by the installer/Agama post script.
Source1:        qdistro-session-provision.sh
# Compositor-VT getty mask helper (verbatim copy of
# scripts/install/harden-compositor-vt.sh — keep in sync).
Source2:        harden-compositor-vt.sh
Requires:       greetd
# qdistro owns /etc/greetd/config.toml — the greetd -> qdgreeter wiring.
# greetd Requires the `greetd-branding` capability; provide it and
# conflict with the distro branding package that owns the same file, the
# same pattern greetd-branding-upstream/-openSUSE use against each other.
Provides:       greetd-branding
Conflicts:      greetd-branding-openSUSE
Conflicts:      greetd-branding-upstream
# _greeter is a qdistro-specific NON-LOGIN user created by
# qdistro-session-provision (the distro's system-user-greeter provides
# a different `greeter` user and is pulled by greetd itself).
# libseat's seatd backend opens a seat for weston under admin's
# lingering user manager (no logind seat of its own).
Requires:       seatd
Requires:       dbus-1
# qdshell.service wraps its ExecStart in /usr/bin/dbus-run-session —
# that binary lives in dbus-1-daemon, not dbus-1.
Requires:       dbus-1-daemon
Requires:       qdwin
Requires:       qdistro-libweston-vendored
Requires:       qdshell
Requires:       qdgreeter
Requires:       qdlocker
BuildArch:      noarch

%description
Session-level deployment payload (repo deploy/ directory): the tty3
greetd production path plus the admin user's systemd session that runs
the qdwin compositor and the qdshell desktop.

Ships:
  /etc/greetd/config.toml                              (greetd -> qdgreeter)
  /etc/systemd/system/greetd.service.d/10-qdistro-hardening.conf
  /usr/lib/systemd/user/qdwin-session.target           (compositor + shell)
  /usr/lib/systemd/user/qdwin-compositor.service
  /usr/lib/systemd/user/qdshell.service
  /usr/local/bin/qdwin-session-launcher                (post-PAM session cmd;
    path is hardcoded in qdgreeter's fallback and the greetd config)
  /etc/qdistro/{locker,broker-hardened,template-retention}.conf/toml
  /etc/systemd/logind.conf.d/90-qdistro-lid-lock.conf  (lid -> Session.Lock)
  /etc/polkit-1/rules.d/50-qdistro-locker-idle.rules   (admin may Lock
    its own logind session; install-qdwin-session-for-vm.sh heredoc)
  /usr/share/qdistro/weston.ini                        (template the
    post-install step materializes as ~admin/weston.ini)
  /usr/share/icons/default/index.theme                 (libXcursor
    "default" theme -> Adwaita; the pointer is invisible without it)
  /usr/libexec/qdistro/qdistro-session-provision       (run once
    post-install: seat group, admin/_greeter groups, linger,
    ~admin/weston.ini + xwayland probe, greetd enable)

Not shipped here (control-plane payload, separate round): the broker,
session-manager, polkit agent, portal backend, hook executor, browser
bridge/daemons, and SELinux modules. Per-user state (~admin/weston.ini,
seat/groups/linger, greetd enable) is performed post-install by
qdistro-session-provision — not in %post, so the step stays visible to
the Agama profile/manual install path.

%prep
%autosetup

%install
install -d %{buildroot}%{_sysconfdir}/greetd \
           %{buildroot}%{_sysconfdir}/systemd/system/greetd.service.d \
           %{buildroot}%{_sysconfdir}/systemd/logind.conf.d \
           %{buildroot}%{_sysconfdir}/qdistro \
           %{buildroot}%{_sysconfdir}/polkit-1/rules.d \
           %{buildroot}%{_userunitdir} \
           %{buildroot}%{_prefix}/local/bin \
           %{buildroot}%{_datadir}/qdistro \
           %{buildroot}%{_datadir}/icons/default \
           %{buildroot}%{_libexecdir}/qdistro

install -m 0644 greetd-config.toml \
    %{buildroot}%{_sysconfdir}/greetd/config.toml
install -m 0644 greetd-hardening.conf \
    %{buildroot}%{_sysconfdir}/systemd/system/greetd.service.d/10-qdistro-hardening.conf
install -m 0644 qdwin-session.target qdshell.service \
    %{buildroot}%{_userunitdir}/
# qdwin-compositor.service: the deploy unit's static WESTON_MODULE_MAP is the
# stale variant — it maps only drm+gl to the vendored tree and the rest to
# /usr/lib64 (mixing distro backends into the vendored core; the ABI is
# internal). install-qdwin-session-for-vm.sh renders the map dynamically and
# maps EVERY module to the vendored dir. Reproduce that here — the vendored
# package is a hard Requires — except xwayland.so, which the stripped
# vendored src does not build (upstream's own probe falls back to the distro
# module; it is ABI-safe because Requires pins the same weston major).
sed -e 's|=/usr/lib64/libweston-16/|=/usr/libexec/qdistro/qdwin-libweston/lib64/libweston-16/|g' \
    -e 's|xwayland.so=/usr/libexec/qdistro/qdwin-libweston/lib64/libweston-16/xwayland.so|xwayland.so=/usr/lib64/libweston-16/xwayland.so|' \
    qdwin-compositor.service \
    > %{buildroot}%{_userunitdir}/qdwin-compositor.service
chmod 0644 %{buildroot}%{_userunitdir}/qdwin-compositor.service
install -m 0755 qdwin-session-launcher.sh \
    %{buildroot}%{_prefix}/local/bin/qdwin-session-launcher
install -m 0644 etc/qdistro/broker-hardened.conf etc/qdistro/locker.conf \
    %{buildroot}%{_sysconfdir}/qdistro/
install -m 0644 etc/qdistro/template-retention.toml \
    %{buildroot}%{_sysconfdir}/qdistro/template-retention.toml
install -m 0644 systemd/logind/90-qdistro-lid-lock.conf \
    %{buildroot}%{_sysconfdir}/systemd/logind.conf.d/90-qdistro-lid-lock.conf

# 50-qdistro-locker-idle.rules — verbatim the heredoc
# install-qdwin-session-for-vm.sh writes: admin may Lock its own logind
# session (the s103 lid-close path drives org.freedesktop.login1
# .lock-sessions as admin; Tumbleweed's default is auth_admin).
cat > %{buildroot}%{_sysconfdir}/polkit-1/rules.d/50-qdistro-locker-idle.rules <<'EOF'
// qdistro: admin may Lock its own logind session without auth.
// Mirrors HandleLidSwitch=lock semantics for headless test VMs.
polkit.addRule(function(action, subject) {
    if (action.id === "org.freedesktop.login1.lock-sessions" &&
        subject.user === "admin") {
        return polkit.Result.YES;
    }
    return undefined;
});
EOF

# weston.ini template — the qdwin-compositor unit runs
# `weston --config=%h/weston.ini`, so the post-install/Agama step copies
# this to ~admin/weston.ini. Contents mirror the proven VM template in
# install-qdwin-session-for-vm.sh (Virtual-1 @1920x1080; xwayland=false,
# flipped to true when Xwayland is present).
cat > %{buildroot}%{_datadir}/qdistro/weston.ini <<'EOF'
[core]
backend=drm-backend.so,pipewire-backend.so
shell=/usr/lib64/weston/qdwin-shell.so
renderer=gl
modules=
xwayland=false
idle-time=0
vt-switching=false

[shell]
locking=false
client=

[output]
name=Virtual-1
mode=1920x1080@60

[pipewire]
num-outputs=2
EOF

# The XDG "default" cursor theme alias — libXcursor resolves theme
# "default" when XCURSOR_THEME is unset; qdwin's cursor-shape preload and
# qdistro-cursor-sprites both ask for it by NULL name. Neither
# adwaita-icon-theme nor xcursor-themes ships default/index.theme on
# Tumbleweed; without it the pointer is invisible (upstream treats its
# absence as fatal for the image). Content = image/root/.../index.theme.
cat > %{buildroot}%{_datadir}/icons/default/index.theme <<'EOF'
[Icon Theme]
Name=Default
Inherits=Adwaita
EOF

install -m 0755 %{SOURCE1} \
    %{buildroot}%{_libexecdir}/qdistro/qdistro-session-provision
install -m 0755 %{SOURCE2} \
    %{buildroot}%{_libexecdir}/qdistro/harden-compositor-vt

# seatd: the distro package ships only the binary — qdistro carries the
# unit (verbatim from fresh-vm-bootstrap.sh).
install -d %{buildroot}%{_unitdir}
cat > %{buildroot}%{_unitdir}/seatd.service <<'EOF'
[Unit]
Description=Seat management daemon
Documentation=man:seatd(1)
After=systemd-user-sessions.service
Before=user@.service

[Service]
Type=simple
ExecStart=/usr/bin/seatd -g seat
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
EOF

%check
test -f %{buildroot}%{_datadir}/icons/default/index.theme
test -x %{buildroot}%{_libexecdir}/qdistro/qdistro-session-provision
# The compositor unit must not load distro libweston modules — every map
# entry except xwayland.so must resolve inside the vendored tree. Parse
# every entry: names are `;`-separated (only the first follows the `=`)
# and may contain digits (x11-backend.so).
_map=$(grep -o 'WESTON_MODULE_MAP=[^ ]*' \
    %{buildroot}%{_userunitdir}/qdwin-compositor.service)
_bad=$(echo "$_map" | tr ';' '\n' | sed 's/^WESTON_MODULE_MAP=//' | \
    grep -v '^xwayland\.so=' | \
    grep -v '=/usr/libexec/qdistro/qdwin-libweston/lib64/libweston-16/' || true)
if [ -n "$_bad" ]; then
    echo "non-vendored libweston module in map: $_bad" >&2
    exit 1
fi

%files
%dir %{_sysconfdir}/greetd
%config(noreplace) %{_sysconfdir}/greetd/config.toml
%{_sysconfdir}/systemd/system/greetd.service.d/
%{_sysconfdir}/systemd/logind.conf.d/
%dir %{_sysconfdir}/qdistro
%config %{_sysconfdir}/qdistro/broker-hardened.conf
%config %{_sysconfdir}/qdistro/locker.conf
%config %{_sysconfdir}/qdistro/template-retention.toml
%{_sysconfdir}/polkit-1/rules.d/50-qdistro-locker-idle.rules
%{_userunitdir}/qdwin-session.target
%{_userunitdir}/qdwin-compositor.service
%{_userunitdir}/qdshell.service
%{_unitdir}/seatd.service
%{_prefix}/local/bin/qdwin-session-launcher
%{_datadir}/qdistro/weston.ini
%{_datadir}/icons/default/
%{_libexecdir}/qdistro/qdistro-session-provision
%{_libexecdir}/qdistro/harden-compositor-vt
