Name:           qdwin
Version:        0.1.0
Release:        1%{?dist}
Summary:        qdistro compositor — libweston shell plugin
License:        GPL-3.0-or-later
URL:            https://github.com/qdistro/qdistro
Source:         %{name}-%{version}.tar.gz
BuildRequires:  meson
BuildRequires:  ninja
BuildRequires:  gcc
BuildRequires:  pkgconf-pkg-config
BuildRequires:  weston-devel
BuildRequires:  pkgconfig(libweston-16)
BuildRequires:  wayland-devel
BuildRequires:  wayland-protocols-devel
BuildRequires:  libinput-devel
BuildRequires:  libxkbcommon-devel
BuildRequires:  libXcursor-devel
BuildRequires:  freerdp-devel
BuildRequires:  winpr-devel
BuildRequires:  pipewire-devel
BuildRequires:  libselinux-devel
BuildRequires:  libpixman-1-0-devel
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
BuildRequires:  cairo-devel
BuildRequires:  libpng16-devel
BuildRequires:  pango-devel
BuildRequires:  fontconfig-devel
BuildRequires:  glib2-devel
BuildRequires:  libva-devel
Requires:       libweston-16-0
Requires:       weston

%description
The qdistro compositor: a libweston shell plugin implementing the
qdwin_shell_v1 / qdwin_locker_v1 / qdwin_nested_v1 protocols, plus the
qdistro-protocols pkg-config package used by the qdistro daemons.
Includes the qdwin-*-probe test binaries for now (split to -tests later).

%prep
%autosetup

%build
%meson
%meson_build

%install
%meson_install

%files
%license LICENSE
%{_libdir}/weston/qdwin-shell.so
%{_libdir}/pkgconfig/qdistro-protocols.pc
%{_datadir}/qdistro/protocols/
%{_bindir}/qdwin-*
%{_bindir}/qdistro-test-*
