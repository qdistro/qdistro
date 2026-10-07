Name:           qdistro-daemons
Version:        0.1.0
Release:        1%{?dist}
Summary:        qdistro root/session daemons (pixelfeed, secctx, tier1-exec, ...)
License:        GPL-3.0-or-later
URL:            https://github.com/qdistro/qdistro
Source:         %{name}-%{version}.tar.gz
BuildRequires:  meson
BuildRequires:  ninja
BuildRequires:  gcc
BuildRequires:  pkgconf-pkg-config
BuildRequires:  qdwin
BuildRequires:  wayland-devel
BuildRequires:  wayland-protocols-devel
BuildRequires:  libselinux-devel
BuildRequires:  pipewire-devel
BuildRequires:  freerdp-devel
BuildRequires:  winpr-devel
Requires:       qdwin

%description
qdistro daemons built by the daemons/ meson project:
qdistro-forward (PipeWire -> freerdp-shadow3), qdistro-nested-pixelfeed,
qdistro-mm-remote-* helpers, qdistro-secctx-exec, qdistro-cursor-sprites
and qdistro-tier1-exec. The SELinux audisp plugin is installed by the
install.sh path and is not part of this package yet.

%prep
%autosetup

%build
%meson
%meson_build

%install
%meson_install
# cursor-sprites user unit — the compositor defers every wp_cursor_shape
# set until this registers, so it must come up with the shell. Ships as
# a system-wide user unit with the qdshell.service.wants symlink the
# install script creates by hand.
install -d %{buildroot}%{_userunitdir} \
           %{buildroot}%{_userunitdir}/qdshell.service.wants
install -m 0644 cursor-sprites/qdistro-cursor-sprites.service \
    %{buildroot}%{_userunitdir}/
ln -s ../qdistro-cursor-sprites.service \
    %{buildroot}%{_userunitdir}/qdshell.service.wants/qdistro-cursor-sprites.service

%files
%{_bindir}/qdistro-cursor-sprites
%{_bindir}/qdistro-forward
%{_bindir}/qdistro-mm-remote-pixelfeed
%{_bindir}/qdistro-mm-remote-source-helper
%{_bindir}/qdistro-mm-remote-viewer-helper
%{_bindir}/qdistro-nested-pixelfeed
%{_bindir}/qdistro-secctx-exec
%{_libexecdir}/qdistro-tier1-exec
%{_userunitdir}/qdistro-cursor-sprites.service
%{_userunitdir}/qdshell.service.wants/
