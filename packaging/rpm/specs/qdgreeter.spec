%define pythons python314
Name:           qdgreeter
Version:        0.1.0
Release:        1%{?dist}
Summary:        qdistro boot greeter — graphical replacement for tuigreet
License:        MIT
URL:            https://github.com/qdistro/qdistro
Source:         %{name}-%{version}.tar.gz
BuildRequires:  python-rpm-macros
BuildRequires:  python314-setuptools
BuildRequires:  python314-pip
BuildRequires:  python314-wheel
%global __requires_exclude ^qt6qmlimport\(shim\)$
Requires:       python314-PyQt6
Requires:       qt6-declarative-imports
Provides:        qt6qmlimport(shim)
BuildArch:      noarch

%description
greetd greeter for qdistro (PyQt6/QML). The wheel ships qml/ as package
data — a wheel without it installs fine and dies at first launch.

%prep
%autosetup

%build
%pyproject_wheel

%install
%pyproject_install

%check
# Assert the QML package-data actually landed in the wheel.
test -n "$(ls %{buildroot}%{python314_sitelib}/qdgreeter/qml/Main.qml 2>/dev/null)"

%files
%{python314_sitelib}/qdgreeter/
%{python314_sitelib}/qdgreeter-*.dist-info/
%{_bindir}/qdgreeter