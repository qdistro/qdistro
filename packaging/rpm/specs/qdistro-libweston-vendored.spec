Name:           qdistro-libweston-vendored
Version:        16.0.0
Release:        1%{?dist}
Summary:        qdistro's patched libweston tree for the qdwin compositor
License:        MIT
URL:            https://github.com/qdistro/qdistro
# Built from the qdwin component tarball: the vendored weston tree with
# qdistro's patches lives at qdwin/libweston-vendored/src. Version tracks
# libweston-vendored/VERSION, not the qdwin component version.
Source:         qdwin-0.1.0.tar.gz
BuildRequires:  meson
BuildRequires:  ninja
BuildRequires:  gcc
BuildRequires:  gcc-c++
BuildRequires:  git
BuildRequires:  pkgconf-pkg-config
BuildRequires:  wayland-devel
BuildRequires:  wayland-protocols-devel
BuildRequires:  libinput-devel
BuildRequires:  libxkbcommon-devel
BuildRequires:  libevdev-devel
BuildRequires:  libgbm-devel
BuildRequires:  libdrm-devel
BuildRequires:  seatd-devel
BuildRequires:  Mesa-libEGL-devel
BuildRequires:  Mesa-libGLESv2-devel
BuildRequires:  Mesa-libGLESv3-devel
BuildRequires:  liblcms2-devel
BuildRequires:  libdisplay-info-devel
BuildRequires:  libX11-devel
BuildRequires:  libxcb-devel
BuildRequires:  libXcursor-devel
BuildRequires:  freerdp-devel
BuildRequires:  winpr-devel
BuildRequires:  pipewire-devel
BuildRequires:  cairo-devel
BuildRequires:  libpng16-devel
BuildRequires:  libpng16-compat-devel
BuildRequires:  pango-devel
BuildRequires:  fontconfig-devel
BuildRequires:  glib2-devel
BuildRequires:  libva-devel
# The system `weston` binary loads this tree: the core via
# LD_LIBRARY_PATH, the backends via WESTON_MODULE_MAP (both set by
# qdwin-compositor.service in the qdistro-session package).
# libweston-16-0 pins the SONAME major the frontend links — a weston
# built against a different libweston major would SIGABRT at compositor
# start (install-vendored-libweston.sh step 3 documents the ABI check).
Requires:       weston
Requires:       libweston-16-0

%description
qdistro carries a patched libweston for layer-shell popup parenting and
the virtio-gpu cursor-hotspot fix (see
qdwin/doc/decisions/0001-vendored-libweston-packaging.md). It installs
the production-profile build tree under
/usr/libexec/qdistro/qdwin-libweston/ so it never shadows the distro
libweston for other weston consumers — only the qdwin compositor unit
opts into it via LD_LIBRARY_PATH + WESTON_MODULE_MAP.

%prep
%autosetup -n qdwin-0.1.0

%build
cd libweston-vendored
export QDWIN_LIBWESTON_PROFILE=production
export QDWIN_LIBWESTON_BUILD_DIR="$PWD/build-prod"
export QDWIN_LIBWESTON_PREFIX="$PWD/prefix"
bash build-libweston.sh

%install
cd libweston-vendored
install -d %{buildroot}%{_libexecdir}/qdistro/qdwin-libweston
cp -a prefix/lib64 %{buildroot}%{_libexecdir}/qdistro/qdwin-libweston/

%check
# Production profile proof: the core .so plus the backends the unit's
# WESTON_MODULE_MAP can remap (drm + gl-renderer at minimum).
test -n "$(ls %{buildroot}%{_libexecdir}/qdistro/qdwin-libweston/lib64/libweston-16.so.0.* 2>/dev/null)"
test -f %{buildroot}%{_libexecdir}/qdistro/qdwin-libweston/lib64/libweston-16/drm-backend.so
test -f %{buildroot}%{_libexecdir}/qdistro/qdwin-libweston/lib64/libweston-16/gl-renderer.so
# No distro libdir paths must leak in — the tree is private by design.

%files
%license libweston-vendored/COPYING
%{_libexecdir}/qdistro/qdwin-libweston/
