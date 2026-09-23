srcs_libweston += files([
	'libweston-desktop.c',
	'client.c',
	'seat.c',
	'surface.c',
	'xwayland.c',
	'xdg-shell.c',
])

srcs_libweston += [
	xdg_shell_server_protocol_h,
	xdg_shell_protocol_c,
]

if get_option('deprecated-xdg-shell-v6-unstable')
  warning('deprecated-xdg-shell-v6-unstable enabled. This will be removed in future versions.')
  srcs_libweston += files([ 'xdg-shell-v6.c' ])
  srcs_libweston += [
	xdg_shell_unstable_v6_server_protocol_h,
	xdg_shell_unstable_v6_protocol_c
  ]
endif
