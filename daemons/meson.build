project('qdistro-daemons', 'c',
  version: '0.1.0',
  license: 'GPL-3.0-or-later',
  default_options: [
    'c_std=gnu11',
    'warning_level=2',
    'werror=false',
  ],
)

# Sibling-checkout: qdwin's protocol XML is expected at
# ../../qdwin/qdwin/*.xml relative to this meson.build file
# (qdistro/daemons/meson.build → ../../qdwin/...). See the qdistro
# umbrella README for the 3-repo layout.
qdwin_dir = meson.current_source_dir() / '..' / '..' / 'qdwin' / 'qdwin'
fs = import('fs')
if not fs.is_dir(qdwin_dir)
  error('qdwin sibling checkout not found at ' + qdwin_dir +
        ' — clone codeberg.org/qdistro/qdwin next to qdistro/')
endif

# Common deps.
cc = meson.get_compiler('c')
dep_wayland_client   = dependency('wayland-client')
dep_wayland_scanner  = dependency('wayland-scanner', native: true)
dep_wayland_protocols = dependency('wayland-protocols', native: true)
wayland_scanner = find_program(
  dep_wayland_scanner.get_variable(pkgconfig: 'wayland_scanner'),
  native: true,
)
wayland_protocols_dir = dep_wayland_protocols.get_variable(pkgconfig: 'pkgdatadir')

# qdwin_shell_v1 client header + protocol code (from qdwin sibling).
qdwin_shell_xml = files(qdwin_dir / 'qdwin-shell-v1.xml')
qdwin_shell_client_header = custom_target(
  'qdwin-shell-v1-client-header',
  input: qdwin_shell_xml,
  output: 'qdwin-shell-v1-client-protocol.h',
  command: [wayland_scanner, 'client-header', '@INPUT@', '@OUTPUT@'],
)
qdwin_shell_protocol_code = custom_target(
  'qdwin-shell-v1-private-code',
  input: qdwin_shell_xml,
  output: 'qdwin-shell-v1-protocol.c',
  command: [wayland_scanner, 'private-code', '@INPUT@', '@OUTPUT@'],
)

# security-context-v1 (qdistro-secctx-exec).
security_context_xml = files(
  wayland_protocols_dir / 'staging/security-context/security-context-v1.xml',
)
security_context_client_header = custom_target(
  'security-context-v1-client-header',
  input: security_context_xml,
  output: 'security-context-v1-client-protocol.h',
  command: [wayland_scanner, 'client-header', '@INPUT@', '@OUTPUT@'],
)
security_context_protocol_code = custom_target(
  'security-context-v1-private-code',
  input: security_context_xml,
  output: 'security-context-v1-protocol.c',
  command: [wayland_scanner, 'private-code', '@INPUT@', '@OUTPUT@'],
)

# linux-dmabuf-v1 (qdistro-nested-pixelfeed zero-copy path).
linux_dmabuf_xml = files(
  wayland_protocols_dir / 'unstable/linux-dmabuf/linux-dmabuf-unstable-v1.xml',
)
linux_dmabuf_client_header = custom_target(
  'linux-dmabuf-v1-client-header',
  input: linux_dmabuf_xml,
  output: 'linux-dmabuf-unstable-v1-client-protocol.h',
  command: [wayland_scanner, 'client-header', '@INPUT@', '@OUTPUT@'],
)
linux_dmabuf_protocol_code = custom_target(
  'linux-dmabuf-v1-private-code',
  input: linux_dmabuf_xml,
  output: 'linux-dmabuf-unstable-v1-protocol.c',
  command: [wayland_scanner, 'private-code', '@INPUT@', '@OUTPUT@'],
)

# qdistro-forward: per-view RDP proxy reading from a PipeWire node.
dep_freerdp_shadow = dependency('freerdp-shadow3', required: false)
dep_freerdp        = dependency('freerdp3',        required: false)
dep_winpr          = dependency('winpr3',          required: false)
dep_pipewire       = dependency('libpipewire-0.3', required: false)

if dep_freerdp_shadow.found() and dep_freerdp.found() and dep_winpr.found() and dep_pipewire.found()
  executable('qdistro-forward',
    sources: [
      'forward/qdistro-forward.c',
      qdwin_shell_client_header,
      qdwin_shell_protocol_code,
    ],
    dependencies: [
      dep_freerdp_shadow, dep_freerdp, dep_winpr, dep_pipewire,
      dep_wayland_client,
    ],
    install: true,
    install_dir: get_option('bindir'),
  )
else
  message('qdistro-forward skipped — freerdp3 / winpr3 / libpipewire-0.3 not found')
endif

# qdistro-nested-pixelfeed: PipeWire → wl_buffer feeder for nested
# compositors. dmabuf zero-copy when the outer compositor offers it.
if dep_pipewire.found()
  executable('qdistro-nested-pixelfeed',
    sources: [
      'nested-pixelfeed/qdistro-nested-pixelfeed.c',
      qdwin_shell_client_header,
      qdwin_shell_protocol_code,
      linux_dmabuf_client_header,
      linux_dmabuf_protocol_code,
    ],
    dependencies: [dep_wayland_client, dep_pipewire],
    install: true,
    install_dir: get_option('bindir'),
  )
else
  message('qdistro-nested-pixelfeed skipped — libpipewire-0.3 not found')
endif

# qdistro-secctx-exec: wp_security_context_v1 wrapper for non-secctx
# clients. Required for tier-3+ sandboxing.
executable('qdistro-secctx-exec',
  sources: [
    'secctx-exec/qdistro-secctx-exec.c',
    security_context_client_header,
    security_context_protocol_code,
  ],
  dependencies: [dep_wayland_client],
  install: true,
  install_dir: get_option('bindir'),
)

# qdistro-tier1-exec: SELinux tier-1 exec-context wrapper. Optional —
# build skipped cleanly when libselinux is unavailable; the tier-1
# pipeline degrades to "secctx tag only".
dep_selinux = dependency('libselinux', required: false)
if dep_selinux.found()
  executable('qdistro-tier1-exec',
    sources: [
      'tier1-exec/qdistro-tier1-exec.c',
    ],
    dependencies: [dep_selinux],
    install: true,
    install_dir: get_option('libexecdir'),
  )
else
  message('qdistro-tier1-exec skipped — libselinux not found')
endif

# qdistro-cursor-sprites: cursor-shape-v1 sprite installer with
# libXcursor theme lookup.
dep_xcursor = dependency('xcursor')
executable('qdistro-cursor-sprites',
  sources: [
    'cursor-sprites/qdistro-cursor-sprites.c',
    qdwin_shell_client_header,
    qdwin_shell_protocol_code,
  ],
  dependencies: [dep_wayland_client, dep_xcursor],
  install: true,
  install_dir: get_option('bindir'),
)

# Note: daemons/audisp/ is shell-script-only (qdistro-audisp-plugin
# + qdistro-audisp.conf + install.sh) and has no meson target. Install
# via daemons/audisp/install.sh on the target machine.
