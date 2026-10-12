# libweston-vendored delta audit — vendored `src/` vs upstream weston-16.0.0

Basis: `diff -rq` of upstream `weston-16.0.0.tar.xz` against
`qdwin/libweston-vendored/src/` at `main` `4888a4923` (vendored tree
unchanged since `e3eec64bd`; only `build-libweston.sh` changed in the
interim — offline display-info seeding + `--wrap-mode=nodownload`).

## Categories

### 1. Recorded functional changes (6 patches, applied in place)

Patches cover 5 source files:

- `include/libweston/meson.build`
- `libweston/backend-headless/headless.c` (inert seat, 96dpi)
- `libweston/backend-rdp/rdp.c` (extsecurity disable)
- `libweston/desktop/xdg-shell.c` (null-parent popup)
- `libweston/input.c` (inert relative pointer)

`0004-install-xwayland-api-header.patch` touches meson glue for the
`xwayland/` API header install.

### 2. UNRECORDED functional changes — not covered by any patch

Wider than previously documented (was "xdg-shell.c only"). Diff sizes in
`<`/`>` lines vs upstream:

| file | delta lines | note |
|---|---|---|
| `libweston/desktop/xdg-shell.c` | 404 | patches 0001 cover part; remainder is the four layer-popup helpers + related grab plumbing |
| `libweston/output-capture.c` | 192 | output-capture additions (pixelfeed path) |
| `libweston/backend-rdp/rdp.c` | 176 | patch 0006 covers part; remainder unrecorded |
| `libweston/backend-drm/kms.c` | 82 | drm backend changes |
| `include/libweston/desktop.h` | 39 | public API decls for the helpers |
| `libweston/input.c` | 36 | patch 0005 covers part; remainder unrecorded |
| `libweston/pixman-renderer.c` | 21 | |
| `libweston/renderer-gl/gl-renderer.c` | 11 | |
| `libweston/backend-headless/headless.c` | 11 | patches cover part |
| `include/libweston/backend-rdp.h` | 5 | |
| `libweston/backend-drm/drm-internal.h` | 5 | |
| `libweston/backend-drm/drm-kms-enums.h` | 2 | |
| `libweston/backend-rdp/rdp.h` | 1 | |
| `meson.build` | mixed | both stripping glue (subdir removals) and functional bits |

New vendored-only file: `libweston/desktop/qdwin-xdg-constrain.h`
(+ `releasing.md` at tree root, doc only).

**Consequence:** the six `.patch` files reconstruct only ~half the
delta. The only faithful "patch source" is the vendored tree itself —
exactly what astra predicted. The `weston-qdistro` derivation must
apply a *generated* full diff of these files onto upstream 16.0.0
(minus the meson stripping glue), not the recorded patches.

### 3. Stripping (upstream-only — to be REVERSED in weston-qdistro)

Top-level dirs absent from vendored: `frontend/` (the `weston`
executable), `desktop-shell/`, `ivi-shell/`, `kiosk-shell/`,
`lua-shell/`, `clients/`, `data/`, `doc/`, `man/`, `pam/`, `pipewire/`
(plugin), `remoting/`, `westinyplus/`, `coverage/`, `weston.ini.in`.
Partially stripped: `tests/` (vendored keeps only `tests/harness/`
subset), `xwayland/` (kept only `xwayland-internal-interface.h`).

### 4. Retained (identical to upstream)

`include/` (minus listed headers), `libweston/` backend dirs,
`shared/`, `protocol/`, `tools/`, `subprojects/*.wrap`,
`meson_options.txt`.

## Subproject pins (wrap-git — now offline-gated by build-libweston.sh)

| wrap | revision | nix treatment |
|---|---|---|
| `display-info` | `0.2.0` | system `libdisplay-info_0_3` satisfies `>=0.2.0 <0.4.0` — no fetch |
| `neatvnc` | `v1.0.0` | `backend-vnc=false` — unused |
| `aml` | `v1.0.0` | neatvnc dep — unused |
| `perfetto` | `v47.0` | tracing, optional — unused |

All builds use `--wrap-mode=nodownload` (parity with the new
`build-libweston.sh` hardening); every required dep resolves via
`dependency()` to nixpkgs.
