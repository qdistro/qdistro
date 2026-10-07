%define pythons python314
Name:           qdbrowser
Version:        0.1.0
Release:        1%{?dist}
Summary:        Qt-based feature-rich web browser for qdistro
License:        GPL-3.0-only
URL:            https://github.com/qdistro/qdistro
Source:         %{name}-%{version}.tar.gz
BuildRequires:  python-rpm-macros
BuildRequires:  python314-setuptools
BuildRequires:  python314-pip
BuildRequires:  python314-wheel
Requires:       python314-PyQt6
Requires:       python314-PyQt6-WebEngine
Requires:       python314-jeepney
Requires:       qdistro-presentation
BuildArch:      noarch

%description
qdistro's first-party browser (QtWebEngine), sibling of qterminator.

%prep
%autosetup

%build
%pyproject_wheel

%install
%pyproject_install

%files
%{python314_sitelib}/qdbrowser/
%{python314_sitelib}/qdbrowser-*.dist-info/
%{_bindir}/qdbrowser*