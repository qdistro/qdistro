# 0001 — Ship qdwin against qdistro's vendored, patched libweston-16

- Status: ACCEPTED (2026-05-29)
- Scope: qdwin (libweston-vendored), qdistro (packaging/install/bootstrap)
- Supersedes the "Open follow-ups: decide install vehicle" note in
  `libweston-vendored/README.md`.

## Context

qdwin's layer-shell popup parenting (`zwlr_layer_surface_v1.get_popup`
+ `xdg_popup`) and the layer-popup pointer grab depend on four helper
symbols that do **not** exist in upstream / distro libweston-16:

- `weston_desktop_xdg_popup_attach_layer_parent`
- `weston_desktop_xdg_popup_get_geometry`
- `weston_desktop_xdg_popup_set_layer_grab_handler`
- `weston_desktop_xdg_popup_dismiss_layer_grab`

qdwin soft-links these via `dlsym(RTLD_DEFAULT, ...)` in
`qdwin/qdwin/qdwin.c`. When they are absent, qdwin fails the relevant
paths closed (`get_popup` →
`ZWLR_LAYER_SURFACE_V1_ERROR_INVALID_SURFACE_STATE`; layer-popup
`xdg_popup.grab` → `XDG_POPUP_ERROR_INVALID_GRAB`) and logs a DEGRADED
warning at startup. Quickshell popups anchored to layer-shell surfaces
(menus, the launcher, tray popups) therefore do not work on stock
libweston-16. See `doc/REBASE-WATCHPOINTS.md` entries 2–6.

The protocol handler that needed patching
(`weston_desktop_xdg_surface_protocol_get_popup`) is `static` and the
desktop subdir is compiled directly into the single
`libweston-16.so`; there is no separate `libweston-desktop.so`. So the
symbol address never crosses a public boundary and **cannot be
LD_PRELOAD-interposed**. The whole library must be replaced.

## Decision

Ship a privately-built, patched **libweston-16** as a self-contained
tree and have only qdwin's `weston` session load it. Do not touch the
distro's `/usr/lib64/libweston-16*` (other consumers keep the stock
library).

### Install vehicle

```
/usr/libexec/qdistro/qdwin-libweston/
  lib64/libweston-16.so.0[.0.0]        # patched core (+ symlinks)
  lib64/libweston-16/                   # backends from the SAME build
    drm-backend.so  pipewire-backend.so  rdp-backend.so
    wayland-backend.so  x11-backend.so  headless-backend.so
    gl-renderer.so  color-lcms.so  [xwayland.so]
```

`/usr/libexec/` is chosen over `/usr/lib64/` deliberately: it is
outside the default linker search path, so the patched `.so` can never
shadow the distro `libweston-16` for any other process. Only qdwin's
unit opts in via `LD_LIBRARY_PATH`. It is also outside the Tumbleweed
targeted-policy `lib_t` glob, matching the broker/pwd precedent.

### Wiring (set by `install-qdwin-session-for-vm.sh`)

`noctalia-session.service` (the qdwin `weston` unit):

```
Environment=LD_LIBRARY_PATH=/usr/libexec/qdistro/qdwin-libweston/lib64
Environment=WESTON_MODULE_MAP=drm-backend.so=/usr/libexec/qdistro/qdwin-libweston/lib64/libweston-16/drm-backend.so;...
```

**Core and backends must come from one build.** libweston's
core↔backend interface is an internal (unversioned) ABI. If the unit
loaded the patched core but distro backends (the old bug — the unit
set `LD_LIBRARY_PATH` to the qdistro tree but `WESTON_MODULE_MAP` still
pointed at `/usr/lib64/libweston-16/`), a libweston ABI bump within the
same SONAME would crash the backend at load. The install script now
maps every module to the vendored tree and only falls back to the
distro path module-by-module if the vendored tree is absent.

### Build

`qdwin/libweston-vendored/build-libweston.sh` gained a
`QDWIN_LIBWESTON_PROFILE=production` mode (vs the default `headless`
test profile). Production enables the full backend set + GL renderer +
lcms + xwayland and installs to a real prefix.
`qdistro/scripts/install/install-vendored-libweston.sh` builds that
profile (if needed) and stages `lib64/` into the install dest, with an
ABI sanity check against the system `weston` binary's `NEEDED` SONAME
and a guard that refuses a headless-only (non-shippable) prefix.
`fresh-vm-bootstrap.sh` runs the staging right after it builds qdwin.

### Version pinning

The vendored tree is pinned to the same upstream release the distro
ships (`libweston-vendored/VERSION` = 16.0.0, matching Tumbleweed's
`libweston-16` 16.0.0). On a distro libweston bump, rebase per
`libweston-vendored/README.md` "Bumping to a new weston version" and
re-verify `doc/REBASE-WATCHPOINTS.md`. The SONAME-major ABI check in
the install script catches a mismatched-major rebase early.

## Fallback / degradation

If `/usr/libexec/qdistro/qdwin-libweston/lib64/libweston-16/drm-backend.so`
is absent at session-install time, the unit is written against the
distro libweston. qdwin still starts (toplevels, cursor, bar) but the
soft-linked layer-popup paths fail closed and log DEGRADED. The CI gate
(`tests/gui/agent-protocol-audit.sh`) flags this as `GAP:` rather than
PASS, and `tests/gui/agent-vendored-libweston-verify.sh` asserts the
loaded library actually lives under `/usr/libexec/qdistro/`.

## Alternatives rejected

1. **LD_PRELOAD a shim** — impossible; the patched handler is `static`
   and not a public symbol (see Context).
2. **Patch the distro package / build an OBS RPM that replaces
   `libweston-16`** — would change libweston for every consumer
   (greetd's fallback weston, any other weston user) and couples the
   image to an OBS pipeline qdistro does not otherwise use. Rejected to
   keep the patch blast-radius to qdwin only.
3. **Carry the popup glue entirely in qdwin's shell plugin without
   libweston changes** — the geometry/grab/dismiss logic lives behind
   `static` libweston-desktop internals (`popup->parent`,
   `popup->geometry`, the protocol grab/reposition handlers); there is
   no public API to attach a non-xdg parent or to override the grab
   rejection. Not achievable without the library patch.

## Verification done at landing

- Production profile builds clean on the dev host
  (drm + wayland + x11 + headless + lcms link; GL/pipewire/rdp gated by
  host devel availability and covered by the VM dep list).
- All four soft-linked helper symbols are exported by the production
  `libweston-16.so.0.0.0` (`nm -D`).
- `install-vendored-libweston.sh` stages the tree with symlinks intact
  and passes the SONAME ABI check against the host `weston`.

## Still requires a live VM run

Layer-popup grab behaviour (journal `qdwin: layer-popup grab started`,
outside-click dismissal, no duplicate `popup_done`, no DEGRADED
warning) cannot be exercised headless. Run
`tests/gui/agent-vendored-libweston-verify.sh` +
`tests/gui/agent-protocol-audit.sh` against a VM that has this tree
installed. See `doc/REBASE-WATCHPOINTS.md` and qdistro
`todo/open-followups.md` "Plan3 live verification".
