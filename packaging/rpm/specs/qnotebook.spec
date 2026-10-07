%define pythons python314
Name:           qnotebook
Version:        0.0.1
Release:        1%{?dist}
Summary:        Personal PyQt6 wiki editor, inspired by Zim
License:        GPL-2.0-or-later
URL:            https://github.com/qdistro/qdistro
Source:         %{name}-%{version}.tar.gz
BuildRequires:  python-rpm-macros
BuildRequires:  python314-setuptools
BuildRequires:  python314-pip
BuildRequires:  python314-wheel
Requires:       python314-PyQt6
Requires:       python314-mistune
Requires:       qdistro-presentation
BuildArch:      noarch

%description
qdistro's notes/wiki app.

%prep
%autosetup
# pyproject declares a non-existent build backend
# (setuptools.backends._legacy:_Backend); use the standard one.
sed -i 's|setuptools.backends._legacy:_Backend|setuptools.build_meta|' pyproject.toml

%build
%pyproject_wheel

%install
%pyproject_install

%files
%{python314_sitelib}/qnotebook/
%{python314_sitelib}/qnotebook-*.dist-info/
%{_bindir}/qnotebook