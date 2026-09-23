# Nested compositors

qdwin supports a second tier of compositor running inside a qdwin
session. Use cases:

- A guest VM running its own Wayland compositor (sway, hyprland,
  weston) that should be displayed as a single "window" inside the
  host desktop.
- A `recall-user` session running on a separate uid, hosted in a
  framed surface on the admin desktop.
- A Tier-4 / Tier-5 sandbox running a contained compositor stack
  whose toplevels are not first-class qdwin citizens.

The protocol that wires this together is `qdwin_nested_v1`,
implemented in both server and client sides of qdwin (server side
inside `qdwin-shell.so`, client side via `qdwin-nested-client.c`
which is also linked into the same `.so` so the outer compositor can
talk to the inner one).

## Wire shape

The inner compositor binds `qdwin_nested_v1` against the outer's
wl_display. For each native toplevel it wants the outer to host, it:

1. Advertises a node id (an opaque string the inner picks).
2. Publishes a PipeWire stream node id where the toplevel's frames
   will be delivered.
3. Lets the outer call `bind_proxy_pixels` to attach the wl_buffer
   stream to a placeholder `wl_surface` that the outer composites.
4. Receives input events back as `qdwin_stream_input_handle_v1`
   requests (pointer motion, button, axis, keyboard key) which the
   inner replays into its own seat infrastructure.

Pixels move via PipeWire, not Wayland: the outer's
`qdistro-nested-pixelfeed` daemon (umbrella repo) consumes the
PipeWire stream, optionally allocates dmabuf buffers via
`zwp_linux_dmabuf_v1`, and posts them as wl_buffers to the
placeholder surface. The dmabuf path is true zero-copy when the
outer's GL renderer can import the inner's render format; otherwise
the daemon falls back to wl_shm with a single CPU copy per frame.

The production qdshell currently pins this consumer to the SHM fallback. The
dmabuf path is implemented and useful diagnostically, but is not the default
until the nested PipeWire producer has a repeated crash-free reliability gate.

## Local-only boundary

`qdwin_nested_v1` is deliberately a local protocol. Its object model is useful
for a future remote transport, but its carriers and identity checks must not be
forwarded or weakened to make it cross a network:

| Local resource | Purpose and authority | Required remote replacement |
| --- | --- | --- |
| Outer Wayland connection | Carries advertise/configure/focus/close object lifetime; manager bind and advertised `origin_uid` are pinned to the kernel-resolved peer uid | Authenticated session carrying explicit origin, stream, generation, ordering, and reconnect state |
| PipeWire node name | Selects a node in the same local PipeWire graph | Encoded media transport with authentication, format negotiation, flow control, and bounded buffering |
| dmabuf or SHM buffer fd | Supplies a local `wl_buffer` to the outer proxy | Decoder-owned local buffers; never forward fd numbers or assume remote dmabuf importability |
| Input-sink AF_UNIX path | Carries QDNI events; the listener verifies the connecting peer uid | Authenticated, replay-protected input messages bound to the exact remote stream and input grant |
| `bind_proxy_pixels` caller uid | Prevents a different local uid replacing an advertised proxy's pixels | Separate token/capability-authorized remote pixel producer; keep the local uid check unchanged |
| Wayland resource destruction | Removes the proxy while the inner compositor and its app remain the lifetime owners | Explicit close/closed and disconnect/reconcile state machine; transport loss must not imply source-app death |

Consequently, R5 is a local liveness audit of these exact resources. R6 is not
“Wayland forwarding”: it needs a new authenticated remote-proxy adapter that
terminates network media/input/control and creates local resources on each end.
The adapter may reuse the nested toplevel semantics, but it must not generalize
`proxy_origin_uid`, accept network-supplied Unix paths, or relax
`bind_proxy_pixels` ownership.

The durable source lifecycle for R5 is the
`qdwin_nested_toplevel_v1` resource itself: outer close produces
`close_requested`; only inner resource destruction removes the proxy. The older
multi-machine RDP bystander FIFO is therefore not on the R5 lifecycle path. A
production source service still replaces that FIFO before an RDP-derived or
network adapter is treated as the R6 source authority.

## Admit-or-deny gate

When the inner compositor first announces a proxy toplevel, qdwin
fires `nested_proxy_pending` to the shell client carrying the inner's
security-context tag, the node id, and any title metadata. The shell
client replies with `nested_proxy_decision(admit | deny)`. This is
the same shape as the activation-token and clipboard-receive gates
described in [protocol.md](protocol.md): fail-safe (timeout = deny),
shell-policy-driven, async.

## Input model

The outer's default pointer grab forwards motion+button+axis events
to the proxy as soon as the pointer enters the placeholder surface.
The keyboard is always-active: the outer mutates the seat keyboard's
`default_grab.interface` so that when the placeholder has keyboard
focus, every key down/up reaches the inner via
`qdwin_stream_input_handle_v1.keyboard_key`. This avoids the
"grab-while-grabbed" Wayland landmine and keeps the inner's keymap
authoritative.

## Cursor sprites

Cursors are owned by the outer because the outer is the seat owner.
The shell client (or its `qdistro-cursor-sprites` helper in the
umbrella repo) installs theme sprites for each `cursor-shape-v1`
shape via `qdwin_shell_v1.set_cursor_sprite`. The inner never sees
pointer-set-cursor.

## When NOT to use this

Plain sandboxed apps (firefox, gtk-something, qt-something) running
under a security-context tag DO NOT need this. They register native
xdg_toplevels with qdwin directly and the shell client gates them
the same way it gates anything else.

`qdwin_nested_v1` is specifically for "compositor inside compositor"
— when the inner thing already implements Wayland server-side and
its toplevels would otherwise be invisible to the outer.
