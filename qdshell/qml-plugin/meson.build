# Qdistro.Qdwin QML plugin — exposes qdwin_shell_v1 to QML.
#
# Lives in the qdshell repo because qdshell is the sole consumer and
# the lifecycle of the plugin tracks qdshell's QML surface, not
# qdwin's compositor internals. The protocol XML lives in the qdwin
# repo (it's the protocol owner); qdwin installs it as part of the
# `qdistro-protocols` pkg-config package, and we discover it through
# that — same shape as upstream `wayland-protocols`. The previous
# `../../qdwin/qdwin/qdwin-shell-v1.xml` sibling-checkout path was
# replaced 2026-05-21; the only requirement now is that qdwin's
# `qdistro-protocols.pc` is on PKG_CONFIG_PATH at build time.
#
# Built as a side-by-side native QML plugin (no qs/quickshell fork).
# qmldir + libqdistro-qdwin.so install under
# datadir/qdistro/qml/Qdistro/Qdwin/ so that anything launched with
# QML_IMPORT_PATH=/usr/share/qdistro/qml picks up
# `import Qdistro.Qdwin 1.0`. The qdshell systemd unit grew that env
# var in qdistro/scripts/install/install-qdwin-session-for-vm.sh
# (noctalia-shell.service drop-in) — keep both locations consistent
# if the install dir ever changes.

qt6 = import('qt6')

dep_qt6_core           = dependency('Qt6Core')
dep_qt6_gui            = dependency('Qt6Gui')
dep_qt6_qml            = dependency('Qt6Qml')
dep_qt6_qmlintegration = dependency('Qt6QmlIntegration', required: false)
dep_wayland_client     = dependency('wayland-client')
dep_wayland_scanner    = dependency('wayland-scanner', native: true)

wayland_scanner = find_program(
  dep_wayland_scanner.get_variable(pkgconfig: 'wayland_scanner'),
  native: true,
)

# qdwin_shell_v1 protocol XML — sourced from qdwin's installed
# qdistro-protocols pkg-config package. qdwin must be built and
# installed (or `qdistro-protocols.pc` otherwise reachable via
# PKG_CONFIG_PATH) before this builds.
dep_qdistro_protocols = dependency('qdistro-protocols')
qdistro_protocols_dir = dep_qdistro_protocols.get_variable(pkgconfig: 'pkgdatadir')
proto_xml = files(qdistro_protocols_dir / 'qdwin-shell-v1.xml')

qdwin_shell_client_header = custom_target(
  'qdwin-shell-v1-client-header',
  input: proto_xml,
  output: 'qdwin-shell-v1-client-protocol.h',
  command: [wayland_scanner, 'client-header', '@INPUT@', '@OUTPUT@'],
)
qdwin_shell_protocol_code = custom_target(
  'qdwin-shell-v1-private-code',
  input: proto_xml,
  output: 'qdwin-shell-v1-protocol.c',
  command: [wayland_scanner, 'private-code', '@INPUT@', '@OUTPUT@'],
)

# Weston framebuffer capture protocol. This is an upstream libweston protocol,
# not a qdistro-private one; keep a verbatim vendored copy beside the sole
# client so qdistro-protocols remains single-directory-safe in both installed
# and Meson-uninstalled builds.
weston_output_capture_xml = files('weston-output-capture.xml')
weston_output_capture_client_header = custom_target(
  'weston-output-capture-client-header',
  input: weston_output_capture_xml,
  output: 'weston-output-capture-client-protocol.h',
  command: [wayland_scanner, 'client-header', '@INPUT@', '@OUTPUT@'],
)
weston_output_capture_protocol_code = custom_target(
  'weston-output-capture-private-code',
  input: weston_output_capture_xml,
  output: 'weston-output-capture-protocol.c',
  command: [wayland_scanner, 'private-code', '@INPUT@', '@OUTPUT@'],
)

# v24 workspaces: ext-workspace-v1 (standard, from installed
# wayland-protocols). qdwin implements the server; we bind it as a client
# alongside qdwin_shell_v1. See todo/decisions/qdwin-workspaces-ext-protocol.md.
dep_wayland_protocols = dependency('wayland-protocols', native: true)
wayland_protocols_dir = dep_wayland_protocols.get_variable(pkgconfig: 'pkgdatadir')
ext_workspace_xml = files(
  wayland_protocols_dir / 'staging/ext-workspace/ext-workspace-v1.xml')
ext_workspace_client_header = custom_target(
  'ext-workspace-v1-client-header',
  input: ext_workspace_xml,
  output: 'ext-workspace-v1-client-protocol.h',
  command: [wayland_scanner, 'client-header', '@INPUT@', '@OUTPUT@'],
)
ext_workspace_protocol_code = custom_target(
  'ext-workspace-v1-private-code',
  input: ext_workspace_xml,
  output: 'ext-workspace-v1-protocol.c',
  command: [wayland_scanner, 'private-code', '@INPUT@', '@OUTPUT@'],
)

# Output (display) management: wlr-output-management-unstable-v1 (client).
# qdwin implements the server; the Display layout tab binds it through this
# plugin. Unlike ext-workspace-v1 this protocol is NOT in wayland-protocols
# (it is a wlr-protocols extension), so qdwin owns the single vendored copy
# and installs it via qdistro-protocols — we discover it there, same as
# qdwin-shell-v1.xml above (no per-consumer copy). See
# todo/decisions/qdwin-output-management.md.
wlr_output_mgmt_xml = files(
  qdistro_protocols_dir / 'wlr-output-management-unstable-v1.xml')
wlr_output_mgmt_client_header = custom_target(
  'wlr-output-management-v1-client-header',
  input: wlr_output_mgmt_xml,
  output: 'wlr-output-management-unstable-v1-client-protocol.h',
  command: [wayland_scanner, 'client-header', '@INPUT@', '@OUTPUT@'],
)
wlr_output_mgmt_protocol_code = custom_target(
  'wlr-output-management-v1-private-code',
  input: wlr_output_mgmt_xml,
  output: 'wlr-output-management-unstable-v1-protocol.c',
  command: [wayland_scanner, 'private-code', '@INPUT@', '@OUTPUT@'],
)

# Idle notifications: ext-idle-notify-v1 (standard, from wayland-protocols).
# qdwin implements the server; the Power tab's inactivity-action and
# display-off timers bind it through this plugin (client) to observe input
# idle. Same discovery as ext-workspace-v1 above.
ext_idle_notify_xml = files(
  wayland_protocols_dir / 'staging/ext-idle-notify/ext-idle-notify-v1.xml')
ext_idle_notify_client_header = custom_target(
  'ext-idle-notify-v1-client-header',
  input: ext_idle_notify_xml,
  output: 'ext-idle-notify-v1-client-protocol.h',
  command: [wayland_scanner, 'client-header', '@INPUT@', '@OUTPUT@'],
)
ext_idle_notify_protocol_code = custom_target(
  'ext-idle-notify-v1-private-code',
  input: ext_idle_notify_xml,
  output: 'ext-idle-notify-v1-protocol.c',
  command: [wayland_scanner, 'private-code', '@INPUT@', '@OUTPUT@'],
)

dep_qt6_network = dependency('Qt6Network')

moc_files = qt6.preprocess(
  moc_headers: ['qdwin-binding.h', 'ctrl-server.h'],
  moc_sources: ['qdistro-qdwin-plugin.cpp'],
  dependencies: [dep_qt6_core, dep_qt6_qml, dep_qt6_network],
)

qml_plugin_install_dir = get_option('datadir') / 'qdistro' / 'qml' / 'Qdistro' / 'Qdwin'

shared_library(
  'qdistro-qdwin',
  sources: [
    'qdwin-binding.cpp',
    'ctrl-server.cpp',
    'qdistro-qdwin-plugin.cpp',
    moc_files,
    qdwin_shell_client_header,
    qdwin_shell_protocol_code,
    weston_output_capture_client_header,
    weston_output_capture_protocol_code,
    ext_workspace_client_header,
    ext_workspace_protocol_code,
    wlr_output_mgmt_client_header,
    wlr_output_mgmt_protocol_code,
    ext_idle_notify_client_header,
    ext_idle_notify_protocol_code,
  ],
  dependencies: [
    dep_wayland_client,
    dep_qt6_core,
    dep_qt6_gui,
    dep_qt6_qml,
    dep_qt6_network,
  ],
  cpp_args: ['-fPIC'],
  install: true,
  install_dir: qml_plugin_install_dir,
)

install_data('qmldir', install_dir: qml_plugin_install_dir)
