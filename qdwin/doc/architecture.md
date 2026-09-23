# Architecture

## Why libweston

qdwin is built as a shell plugin for libweston rather than from
scratch on wlroots, Smithay, or by forking Mutter / KWin. The choice
was made deliberately and is load-bearing for several downstream
decisions.

**libweston is the only mainstream wayland compositor with a stable
public C API**, designed from the start to host out-of-tree shells.
weston ships kiosk-shell, desktop-shell, fullscreen-shell, and ivi-shell
in-tree using the same API surface we use. The shell plugin is loaded
as a `.so`, libweston handles every protocol that isn't shell-specific
(seat, output, xdg-shell, dmabuf, presentation-time, etc.), and the
shell adds the policy + composition logic on top.

**Effects are outsourced**. qdwin renders flat — no blur, no
animation, no scene graph. Visual effects are the shell client's job
(qdshell does them in Quickshell/QML). This keeps the compositor's
TCB small and within the comfort zone of one author.

**wlroots was the runner-up**. Rejected because it's a library-of-
primitives rather than a compositor framework: every wlroots shell
re-implements the same xdg-shell mainloop. Forking Mutter or KWin
was rejected because both target distro desktops; the surface area
LLM contributors would need to understand is wildly out of scope.
Writing one from scratch was rejected because reimplementing
xdg-shell + xwayland alone consumes a year before qdistro even
starts.

The full decision rationale, including the dropped alternatives, is
in qdistro's `doc/compositor.md`.

## Plugin shape

`qdwin-shell.so` registers as the libweston shell. It implements a
single `wet_shell_init()` entry point and from there:

1. Creates the `qdwin_shell_v1` global on the wl_display. Restricts
   binding to a single privileged uid (the admin) — every other
   client gets the global filtered out via libweston's
   `wl_global_create` filter callback.
2. Drives the libweston `weston_desktop_api` (toplevel add/remove,
   commit, set_title, parent change) and translates each event into
   a corresponding `qdwin_shell_v1` event sent to the shell client.
3. Implements the layer shell, idle-notify, cursor-shape,
   fractional-scale, primary-selection, security-context, and
   xdg-activation server protocols. These are public Wayland
   protocols; qdwin owns the policy (which clients can use which
   protocol), libweston the wire mechanics.
4. Acts as the dispatcher for hotkeys, focus changes, and
   selection mediation. Every cross-client clipboard transfer is
   gated by an event/decision round-trip with the shell client
   (`data_offer_receive_pending` → `data_offer_receive_decision`).

`qdwin.c` is one ~11k-line translation unit. That is deliberate.
Splitting it requires building stable internal abstractions, which
mostly serves refactoring rather than clarity. The file is
organized by protocol surface — every interface from the XML has a
contiguous handler region.

## Trust model

- **Trusted (single instance, must run before any other client):**
  the shell client. Today that's qdshell on its launcher uid; in
  principle anyone implementing the `qdwin_shell_v1` client surface.
  qdwin grants this client unconditional toplevel management,
  selection mediation, and policy decisions.

- **Sandboxed:** every other Wayland client. Each connection is
  expected to carry a `wp_security_context_v1` tag from a sandbox
  engine (waypipe, secctx-exec wrapper) identifying the engine and
  app_id. qdwin forwards the tag to the shell client; the shell
  decides whether to grant focus, accept selection events, etc.

- **No third tier**. There is no "trusted-app" tier between shell
  and sandboxed. Either you implement the shell protocol or you go
  through the broker like every other app.

## Nested compositing

A second-level compositor (a guest VM's compositor, or a
recall-user's contained session) is represented in qdwin as a
"proxy" toplevel that qdwin doesn't actually composite — it
forwards pixels and input via the `qdwin_nested_v1` protocol. See
[nested.md](nested.md).

## Vendored libweston

qdwin ships a vendored copy of libweston-16 in `libweston-vendored/`
with a single patch that allows `xdg_popup` with a NULL parent
surface. The shell client uses this in two places where Wayland's
strict parent-popup model gets in the way (Quickshell-style
"floating" popups). The patch is offered to upstream; until accepted,
distros need a custom libweston build.

When the distro libweston already has the patch, qdwin links against
the system copy and the vendored tree is unused.
