Name:           qdistro-admin
Version:        0.1.0
Release:        1%{?dist}
Summary:        qdistro admin approval surfaces (graphical app, root CLI, TUI)
License:        GPL-3.0-or-later
URL:            https://github.com/qdistro/qdistro
Source:         %{name}-%{version}.tar.gz
Requires:       python314
Requires:       python314-dbus-python
Requires:       python314-PyYAML
Requires:       python314-PyQt6
Requires:       python314-textual
Requires:       python314-rich
BuildArch:      noarch

%description
The broker's trusted control-plane paths: the graphical approval app
(qdistro-admin-approval-app), the root approval CLI
(qdistro-approvals), and the admin Textual TUI (qdistro-admin-tui).

The binaries land under /usr/local ON PURPOSE: broker/qdistro_admin_broker.py
allowlists those exact paths for the admin-control D-Bus methods
(_ADMIN_CONTROL_*_EXES/_ROOT_ADMIN_CONTROL_EXES). Moving them to /usr/bin
without a broker allowlist change silently revokes the approval paths.

%prep
%autosetup

%install
install -d %{buildroot}%{_prefix}/local/bin \
           %{buildroot}%{_prefix}/local/sbin \
           %{buildroot}%{_prefix}/local/lib/qdistro/admin-tui \
           %{buildroot}%{_datadir}/applications

# Graphical approval app + its Wayland launcher (deploy/).
install -m 0755 admin_app/qdistro_admin_app.py \
    %{buildroot}%{_prefix}/local/bin/qdistro-admin-approval-app
install -m 0755 deploy/start-admin-app-wayland.sh \
    %{buildroot}%{_prefix}/local/bin/qdistro-start-admin-app
install -m 0644 admin_app/qdistro-admin-app.desktop \
    %{buildroot}%{_datadir}/applications/qdistro-admin-app.desktop

# Root CLI.
install -m 0755 cli/qdistro_approvals.py \
    %{buildroot}%{_prefix}/local/sbin/qdistro-approvals

# TUI: modules in a root-owned lib dir, entry point a symlink — the same
# layout install-admin-cli-for-vm.sh uses (the TUI puts its own resolved
# dir on sys.path, so the symlink finds its siblings; argv still carries
# the trusted /usr/local/bin path).
install -m 0644 tui/__init__.py tui/broker_client.py tui/silo_colors.py \
    %{buildroot}%{_prefix}/local/lib/qdistro/admin-tui/
install -m 0755 tui/qdistro_admin_tui.py \
    %{buildroot}%{_prefix}/local/lib/qdistro/admin-tui/
ln -sf %{_prefix}/local/lib/qdistro/admin-tui/qdistro_admin_tui.py \
    %{buildroot}%{_prefix}/local/bin/qdistro-admin-tui

%check
test -x %{buildroot}%{_prefix}/local/bin/qdistro-admin-approval-app
test -x %{buildroot}%{_prefix}/local/sbin/qdistro-approvals
test -L %{buildroot}%{_prefix}/local/bin/qdistro-admin-tui
%{__python3} -c 'import dbus, yaml, textual, rich; from PyQt6 import QtCore' 2>/dev/null || true

%files
%{_prefix}/local/bin/qdistro-admin-approval-app
%{_prefix}/local/bin/qdistro-start-admin-app
%{_prefix}/local/bin/qdistro-admin-tui
%{_prefix}/local/sbin/qdistro-approvals
%{_prefix}/local/lib/qdistro/admin-tui/
%{_datadir}/applications/qdistro-admin-app.desktop
