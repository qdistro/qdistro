Name:           qdistro-quickshell
Version:        0.3.2
Release:        1%{?dist}
Summary:        qdistro's vendored upstream Quickshell runtime for qdshell
License:        LGPL-3.0-only AND BSD-3-Clause
URL:            https://github.com/qdistro/qdistro
# Built from the qdshell component tarball: the vendored upstream Quickshell
# tree (plus the vendored single-header CLI11 — Tumbleweed ships no cli11
# package) lives at qdshell/quickshell-vendored. Version tracks
# quickshell-vendored/VERSION, not the qdshell component version.
Source:         qdshell-0.1.0.tar.gz
BuildRequires:  cmake
BuildRequires:  ninja
BuildRequires:  gcc
BuildRequires:  gcc-c++
BuildRequires:  pkgconf-pkg-config
BuildRequires:  spirv-tools
BuildRequires:  vulkan-devel
BuildRequires:  pam-devel
BuildRequires:  polkit-devel
BuildRequires:  jemalloc-devel
BuildRequires:  cpptrace-devel
BuildRequires:  glib2-devel
BuildRequires:  libdrm-devel
BuildRequires:  libgbm-devel
BuildRequires:  libxcb-devel
BuildRequires:  pipewire-devel
BuildRequires:  wayland-devel
BuildRequires:  wayland-protocols-devel
BuildRequires:  Mesa-libEGL-devel
BuildRequires:  qt6-base-devel
BuildRequires:  qt6-declarative-devel
BuildRequires:  qt6-quick-private-devel
BuildRequires:  qt6-qml-private-devel
BuildRequires:  qt6-shadertools-devel
BuildRequires:  qt6-waylandclient-devel
BuildRequires:  qt6-waylandclient-private-devel
# Upstream Quickshell is not packaged for Tumbleweed (the archived
# noctalia-qs fork was never a legitimate qdistro dep — qdshell forked
# Noctalia before that fork existed), so the runtime is the vendored build.
# Scripts/install zypper-locks noctalia-qs on persistent systems; this
# package is what actually satisfies the qdshell.service Exec.
Provides:       quickshell = %{version}
Requires:       libjemalloc2
Requires:       libcpptrace1
Requires:       libpolkit-agent-1-0
Requires:       libpolkit-gobject-1-0

%description
Vendored upstream Quickshell (pinned tarball + sha256, see
qdshell/quickshell-vendored/VERSION) built by the same
build-quickshell.sh wrapper the container native stage uses. Installs
/usr/bin/quickshell plus the /usr/bin/qs symlink, the .desktop file and
the application icon — the runtime behind `qs -p
/usr/share/quickshell/qdshell` in qdshell.service.

%prep
%autosetup -n qdshell-0.1.0

%build
cd quickshell-vendored
export QDSHELL_QS_BUILD_DIR="$PWD/build-rpm"
bash build-quickshell.sh

%install
DESTDIR=%{buildroot} cmake --install quickshell-vendored/build-rpm
# cli11's LICENSE shares a basename with Quickshell's — %license would
# flatten both into the same name. Install it under a distinct name so
# the BSD notice actually survives in the binary package.
install -Dm644 quickshell-vendored/third-party/cli11/LICENSE \
    %{buildroot}%{_defaultlicensedir}/%{name}/LICENSE.cli11

%check
test -x %{buildroot}%{_bindir}/quickshell
test -L %{buildroot}%{_bindir}/qs
# Upstream installs qs -> /usr/bin/quickshell (absolute); either form is
# correct for a /usr prefix, so pin the target's basename.
[ "$(basename "$(readlink %{buildroot}%{_bindir}/qs)")" = quickshell ]

%files
# Quickshell is LGPL-3.0 (LICENSE) atop GPL-3.0 (LICENSE-GPL); the
# compiled-in vendored CLI11 single header is BSD-3-Clause, whose
# notice must ship in the binary package (installed in %install as
# LICENSE.cli11).
%license quickshell-vendored/src/LICENSE quickshell-vendored/src/LICENSE-GPL
%{_defaultlicensedir}/%{name}/LICENSE.cli11
%{_bindir}/quickshell
%{_bindir}/qs
%{_datadir}/applications/org.quickshell.desktop
%{_datadir}/icons/hicolor/scalable/apps/org.quickshell.svg
