%define pythons python314
Name:           qfileman
Version:        0.1.0
Release:        1%{?dist}
Summary:        qdistro file manager — Qt, plugin support
License:        GPL-3.0-only
URL:            https://github.com/qdistro/qdistro
Source:         %{name}-%{version}.tar.gz
BuildRequires:  python-rpm-macros
BuildRequires:  python314-setuptools
BuildRequires:  python314-pip
BuildRequires:  python314-wheel
Requires:       python314-PyQt6
Requires:       python314-tomli-w
Requires:       qdistro-presentation
BuildArch:      noarch

%description
qdistro's first-party file manager (Python package qfileman; source
directory is qdfileman/ in the monorepo).

%prep
%autosetup

%build
%pyproject_wheel

%install
%pyproject_install

%check
test -f %{buildroot}%{_datadir}/applications/qfileman.desktop

%files
%{python314_sitelib}/qfileman/
%{python314_sitelib}/qfileman-*.dist-info/
%{_bindir}/qfileman
%{_datadir}/applications/qfileman.desktop
%{_datadir}/metainfo/qfileman.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/qfileman.svg