%define pythons python314
Name:           qdlocker
Version:        0.1.0
Release:        1%{?dist}
Summary:        qdistro screen locker — Qt/QML peer client to qdwin
License:        MIT
URL:            https://github.com/qdistro/qdistro
Source:         %{name}-%{version}.tar.gz
BuildRequires:  python-rpm-macros
BuildRequires:  python314-setuptools
BuildRequires:  python314-pip
BuildRequires:  python314-wheel
%global __requires_exclude ^qt6qmlimport\(shim\)$
Requires:       python314-PyQt6
Requires:       python314-dbus_next
Requires:       python314-python-pam
Requires:       python314-pywayland
Requires:       qt6-declarative-imports
Provides:        qt6qmlimport(shim)
Requires:       qdwin
BuildArch:      noarch

%description
Screen locker speaking the qdwin_locker_v1 protocol. The wheel ships
qml/ and the generated protocol module as package data.

%prep
%autosetup

%build
%pyproject_wheel

%install
%pyproject_install
# Session unit — shipped from qdlocker/systemd/ with the same ExecStart
# rewrite qdistro-bootstrap.sh's install_qdlocker_service applies
# (canonical upstream ExecStart is /usr/local/bin; our console_script
# lands at /usr/bin). The unit is session-scoped (Requisite=
# qdwin-compositor) and pulled in by qdwin-session.target's Wants=,
# so no .wants symlink is needed.
install -d %{buildroot}%{_userunitdir}
sed 's|ExecStart=/usr/local/bin/qdlocker|ExecStart=/usr/bin/qdlocker|g' \
    systemd/qdlocker.service > %{buildroot}%{_userunitdir}/qdlocker.service
# Drop-in — the QDLOCKER_QDSHELL_PATH/PAM env the bootstrap writes under
# /etc/systemd/user/qdlocker.service.d/.
install -d %{buildroot}%{_sysconfdir}/systemd/user/qdlocker.service.d
cat > %{buildroot}%{_sysconfdir}/systemd/user/qdlocker.service.d/qdshell-path.conf <<'EOF'
[Service]
Environment=QDLOCKER_QDSHELL_PATH=/usr/share/quickshell/qdshell
Environment=QDLOCKER_PAM_SERVICE=qdlocker
EOF
# Dedicated unlock PAM service (common-account + pam_faillock deny=5).
install -D -m 0644 pam/qdlocker %{buildroot}%{_sysconfdir}/pam.d/qdlocker

%check
test -n "$(ls %{buildroot}%{python314_sitelib}/qdlocker/qml/Main.qml 2>/dev/null)"
grep -q 'ExecStart=/usr/bin/qdlocker' %{buildroot}%{_userunitdir}/qdlocker.service

%files
%{python314_sitelib}/qdlocker/
%{python314_sitelib}/qdlocker-*.dist-info/
%{_bindir}/qdlocker
%{_userunitdir}/qdlocker.service
%{_sysconfdir}/systemd/user/qdlocker.service.d/
%config %{_sysconfdir}/pam.d/qdlocker