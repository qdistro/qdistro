# Compositor

qdistro's compositor is a **libweston shell plugin**. libweston is the Wayland
reference compositor's core as a library, MIT-licensed, with automotive-grade
stability heritage and a plugin architecture designed for third-party shells.
The plugin is named **qdwin**. The user-facing shell client is **qdshell**.

For wire-level protocol detail (qdwin_shell_v1, qdwin_nested_manager_v1, etc.)
see the qdwin repository's `doc/protocol.md`. This document covers the
architectural shape only.

## Why libweston

The compositor is qdistro's **trusted computing base**. Every line in it runs
with seat, framebuffer, and input access across all uids. Over a multi-year
product horizon the choice of base library compounds: API churn, upstream pace,
threat-model fit, and "how easy is this to explain to a future contributor" all
weight more than initial velocity.

libweston is picked because:

1. **Threat-model alignment.** libweston is the Wayland reference implementation,
 conservatively maintained by Collabora with automotive-grade stability
 expectations. The same codebase ships in production vehicles (AGL,
 Renesas R-Car) and industrial deployments (NXP i.MX BSPs, Yocto) where a
 crash is a safety event. That posture is closer to qdistro's than any
 "daily desktop abuse" testing profile.

2. **API stability.** Major versions are additive with deprecation cycles.
 Over a five-year horizon this matters more than ambient community activity.

3. **Shell-plugin architecture.** libweston provides backends, output
 management, surface lifecycle, input routing, XWayland, DRM, color
 management, and RDP capture. qdistro supplies the *policy*: window
 placement, per-uid isolation, private protocols, Qt-shell attachment,
 handoff. AGL's `agl-compositor` (~11.8k LOC, production) is the direct
 template.

4. **Multi-tenant heritage.** Automotive cockpits ship Weston driving multiple
 displays with different users/tenants per zone. That is qdistro's model on
 a different form factor. The multi-backend idiom (DRM + headless + RDP +
 PipeWire + wayland-nested simultaneously, one config) is first-class.

5. **Built-in RDP / capture.** Weston upstream ships `backend-rdp` (FreeRDP),
 `backend-pipewire`, and `weston_capture_v1`. RDP is qdistro's forwarding
 transport across the board — mature codec, native audio and clipboard
 channels, matches Mutter, KDE screen-share, and WSL2's `wslg` path.

6. **Effects outsourcing is architecturally correct.** Rich in-compositor
 effects are attack surface in the TCB. For qdistro, effects belong
 *outside* the compositor (see "Effects outsourcing" below). Weston's lack
 of a scenefx-like library isn't a limitation — it's exactly the shape we
 want.

7. **Distribution availability.** `weston-devel` ships on openSUSE Tumbleweed,
 Debian/Ubuntu, Fedora, Arch. MIT-licensed; no CLA, no GPL contamination.

Alternatives evaluated and rejected:

- **wlroots** — no API-stability promise; the security-focused product would
 eat continuous re-verification. Multi-tenant story is "roll your own."
- **Smithay** — pre-1.0, quarterly API churn, no Python bindings, no scene-
 graph analogue to wlroots' `wlr_scene`.
- **Mutter, KWin** — neither is intended as a library; both drag heavy
 dependency trees. Soft-fork precedents (Muffin, KWinFT) show full-time-
 engineer maintenance cost.
- **From scratch** — 40-60k LOC of plumbing that libweston already handles;
 attack surface owned grows dramatically.

## Layering

```
+----------------------------------------------------+
| qdshell: panels, system tray, notifications, |
| admin panel, menus, locker UI |
| (Qt + QML) |
| |
| Per-uid satellite clients: |
| Qt-rendered window chrome, context menus|
| (one per active uid) |
+----------------------------------------------------+
| Session / policy glue: |
| - lock state, render-policy decisions |
| - nested-session lifecycle |
| - qbus-admin endpoint |
| - polkit agent integration |
| - qdwin_shell_v1 private protocol |
| (Python via CFFI against libweston) |
+----------------------------------------------------+
| qdwin: libweston plugin (C) |
| - private protocol server |
| - peer-uid enforcement |
| - holding-state + chrome compositing |
| - window placement / tiling policy |
+----------------------------------------------------+
| libweston core (C, upstream): |
| surfaces, input, output, DRM/KMS, damage |
| tracking, cursor, seat, XWayland, backends |
| (DRM, headless, wayland, RDP, PipeWire) |
+----------------------------------------------------+
```

Qt and QML live strictly above the core; no Qt inside the pixel-pushing path.
The shell plugin is small C (qdistro-specific policy); libweston's plumbing
(backends, surface lifecycle, input routing, XWayland, capture) is reused
rather than rebuilt. This keeps the TCB minimal and auditable while preserving
Python + Qt modifiability for everything above it.

## Implementation languages

**Policy: C where needed, Python where possible.**

- **In-process Weston extensions** — shell plugin, backends, any hot-path code
 Weston's main loop calls per-event or per-frame. Weston dlopens these as
 shared libraries and invokes C-ABI entry points. **C is the only credible
 choice**, and the C surface is kept small. Where upstream Weston exposes an
 embedded scripting hook instead — e.g. the lua-shell added in Weston 15,
 which scripts rule-based window management in Lua and ships a demo tiling
 shell — the script beats new C: it keeps the same runtime-editable
 transparency as the Python layer.

- **Out-of-process consumers and satellite tooling** — qdshell, per-uid
 policy tools, effects consumers, install-time bindings, tests, anything
 that talks to the compositor over Wayland or qbus. **Python (+ PyQt6 where
 UI matters).** Fast iteration, memory safety, rich testing libraries.

The split is load-bearing for the security posture: keep the TCB compact and
in C where auditability matters; push everything else outward where a different
language pays for itself.

## Chrome and content are independent

Window decorations (titlebar, borders, buttons, context menus) and the content
the user is working with (the application's `wl_surface`) are **independent
compositional units**, in the same sense that a browser's UI chrome and the
webpage are independent.

- **Decoration is owned by qdshell.** The four chrome `wl_surface`s
 (north / east / south / west) are created by qdshell as a normal
 `wl_compositor.create_surface` user, attached via private protocol, and
 re-painted on qdshell's own schedule. qdshell receives input events on
 those surfaces through standard `wl_pointer`, not a tunnelled extension.

- **Content is owned by the application.** The content `wl_surface` receives
 configures, frame callbacks, damage, and input through the standard
 `xdg_toplevel` path. The application is the only party that knows when to
 repaint.

- **The compositor doesn't bridge them.** qdwin doesn't repaint chrome when
 content changes, doesn't re-route events from one to the other, and doesn't
 cross-reference their lifecycles. It composites whichever buffer is
 currently attached to each surface.

Properties this guarantees:

1. **Hung content doesn't freeze chrome.** If the application stops responding,
 qdshell's chrome stays clickable.
2. **Hung chrome doesn't freeze content.** If qdshell crashes, the content
 surface keeps compositing and receiving input; it just loses its decoration
 until qdshell respawns.
3. **Per-surface redraw budgets.** Chrome repaints don't ratchet the content
 size or trigger client reconfigures; content repaints don't re-stream
 chrome buffers.
4. **Content-only forwarding works.** A per-view RDP stream can capture and
 forward just the content surface's pixels — the remote viewer sees the
 application output; the local user sees application + chrome composited
 together.

## Single-shell-client model

Each uid runs one qdshell process. qdshell:

- Binds `wl_seat` / `wl_pointer` directly; listens for chrome-surface input
 through standard Wayland.
- Paints chrome on its own schedule, driven by `toplevel_state` and
 `toplevel_geometry` events from qdwin.
- Hosts the panel, menus, notifications, and admin overlays.

Replacing qdshell with a different decorator (different glyphs, different
layout, different colour algebra) requires no compositor change. The chrome
is not a built-in libweston feature — it's just `wl_surface`s qdshell happens
to paint and place via the private protocol.

## Runtime environments

`wp_security_context_v1` and qdistro secctx tags provide authenticated client
identity metadata: sandbox engine, app id, instance id, silo, and process
identity. They are not isolation by themselves. The compositor still enforces
policy by deciding which clients may use privileged protocols such as
screencopy, virtual input, clipboard transfer, activation, and lock-time
capture.

The compositor must work across a wide range of graphics stacks — not just
"modern GPU on bare metal." A large share of qdistro development and a real
share of deployment happens inside VMs where GPU acceleration ranges from
"virgl with accel3d=yes" to "virtio-gpu without 3D" to "no GPU at all."
The compositor **does not require GPU acceleration**.

Target environments, in decreasing order:

1. **Bare metal / GPU passthrough.** Native Mesa EGL/GL. Reference target.
2. **VM with virtio-gpu + virgl.** Mesa virgl backend gives real GL
 acceleration. Fine for all workloads.
3. **VM with virtio-gpu only (accel3d=no).** Software rendering (pixman).
 Usable for static-mostly workloads. ~30% of one vCPU at 1080p / 30-60 Hz
 is the working budget.
4. **VM with no GPU / framebuffer-only.** Pixman + `headless` backend +
 RDP/PipeWire output. Equivalent to a "server-style install" — no local
 display, remote access only.

Implications for the compositor:

- **No renderer lock-in.** Auto-select between GL (native or virgl) and pixman
 at startup based on what the kernel/Mesa offers. libweston does this out of
 the box; the shell plugin contributes no renderer code and must not assume
 GL is present.
- **Effects are out-of-process.** Rich in-compositor effects disproportionately
 penalize software-rendered targets.
- **No assumption of >60 Hz compositing.** Animations live in Qt shell clients
 that can opt out per target.
- **VM dev loop is a first-class environment.** A commit that breaks the
 pixman path is a P0 regression regardless of how nice it looks on metal.

## Effects live outside the compositor

Rich per-window visual effects (blur, shadows, rounded corners, colour
transforms, magnifiers, recording overlays) are implemented as **separate
Wayland clients** that subscribe to per-view pixel streams, apply shaders in
their own process, and render the result as normal toplevel surfaces.

The qdistro compositor ships **no built-in effects framework**. This is the
opposite of KWin / Hyprland / Wayfire, which bake effect pipelines into the
compositor. It is the same direction Mutter and modern KWin have drifted:
effects, recording, and remote-desktop already run as separate daemons
consuming PipeWire screencast streams from the compositor.

Why:

1. **Every effect shader would enter the TCB.** GLSL compiler bugs, GPU driver
 state quirks, and shader misuse all become compositor-process
 vulnerabilities.
2. **User-supplied effects** force a choice: forbid them (boring) or load
 user GLSL into the TCB (effectively arbitrary-shader-execution privilege).
3. **Effect code review** belongs in the effects tool's repo, independent of
 the compositor.
4. **Crash blast radius** stays per-tool, not session-wide.

The shared primitive is a private `qdistro_view_stream_v1` Wayland protocol
that exposes per-view capture with peer-uid authorization. A uid's effects,
recording, or RDP tools see only that uid's windows. Admin override goes
through the broker with explicit approval and audit. Transport reuses
libweston's `backend-pipewire` for the common case; a DMA-BUF direct path
exists for low-latency same-GPU consumers.

**Simple-effects escape hatch.** A small patch to `gl-renderer.c` accepts a
per-surface 4×4 colour-matrix uniform, plumbed through the shell protocol. The
matrix is the *only* effect mechanism supported in-compositor. Allowed:
colour-matrix transforms (invert, tint, desaturate, hue shift) and per-surface
alpha. Forbidden: anything that samples neighbouring pixels (no blur, no
shadow, no convolution), and anything user-provided as raw GLSL.

## Vendored libweston

qdistro vendors a small libweston patchset alongside its plugin. The vendoring
is narrowly scoped: a `NULL`-parent fix in the popup-grab path that closes a
crash class for popups whose parent has gone away before the grab dispatches.
Stock libweston runs the compositor; the vendored `.so` is loaded only where
the patch is needed and the result is regression-tested against both stock
and vendored binaries.

This trade-off is preferred over a full fork because:

- The patch is small and reviewable.
- Upstream libweston is conservatively maintained — full forks rapidly diverge.
- Carrying a vendored `.so` is cheaper than carrying a fork.

## No scene graph

A recurring question is "libweston doesn't have a `wlr_scene` / scenefx /
KWin-style render pipeline. Isn't that a limitation?" The answer is no, *given*
the effects-outsourcing decision above.

Scene graphs in KWin / Hyprland / Wayfire serve features **inside** the
compositor: per-node effect metadata, inherited transforms in a tree,
render-to-texture subtrees, damage consolidation across hierarchy. Remove the
in-compositor effects (which qdistro outsources) and the remaining work — a
flat list of views with per-view damage — is exactly what libweston already
provides.

Three existing libweston consumers validate this:

- `desktop-shell` (4.9k LOC) — workspaces, panels, backgrounds, multi-output.
- `kiosk-shell` (1.5k LOC) — single-app-per-output, multi-seat.
- `agl-compositor` (11.8k LOC, production) — multi-display, multi-zone,
 window-handoff between zones, private policy protocols.

None of them reach for a scene graph.

## Hosting nested compositors

Two ways nested compositors attach:

1. **Native nested** (same machine, same admin compositor host). The nested
 compositor opens a Wayland connection to admin via its socket in admin's
 `XDG_RUNTIME_DIR`. Admin treats it as a single client with many top-level
 surfaces.
2. **Bridged nested** (container or different user). waypipe bridges a Wayland
 connection from the container/user into admin's compositor.

For multi-window apps inside containers, each nested top-level becomes a
native-feeling top-level in admin's compositor via the
`qdwin_nested_manager_v1` private protocol — better UX for IDEs than a single
window-containing-windows pattern.

The nested-compositor-per-big-app pattern means handing off a running IDE
doesn't need per-window gymnastics. The nested compositor is one outer client;
moving the whole thing between admin contexts is a single client migration
from admin's point of view. The IDE's N windows inside never notice because
their `wl_display` (to the nested compositor) never changed.
