%define pythons python314
Name:           qdistro-presentation
Version:        1.0.0
Release:        1%{?dist}
Summary:        qdshell presentation snapshot model, publisher, and Qt adapter
License:        GPL-3.0-or-later
URL:            https://github.com/qdistro/qdistro
Source:         %{name}-%{version}.tar.gz
BuildRequires:  python-rpm-macros
BuildRequires:  python314-setuptools
BuildRequires:  python314-pip
BuildRequires:  python314-wheel
Requires:       python314-PyQt6
BuildArch:      noarch

%description
Snapshot model, publisher and Qt adapter for qdistro session
presentation. Python import name: qdistro_presentation.

%prep
%autosetup

%build
%pyproject_wheel

%install
%pyproject_install

%files
%{python314_sitelib}/qdistro_presentation/
%{python314_sitelib}/qdistro_presentation-*.dist-info/
%{_bindir}/qdistro-presentation-publish