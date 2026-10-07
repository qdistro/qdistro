Name:           qdistro-desktop
Version:        0.1.0
Release:        1%{?dist}
Summary:        qdistro desktop — metapackage pulling the full session
License:        GPL-3.0-or-later
URL:            https://github.com/qdistro/qdistro
# Compositor + shell
Requires:       qdwin
Requires:       qdistro-libweston-vendored
Requires:       qdistro-daemons
Requires:       qdshell
Requires:       qdistro-session
# Greeter/locker
Requires:       qdgreeter
Requires:       qdlocker
# Control-plane surfaces packaged so far
Requires:       qdistro-admin
Requires:       qdistro-browser-bridge
Requires:       qdistro-presentation
# First-party apps
Requires:       qterminator
Requires:       qfileman
Requires:       qnotebook
Requires:       qdbrowser
# Runtime substrate (subset of scripts/vm/install-deps.sh QDISTRO_PKGS
# that a desktop needs; test-VM tooling like libvirt/bats/ydotool stays out)
Requires:       weston
Requires:       noctalia-qs
Requires:       greetd
Requires:       dbus-1
Requires:       pipewire
Requires:       wireplumber
# X clients need the XWayland binary; the compositor unit maps only the
# module (.so). Recommends (not Requires) — session-provision flips
# weston.ini xwayland=true when the module exists, so the soft dep keeps
# a fully headless-X install possible.
Recommends:     xwayland
Requires:       pipewire-tools
# Text on the greeter/locker/shell: with no font installed every glyph is
# a tofu box (image/config.xml installs exactly these two).
Requires:       dejavu-fonts
Requires:       google-noto-coloremoji-fonts
Requires:       Mesa
Requires:       Mesa-libEGL1
Requires:       Mesa-libGL1
Requires:       Mesa-dri
Requires:       qt6-wayland
Requires:       qt6-declarative-imports
Requires:       adwaita-icon-theme
Requires:       xcursor-themes
Requires:       wl-clipboard
Requires:       socat
Requires:       wayland-utils
Requires:       polkit
Requires:       libnotify-tools
Requires:       python314-pywayland
Requires:       python314-PyQt6
Requires:       python314-dbus-python
Requires:       python314-dbus_next
Requires:       python314-python-pam
BuildArch:      noarch

%description
Metapackage for a qdistro desktop install: `zypper in qdistro-desktop`
pulls the compositor (qdwin + vendored libweston), the qdshell
Quickshell desktop, the greetd/qdgreeter boot path, the screen locker,
the admin approval surfaces, and the first-party apps.

Not yet packaged (still bootstrap-installed on a real system): the
broker, session-manager, polkit agent, portal backend, hook executor,
browser bridge + WebExtensions, SELinux policy modules, qsu, and the
per-user state (~admin/weston.ini, admin/_greeter users, linger).

%build
# metapackage — nothing to build

%install
install -d %{buildroot}%{_datadir}/qdistro
echo '%{name}-%{version}-%{release}' > %{buildroot}%{_datadir}/qdistro/desktop-version

%files
%{_datadir}/qdistro/desktop-version
