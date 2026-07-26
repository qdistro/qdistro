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
 `backend-pipewire`, and `weston_capture_v1` — mature codec, native audio and
 clipboard channels, matching Mutter, KDE screen-share, and WSL2's `wslg` path.
 In practice qdistro's shipped forwarding path is a standalone FreeRDP shadow
 server (`daemons/forward/qdistro-forward.c`), not weston's `backend-rdp`;
 `rdp-backend.so` is mapped in the compositor unit and used by diagnostics and
 the test harness, but it is not what carries product traffic. Availability of
 the upstream backend remains a reason to pick libweston; "RDP everywhere" is
 not a description of the current wiring.

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
| admin panel, menus |
| (Qt + QML; exactly one instance, admin's) |
| qdlocker: lock UI (separate PyQt process) |
+----------------------------------------------------+
| Out-of-process policy peers (Python / C++): |
| - qdistro-admin-broker (org.qdistro.AdminBroker1)|
| - qdistro-session-manager |
| - qdistro-polkit-agent |
| - qdshell's qml-plugin (C++), which speaks |
| qdwin_shell_v1 on qdshell's behalf |
+----------------------------------------------------+
| qdwin: libweston plugin (C) |
| - private protocol server (qdwin_shell_v1, |
| qdwin_locker_v1, secctx, nested manager) |
| - lock state + render policy |
| - holding-state + chrome compositing |
| - window placement / tiling policy |
+----------------------------------------------------+
| libweston core (C, upstream + vendored patches): |
| surfaces, input, output, DRM/KMS, damage |
| tracking, cursor, seat, XWayland, backends |
| (DRM, headless, wayland, RDP, PipeWire) |
+----------------------------------------------------+
```

Three things this diagram used to show that do not exist, called out because
they change what a reader assumes is enforced:

- **There is no Python-via-CFFI layer against libweston.** Lock state and
 render policy live in C inside `qdwin-shell.so`; the Wayland-side glue lives
 in C++ in `qdshell/qml-plugin/qdwin-binding.cpp`; qdlocker uses pywayland. The
 only CFFI artefact in the tree is a dead Phase-6.0 spike whose cdef surface is
 three log/version symbols, which no meson file, installer, or unit references.
- **There are no per-uid satellite clients.** See "Single-shell-client model".
- **"qbus-admin" is not a component.** The broker's bus name is
 `org.qdistro.AdminBroker1`; "qbus" is shorthand for qdistro's D-Bus
 conventions ([qbus.md](qbus.md)), not a daemon.
- **"Peer-uid enforcement" is not a property of the view-stream path.** See
 "Effects live outside the compositor" below. qdwin does enforce per-uid
 visibility on privileged *globals*, which is a narrower thing.

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

- **Out-of-process consumers and satellite tooling** — qdshell, qdlocker,
 policy daemons, effects consumers, tests, anything that talks to the
 compositor over Wayland or D-Bus. **Python (+ PyQt6 where UI matters)**, with
 C++ where a Qt/QML plugin is the right shape (qdshell's `qml-plugin` speaks
 the raw Wayland protocol). Fast iteration, memory safety, rich testing
 libraries.

The split is load-bearing for the security posture: keep the TCB compact and
in C where auditability matters; push everything else outward where a different
language pays for itself.

## Chrome and content are independent

> **Status: the protocol ships; no client uses it.** `attach_decoration` and the
> four-surface chrome model are implemented in `qdwin-shell-v1.xml` and in the
> qdwin server, and exercised by the C test client
> (`qdwin/test-client/qdwin-probe.c`). **Nothing in qdshell calls it.**
> `qdwin-binding.cpp` has no `attach_decoration` path, and its `chrome_button`
> listener is an empty stub with no QML consumer. qdwin says so itself at the
> `toplevel_added` path: "qdshell … does not attach qdwin SSD chrome for
> ordinary local applications", and it deliberately does not require a
> decoration request before a normal surface becomes visible.
>
> **What this means in practice.** Ordinary local applications on qdistro are
> **undecorated by qdwin** and fall back to whatever client-side decoration they
> draw themselves. qdshell does set a per-toplevel silo colour on qdwin
> (`setBorderColor` → `qdwin_toplevel_border_rgba`), but qdwin's only reader of
> that field is the `attach_decoration` handler — with no client attaching
> decoration, the colour is stored and never painted. qdwin's own source calls
> the SSD path "stub today". So there is no compositor-drawn trusted chrome
> carrying silo identity on a v1 desktop; the four properties listed below are
> properties of the design and are vacuous until a decorating client exists.

Window decorations (titlebar, borders, buttons, context menus) and the content
the user is working with (the application's `wl_surface`) are designed as
**independent compositional units**, in the same sense that a browser's UI
chrome and the webpage are independent.

- **Decoration is intended to be owned by qdshell.** The four chrome
 `wl_surface`s (north / east / south / west) would be created by qdshell as a
 normal `wl_compositor.create_surface` user, attached via private protocol, and
 re-painted on qdshell's own schedule. qdshell would receive input events on
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

> **Status: there is exactly one qdshell — admin's.** Per-uid qdshell instances
> do not exist. The only per-uid unit is `qdshell-session-<name>@<uid>.service`,
> whose helper joins the silo cgroup, drops privileges, and execs
> `dbus-run-session sleep infinity` as a cgroup keep-alive; its own header says
> "Real qdshell wiring layers on top of this in a follow-up task." A silo runs
> no shell and no compositor client
> ([sessions.md](sessions.md#d-bus-surface)).
>
> The security reading: qdwin's single bound shell is a single point of trust
> today, and `qdwin_shell_require_bound()` — the gate on the private protocol —
> means "is this admin's qdshell", not "is this the shell for the uid that owns
> the object being asked about". Any doc statement that relies on separating one
> uid's shell from another's is describing a future topology.

The design is one qdshell process per uid. qdshell:

- Binds `wl_seat` / `wl_pointer` directly; listens for chrome-surface input
 through standard Wayland.
- Paints chrome on its own schedule, driven by `toplevel_state` and
 `toplevel_geometry` events from qdwin (design; see the chrome status note
 above — no chrome is attached today).
- Hosts the panel, menus, notifications, and admin overlays.

Replacing qdshell with a different decorator (different glyphs, different
layout, different colour algebra) requires no compositor change. The chrome
is not a built-in libweston feature — it's just `wl_surface`s qdshell happens
to paint and place via the private protocol.

## Runtime environments

`wp_security_context_v1` and qdistro secctx tags provide authenticated client
identity metadata: sandbox engine, app id, instance id, silo, and process
identity. They are not isolation by themselves. The compositor enforces policy
by deciding which clients may see and use privileged protocols. Precisely what
that covers:

| Surface | Enforced? |
|---|---|
| Input method / virtual keyboard | **Yes** — `qdwin_global_visible()` hides the globals from secctx-tagged silo clients, and both bind handlers go through `qdwin_ime_family_bind_allowed`, a fail-closed uid + exe pin that rejects before the resource is created. |
| Security-context manager | **Yes** — visible only to the bound shell or the authorized `qdistro-secctx-exec` helper. |
| Clipboard transfer | **Yes** — set-time and receive-time gates into the broker ([clipboard.md](clipboard.md)), with the caveats recorded there. |
| `xdg_activation_v1` | **Yes** — cross-uid activation stalls on a fail-closed broker check. |
| `weston_capture_v1` (whole-output pixels) | **Yes, doubly.** The global is hidden from every client but the bound shell by `qdwin_global_visible()`. Separately, libweston defers the capture *attempt* to a screenshot authority, and qdwin registers one **only** when `QDWIN_ENABLE_SHELL_CAPTURE=1` and `geteuid() == allowed_uid` — a dev/test opt-in that logs a WARNING, is emitted into the compositor unit only when the installer's caller exports it, and is explicitly `unset` by `qdistro-bootstrap.sh` and `image/config.sh`. **On a production install no authority is registered, so capture attempts hit libweston's fail-closed default and are denied.** When the opt-in is on, `qdwin_capture_auth_cb` re-checks the exact bound-shell `wl_client` and a single designated output at execution time, and authorizes nothing else. |
| Per-view capture (`qdwin_view_stream_v1`) | **No per-uid authorization.** See "Effects live outside the compositor". |
| Lock-time capture | **No gate exists.** Lock state is not consulted on any capture or virtual-input path ([sessions.md](sessions.md)). |
| screencopy | **Not applicable — qdistro implements no screencopy protocol.** There is no wlr-screencopy in the tree; earlier wording here named a protocol that does not exist. |

The compositor must work across a wide range of graphics stacks — not just
"modern GPU on bare metal." A large share of qdistro development and a real
share of deployment happens inside VMs where GPU acceleration ranges from
"virgl with accel3d=yes" to "virtio-gpu without 3D" to "no GPU at all."
The compositor is written so as not to depend on GPU acceleration — but see the
renderer note below: **what ships pins `renderer=gl`**, so the pixman targets
listed here are a design goal that the shipped configuration does not currently
select.

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

- **No renderer lock-in *in the plugin*.** The shell plugin contributes no
 renderer code and must not assume GL is present — that part is true and is
 the property worth keeping.

 **But the shipped configuration is not auto-selecting.**
 `install-qdwin-session-for-vm.sh` writes `renderer=gl` into `weston.ini`
 unconditionally, and `deploy/qdwin-compositor.service` documents why: GL is
 required for the virtio-gpu hardware cursor plane, because libweston only
 allocates the GBM cursor BOs on the GL path, and under pixman the cursor is
 software-composited into the scanout and doubles with the SPICE host cursor.
 GL runs on a software-only virtio-gpu via llvmpipe-over-GBM. There is no
 startup auto-select between GL and pixman. Target 4 above ("no GPU /
 framebuffer-only") is therefore untested-as-shipped, and a commit that breaks
 the pixman path will not be caught by the default install.
- **Effects are out-of-process.** Rich in-compositor effects disproportionately
 penalize software-rendered targets.
- **No assumption of >60 Hz compositing.** Animations live in Qt shell clients
 that can opt out per target.
- **VM dev loop is a first-class environment.** A commit that breaks the
 pixman path is a P0 regression regardless of how nice it looks on metal.

## Effects live outside the compositor

Rich per-window visual effects (blur, shadows, rounded corners, colour
transforms, magnifiers, recording overlays) are **designed to be** separate
Wayland clients that subscribe to per-view pixel streams, apply shaders in
their own process, and render the result as normal toplevel surfaces. **No such
client exists in the tree**; the only `subscribe_view_stream` callers are a C
test client and the VM-gated multimachine components. The architectural
decision below is real and load-bearing; the effects tooling it anticipates is
unwritten.

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

The shared primitive is a private Wayland protocol —
`qdwin_view_stream_v1`, obtained via `qdwin_shell_v1.subscribe_view_stream`
(this page previously named it `qdistro_view_stream_v1`, which does not exist).

> **Status: per-view capture has no peer-uid authorization.** The only gate on
> `qdwin_handle_subscribe_view_stream` is `qdwin_shell_require_bound()`. Once a
> client is the bound shell, it may subscribe to **any** toplevel handle,
> regardless of which uid owns that window. `qdwin-shell-v1.xml` states this
> outright: "Admin approval is NOT done here … qdwin trusts its bound shell …
> A future non-shell caller would gate behind a separate allowed-uid check."
> There is no broker call, no approval, and no audit row on this path.
>
> **The residual risk this leaves.** The claim "a uid's effects, recording, or
> RDP tools see only that uid's windows" is **not** enforced by the compositor.
> What contains it today is topology, not policy: there is exactly one bound
> shell (admin's), silos run no shell, and no effects or recording client
> exists — so there is currently no non-admin subscriber to constrain. That is a
> containment-by-absence argument, and it expires the moment a per-uid shell or
> a second view-stream consumer ships. The allowed-uid check the protocol
> anticipates must land before then.
>
> One thing the path *does* enforce per-stream: `allow_input` is per handle and
> fail-closed. With `allow_input=0` the stream keeps its pixels and its
> per-stream seat (for focus locking) but every injected event is dropped at the
> `inject_*` boundary — pointer motion, buttons, keyboard **and axis/scroll**
> alike. A read-only export cannot be driven by the remote subscriber. See
> [window-handoff.md](window-handoff.md) for the handoff-side view of the same
> mechanism.

Transport reuses libweston's `backend-pipewire` for the common case. **There is
no DMA-BUF direct path**: `dmabuf` does not appear in `qdwin.c`, in
`qdwin-shell-v1.xml`, in `daemons/forward/`, or in qdshell's qml-plugin. Adding
one for low-latency same-GPU consumers remains a design option, not a shipped
capability.

**Simple-effects escape hatch — designed, not built.** The plan is a small patch
to `gl-renderer.c` accepting a per-surface 4×4 colour-matrix uniform, plumbed
through the shell protocol, as the *only* effect mechanism supported
in-compositor: colour-matrix transforms (invert, tint, desaturate, hue shift)
and per-surface alpha allowed; anything that samples neighbouring pixels (blur,
shadow, convolution) and anything user-provided as raw GLSL forbidden.

None of it exists. The vendored `gl-renderer.c` carries exactly one qdistro
edit and it is capture retention, not a colour matrix. There is no per-surface
matrix uniform, no per-surface alpha request, and no shell-protocol request to
carry either. The *policy* — no user GLSL in the TCB, no neighbour-sampling
effects — is the part that is decided; the mechanism is unimplemented.

## Vendored libweston

qdistro carries a **full weston-16 source tree** under
`qdwin/libweston-vendored/src`, patched in place, and builds its own
`libweston-16.so.0`, `drm-backend.so` and `gl-renderer.so` from it. Both of the
claims this section used to make — "narrowly scoped to a `NULL`-parent popup
fix" and "stock libweston runs the compositor" — are wrong, and both understated
how much of the TCB qdistro owns.

**What is actually patched.** Four changes are recorded as reproducible
`.patch` files (`0001` NULL-parent xdg_popup, `0002` headless inert seat,
`0003` headless 96-dpi physical size, `0004` install the XWayland API header),
plus in-tree edits that have no `.patch` record: a positioner-snapshot security
change with a new `qdwin-xdg-constrain.h` kernel under
`src/libweston/desktop/`, a new public API entry in
`include/libweston/desktop.h`, a virtio-gpu cursor-hotspot fix in
`backend-drm/kms.c`, and a capture-retention edit in
`renderer-gl/gl-renderer.c`.

**What is actually loaded.** `deploy/qdwin-compositor.service` pins the vendored
build **unconditionally**, not "only where the patch is needed":
`LD_LIBRARY_PATH` points at `/usr/libexec/qdistro/qdwin-libweston/lib64`, and
`WESTON_MODULE_MAP` routes `drm-backend.so` and `gl-renderer.so` to the vendored
copies. (The remaining backends — headless, pipewire, rdp, wayland, x11,
xwayland, color-lcms — stay on the distro packages.) The DRM backend must run
against the vendored core it was built against, so this is not separable.

**The residual risk this leaves.** qdistro is on the hook for a weston fork's
security maintenance, not a patch's: distro updates to `libweston-16` do **not**
reach the compositor's core, DRM backend, or GL renderer. Rebasing on an
upstream weston bump is a tree-level operation. The in-tree edits with no
`.patch` record are the ones most likely to be lost in such a rebase, and the
positioner-snapshot change is security-relevant.

The original rationale for vendoring rather than LD_PRELOADing still holds and
is worth keeping: there is no separate `libweston-desktop.so`, and the
protocol handler that needed patching
(`weston_desktop_xdg_surface_protocol_get_popup`) is `static`, so its address
never crosses a public symbol boundary and cannot be interposed. Replacing the
whole library was the only shape that worked. What changed is that the
replacement has since grown well past one patch.

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
