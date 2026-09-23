# qdshell ↔ qdwin Wayland protocol matrix

Single, checked-in matrix of the Wayland protocols qdshell / Quickshell
needs and qdwin's support for each. It is the source of truth the CI
gates assert against (`tests/gui/agent-protocol-audit.sh` checks the
"Advertised global" column; `tests/gui/agent-vendored-libweston-verify.sh`
checks the libweston-backed popup rows).

Last reviewed: 2026-05-29 (qdwin main `69dfd7a`; qdshell main `c407b2a51`).

How to refresh: re-run the static scan in
`agent-protocol-audit.sh` (it greps qdshell for layer-shell / popup /
cursor / clipboard usage) and re-read the qdwin global versions from
`qdwin/qdwin/qdwin.c` (`wl_global_create` calls around L16750–17025).

Legend:
- Source: `libweston` = baseline from libweston core/desktop;
  `qdwin` = a global qdwin creates itself.
- Status: OK = working on the shipped (vendored-libweston) build;
  VENDORED = works only against qdistro's patched libweston (see
  `doc/decisions/0001-vendored-libweston-packaging.md`); GATED = works
  but behind an explicit allow/deny policy.

## Core / baseline (Quickshell window + buffer plumbing)

| Protocol | Ver | Source | qdshell / Quickshell use | qdwin status | Notes |
| --- | --- | --- | --- | --- | --- |
| `wl_compositor` | 5 | libweston | every QML surface | OK | vendored libweston-16 pins v5 (`compositor.c` `wl_compositor_interface, 5`); v6 `preferred_buffer_scale`/`preferred_buffer_transform` events not emitted — toolkits fall back |
| `wl_shm` | 1+ | libweston | software buffers (pixman path) | OK | |
| `wl_seat` | — | libweston | pointer / keyboard input | OK | single-seat per session |
| `wl_output` | — | libweston | `Quickshell.screens`, per-monitor panels | OK | |
| `wl_subcompositor` | 1 | libweston | Qt subsurfaces | OK | |
| `wp_viewporter` | 1 | libweston | Qt scaling / crop | OK | compositor.c:9584 |
| `wp_presentation` | — | libweston | frame timing | OK | |
| `wl_data_device_manager` | 3 | libweston | drag-and-drop + clipboard | GATED | qdwin private clipboard gate; see Data device row |
| `xdg_wm_base` | — | libweston-desktop | Qt toplevels + xdg popups | OK | popup parent glue: see layer-popup rows |

## Shell surfaces

| Protocol | Ver | Source | qdshell / Quickshell use | qdwin status | Notes |
| --- | --- | --- | --- | --- | --- |
| `zwlr_layer_shell_v1` | 5 | qdwin | `WlrLayershell` / `PanelWindow` — bar, OSD, notifications, menus, overlays (56 QML refs) | OK | configure/map/stacking, exclusive zones, `exclusionMode` |
| `zwlr_layer_surface_v1` keyboard interactivity | — | qdwin | `WlrKeyboardFocus` (`None`/`OnDemand`/`Exclusive`) — launcher/menus take text input | OK | EXCLUSIVE unconditional at map; ON_DEMAND focus transfer on button (plan3 M4); journal `qdwin: layer-shell ON_DEMAND focus` |
| `zwlr_layer_surface_v1.get_popup` + `xdg_popup` | — | qdwin + libweston | `PopupWindow` anchored to a `PanelWindow` (menus, launcher, tray popups) | **VENDORED** | attaches layer-surface parent at popup commit via `weston_desktop_xdg_popup_attach_layer_parent` (xdg-shell.c:1207). Stock libweston-16 → `INVALID_SURFACE_STATE` |
| `xdg_popup.grab` on layer-parented popup | — | qdwin + libweston | popup grab → outside-click dismiss | **VENDORED** | `weston_desktop_xdg_popup_set_layer_grab_handler` / `..._dismiss_layer_grab`; journal `qdwin: layer-popup grab started`. Stock → `INVALID_GRAB` |
| `xdg_popup.reposition` on layer-parented popup | — | qdwin | menu re-anchor | VENDORED | geometry from positioner alone (plan3 M1); no new symbol but only meaningful on layer-parented popups |
| `xdg_toplevel` (ordinary local apps) | — | libweston-desktop + qdwin | terminals/apps launched from the shell | OK | qdwin releases holding by default (`default_toplevel_policy`); qdshell does NOT attach qdwin SSD chrome to make a normal app visible. Nested/proxy keep explicit allow/deny (GATED) |
| `xdg_decoration` (`zxdg_decoration_manager_v1`) | — | qdwin | Qt toolkit decoration negotiation | OK | qdwin advertises so toolkits request server-side and don't self-draw |

## Cursor / scaling

| Protocol | Ver | Source | qdshell / Quickshell use | qdwin status | Notes |
| --- | --- | --- | --- | --- | --- |
| `wp_cursor_shape_manager_v1` | 2 | qdwin | `cursorShape` on widgets (79 QML refs) | OK | theme sprites; **cursor-surface input regions cleared at install + on every commit** so the sprite cannot eat clicks meant for layer-shell UI (REBASE-WATCHPOINTS #1; journal `qdwin: cursor-sprite commit re-cleared input`) |
| `wp_fractional_scale_manager_v1` | 1 | qdwin | HiDPI fractional scale | OK | paired with `wp_viewporter` |

## Activation / idle

| Protocol | Ver | Source | qdshell / Quickshell use | qdwin status | Notes |
| --- | --- | --- | --- | --- | --- |
| `xdg_activation_v1` | 1 | qdwin | focus-steal / launch focus | GATED | activation token issue + redeem; policy hooks in the qdwin qdshell plugin (not all surfaced to QML). See `tests/host/09-xdg-activation-gating.md` |
| `ext_idle_notifier_v1` | 2 | qdwin | `Quickshell` idle (144 `Idle` refs); qdlocker idle-to-lock | OK | internal-idle mode (`weston.ini idle-time=0`) so subscriber timeouts honoured |
| `zwp_idle_inhibit_manager_v1` | 1 | qdwin | inhibit during media/presentation | OK | |

## Clipboard / selection / capture

| Protocol | Ver | Source | qdshell / Quickshell use | qdwin status | Notes |
| --- | --- | --- | --- | --- | --- |
| `wl_data_device` selection | 3 | libweston + qdwin | clipboard, `cliphist` (231 `clipboard` refs) | GATED | qdwin private clipboard gate; qdshell receive-time gate listener still stubbed in the plugin (tracked) |
| `zwp_primary_selection_device_manager_v1` | — | qdwin | middle-click paste | OK | |
| `zwlr_screencopy_manager_v1` | — | — | screenshots / `ScreencopyView` | **ABSENT** | qdwin does NOT advertise wlr-screencopy (no such global anywhere in the source tree). Whole-output capture is the env-gated weston `weston_capture_v1`/`weston-screenshooter` path (`QDWIN_ENABLE_SCREENSHOOTER=1`, dev/test only); qdwin's own forwarding uses the private §6.5 `qdwin_view_stream_v1` (PipeWire). Third-party `grim`/portal-wlr screencopy clients will not find a global |
| `zwp_linux_dmabuf_v1` | — | libweston | GPU buffer import (GL clients) | OK | linux-dmabuf.c; advertised when a GL renderer is active |

## qdistro-private

| Protocol | Ver | Source | qdshell use | qdwin status | Notes |
| --- | --- | --- | --- | --- | --- |
| `qdwin_shell_v1` | 26 | qdwin | toplevel tracking/focus, wm-policy, idle/DPMS, keybind registration, view streams | OK | the qdshell ↔ qdwin private contract (`qdwin-shell-v1.xml`); bound via the `Qdistro.Qdwin` QML plugin |
| `qdwin_nested_manager_v1` | — | qdwin | tier-4 nested proxy sessions | GATED | secctx-bound allow/deny |
| `qdwin_locker_v1` | — | qdwin | lock screen | GATED | locker bind gate (`tests/host/07-locker-bind-gate.md`) |
| `wp_security_context_manager_v1` | — | qdwin | sandbox secctx tagging | GATED | feeds the bind gates above |

## Required-globals contract (CI gate)

`agent-protocol-audit.sh` fails if any of these are NOT advertised by a
running qdwin session (they are the minimum for qdshell to render and
interact):

```
wl_compositor  wl_shm  wl_seat  xdg_wm_base
zwlr_layer_shell_v1  wp_cursor_shape_manager_v1
wp_fractional_scale_manager_v1  qdwin_shell_v1
```

The VENDORED rows above are **not** in the hard required-globals list
(the global is advertised regardless); they are asserted at runtime by
`agent-vendored-libweston-verify.sh` (loaded library path) +
`agent-protocol-audit.sh` (no null-parent crash, `layer-popup grab
started` present, no DEGRADED warning). Those need a live VM with the
vendored tree installed — see the decision doc + REBASE-WATCHPOINTS.

## Gaps / follow-ups (live, not regressions)

1. qdshell's `wl_data_device` receive-time gate listener is still a
   stub in the qdwin qdshell plugin — clipboard gating is enforced
   compositor-side only.
2. `xdg_activation_v1` policy hooks are not fully surfaced to QML.
3. Pointer-config / xkb-repeat live-apply needs a `qdwin_shell_v1`
   request bump (see qdistro `todo/open-followups.md`) — orthogonal to
   the Quickshell protocol set.
