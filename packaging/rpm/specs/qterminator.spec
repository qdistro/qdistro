%define pythons python314
Name:           qterminator
Version:        0.1.0
Release:        1%{?dist}
Summary:        Qt port of Terminator terminal emulator
License:        GPL-3.0-only
URL:            https://github.com/qdistro/qdistro
Source:         %{name}-%{version}.tar.gz
BuildRequires:  python-rpm-macros
BuildRequires:  python314-setuptools
BuildRequires:  python314-pip
BuildRequires:  python314-wheel
Requires:       python314-PyQt6
Requires:       qdistro-presentation
BuildArch:      noarch

%description
qdistro's first-party terminal (Python package qterminator; source
directory is qdterm/ in the monorepo).

%prep
%autosetup

%build
%pyproject_wheel

%install
%pyproject_install

%check
test -f %{buildroot}%{_datadir}/applications/qterminator.desktop

%files
%{python314_sitelib}/qterminator/
%{python314_sitelib}/qterminator-*.dist-info/
%{_bindir}/qterminator*
%{_datadir}/applications/qterminator.desktop
%{_datadir}/metainfo/qterminator.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/qterminator.svg