Name:           qdshell
Version:        0.1.0
Release:        1%{?dist}
Summary:        qdistro desktop shell — Quickshell/QML on top of qdwin
License:        GPL-3.0-or-later
URL:            https://github.com/qdistro/qdistro
Source:         %{name}-%{version}.tar.gz
# QML dep scanning emits unsatisfiable requires for: the implicit `qs.*`
# project namespace (the QML tree itself — Quickshell resolves it relative
# to `-p`), `Qdistro.*` (our own plugin, same package), and `Quickshell.*`
# (compiled into the noctalia-qs binary — it ships no qmldir/provides).
# Disable .qml auto-requires entirely; the real runtime deps are the
# explicit Requires below (noctalia-qs, qt6-declarative-imports).
%global _disable_qml_requires 1
Provides:       qt6qmlimport(Qdistro.Qdwin.1)
BuildRequires:  meson
BuildRequires:  ninja
BuildRequires:  gcc
BuildRequires:  gcc-c++
BuildRequires:  pkgconf-pkg-config
# Qt6Core/Gui/Network via qt6-base-devel; Qt6Qml + Qt6QmlIntegration
# via qt6-declarative-devel + qt6-qml-devel (QmlIntegration.pc ships
# in qt6-qml-devel on Tumbleweed, per scripts/vm/install-deps.sh).
BuildRequires:  qt6-base-devel
BuildRequires:  qt6-declarative-devel
BuildRequires:  qt6-qml-devel
BuildRequires:  wayland-devel
BuildRequires:  wayland-protocols-devel
# The plugin binds qdwin_shell_v1; the protocol XML ships in qdwin's
# qdistro-protocols.pc.
BuildRequires:  qdwin
# Runtime: the shell is a Quickshell project loaded by
# `qs -p /usr/share/quickshell/qdshell` (qdshell.service). Tumbleweed's
# quickshell package is named noctalia-qs (binary /usr/bin/qs).
Requires:       noctalia-qs
Requires:       qt6-declarative-imports
# dbus-run-session wraps ExecStart in qdshell.service.
Requires:       dbus-1
Requires:       qdwin

%description
qdshell is the trusted desktop shell for the admin session — bar,
panels, launcher, notifications, OSD, settings — a hard fork of
Noctalia v4.5.0 that only targets the qdwin compositor. This package
ships the QML tree at /usr/share/quickshell/qdshell plus the native
Qdistro.Qdwin QML plugin (libqdistro-qdwin.so, binds qdwin_shell_v1)
under /usr/share/qdistro/qml/, resolved via QML_IMPORT_PATH set by
qdshell.service.

%prep
%autosetup

%build
%meson
%meson_build

%install
%meson_install
# The QML tree has no build/install step — install-qdwin-session-for-vm.sh
# rsyncs it verbatim to /usr/share/quickshell/qdshell. Mirror that here,
# minus dev-only content the session never loads (tests, CI scripts, the
# C++ plugin sources already installed via meson above, nix/vcs noise).
install -d %{buildroot}%{_datadir}/quickshell/qdshell
tar -cf - \
    --exclude='./build*' \
    --exclude='./x86_64*' \
    --exclude='./.git*' \
    --exclude='./tests' \
    --exclude='./Tests' \
    --exclude='./scripts' \
    --exclude='./nix' \
    --exclude='./qml-plugin' \
    --exclude='./meson.build' \
    --exclude='./node_modules' \
    --exclude='./__pycache__' \
    --exclude='./pytest.ini' \
    --exclude='./flake.*' \
    --exclude='./shell.nix' \
    --exclude='./lefthook.yml' \
    --exclude='./gen_protocol.sh' \
    . | tar -xf - -C %{buildroot}%{_datadir}/quickshell/qdshell
# The session runs as the admin user — the tree must be world-readable
# (same umask hardening the install script applies).
chmod -R u=rwX,go=rX %{buildroot}%{_datadir}/quickshell/qdshell

%check
test -f %{buildroot}%{_datadir}/qdistro/qml/Qdistro/Qdwin/libqdistro-qdwin.so
test -f %{buildroot}%{_datadir}/qdistro/qml/Qdistro/Qdwin/qmldir
test -f %{buildroot}%{_datadir}/quickshell/qdshell/shell.qml
# No dev leftovers in the payload.
test ! -d %{buildroot}%{_datadir}/quickshell/qdshell/tests
test ! -e %{buildroot}%{_datadir}/quickshell/qdshell/meson.build

%files
%license LICENSE
%doc README.md CREDITS.md
%{_datadir}/quickshell/qdshell/
%{_datadir}/qdistro/qml/
