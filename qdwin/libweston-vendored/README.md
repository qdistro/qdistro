# libweston-vendored — patched libweston-16 for qdwin

A privately-built copy of `libweston-16.so.0` carrying the
**NULL-parent xdg_popup** patch
(`0001-allow-null-parent-xdg-popup.patch`). The patch lifts an
assertion that prevents popup surfaces from being attached without
a parent; qdwin's shell client needs root-level popups for
Quickshell-style floating menus.

## Why a private libweston instead of LD_PRELOADing libweston-desktop?

There is no separate `libweston-desktop.so`. The desktop subdir is
`subdir()`'d into the single `lib_weston = shared_library('weston-16',
...)`, and the protocol handler we need to patch
(`weston_desktop_xdg_surface_protocol_get_popup`) is `static` — its
address never crosses a public symbol boundary, so it cannot be
LD_PRELOAD-interposed. Replacing the whole libweston is the only
shape that works. See the design note for full reasoning.

## Layout

```
VERSION                              # "16.0.0" (must match Tumbleweed package)
0001-allow-null-parent-xdg-popup.patch
src/                                 # weston @ tag 16.0.0 with desktop/xdg-shell.c patched in-place
build-libweston.sh                   # one-shot build wrapper
README.md                            # this file
```

The patch is **already applied to `src/libweston/desktop/xdg-shell.c`**.
The `.patch` file is the reproducible record so we can regenerate the
tree against a future weston bump (apply with `patch -p1` from
`src/`).

## Build

```sh
cd ${QDWIN_REPO}/libweston-vendored
./build-libweston.sh
```

Output: `src/build/libweston/libweston-16.so.0.0.0` (~1.7 MB).
SONAME `libweston-16.so.0` — the LD_LIBRARY_PATH trick relies on this
matching the system library exactly.

Required system devel packages:
- `wayland-devel`, `libxkbcommon-devel`, `libpixman-1-0-devel`,
  `libinput-devel`, `libdrm-devel`, `lcms2-devel`
- `libevdev-devel` is NOT required for this minimal config (we strip
  all backends except headless and don't link libinput-backend.a).

## Use from qdwin

```sh
LD_LIBRARY_PATH=${QDWIN_REPO}/libweston-vendored/src/build/libweston${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH} \
  /usr/local/bin/qdwin --backend=headless ...
```

For an installed-tree layout (Phase 7+), copy
`src/build/libweston/libweston-16.so.0.0.0` plus its symlinks to
`/usr/libexec/qdistro/qdwin-libweston/` and have the qdwin systemd
unit set `Environment=LD_LIBRARY_PATH=/usr/libexec/qdistro/qdwin-libweston`.

Verify the right copy is loaded after start with:

```sh
pmap $(pgrep qdwin) | grep libweston-16.so
# expect path under qdistro/, NOT /usr/lib64/
```

## Verifying the patch is live

After qdwin starts with the vendored library in `LD_LIBRARY_PATH`,
have a client send `xdg_surface.get_popup(parent=NULL)` — e.g. via
the smoke harness in `qdwin/test_zwlr_layer_shell.py` (a
new `test_null_parent_popup` case is required and is the next TODO).

Stock libweston response: client disconnects with
`xdg_surface#NN: error 3: popup parent must be non-null`.
Patched response: no error on get_popup; if the client commits without
setting a parent via another protocol, error is posted at commit-time
with message `popup parent must be set before commit`.

## Bumping to a new weston version

1. `cd $HOME/doc/weston && git fetch && git tag --list "16.*"`
2. Update `VERSION` in this directory.
3. Re-extract: `cd src && rm -rf * && git -C $HOME/doc/weston archive 16.0.X | tar -x`
4. Strip again: `rm -rf clients data desktop-shell doc fullscreen-shell ivi-shell kiosk-shell man notes.txt pipewire remoting tests pam wcap weston.ini.in` (keep tools/, frontend/include/ if needed)
5. Apply patch: `patch -p1 < ../0001-allow-null-parent-xdg-popup.patch`
6. Rebuild: `./build-libweston.sh`

If patch fails: re-edit `xdg-shell.c` by hand, then regenerate the
patch with `diff -u`.

## Host-side gate

`run-null-parent-test.sh` (in this directory) spawns headless weston
twice — once with the system libweston, once with the vendored .so
via the meson build prefix `/tmp/qdwin-libweston-prefix` — and runs
`qdwin/test_zwlr_layer_shell.py` against each. Stock
should report `null_parent_popup` as a deliberate rejection; vendored
should report it as accepted. Use this as the regression gate
whenever `0001-allow-null-parent-xdg-popup.patch` or its rebase
target moves.

## Smoke and protocol-test wiring

Both `compositor/run-noctalia-smoke.sh` and
`qdwin/run-protocol-tests.sh` honour
`QDWIN_USE_VENDORED_LIBWESTON=1`. They prepend
`$QDWIN_VENDORED_LIBWESTON_PREFIX/lib64` (default
`/usr/libexec/qdistro/qdwin-libweston`) to LD_LIBRARY_PATH and
propagate the same env to the python test client so
`test_null_parent_popup` flips its expected outcome to "accepted".

To exercise this in-VM the `.so` plus its symlinks plus the
matching `headless-backend.so` need to be deployed under that
prefix path. The host build script's `ninja install` step lays
them out correctly under `/tmp/qdwin-libweston-prefix/`; rsyncing
that tree into `/usr/libexec/qdistro/qdwin-libweston/` in a VM is
sufficient.

## Production build profile + install vehicle (DECIDED)

The install vehicle is `/usr/libexec/qdistro/qdwin-libweston/` — see the
decision record `qdwin/doc/decisions/0001-vendored-libweston-packaging.md`.

`build-libweston.sh` has two profiles via `QDWIN_LIBWESTON_PROFILE`:

- `headless` (default) — minimal headless-only build used by the
  host-side protocol tests (`run-null-parent-test.sh`,
  `run-protocol-tests.sh`); installs under `/tmp/qdwin-libweston-prefix`.
- `production` — full backend set (drm + pipewire + rdp + wayland + x11
  + headless + GL + lcms + xwayland) for shipping; installs under
  `/tmp/qdwin-libweston-prod-prefix` by default. This is what qdistro's
  `scripts/install/install-vendored-libweston.sh` builds (on demand) and
  stages into `/usr/libexec/qdistro/qdwin-libweston/`.
  `fresh-vm-bootstrap.sh` runs that staging right after building qdwin,
  and `install-qdwin-session-for-vm.sh` points the qdwin `weston` unit's
  `LD_LIBRARY_PATH` + `WESTON_MODULE_MAP` at the staged tree (core AND
  backends from the same build).

Headless CI gate (no VM): `run-production-symbols-test.sh` builds the
production profile and asserts the four soft-linked
`weston_desktop_xdg_popup_*` helper symbols are exported and the staging
script lays a complete tree. Wired into qci as
`host/qdwin-vendored-libweston-symbols`.

## Open follow-ups

- Upstream the patch to weston master; spec wording supports it.
- Live-VM grab/dismiss discriminators:
  `qdwin/tests/gui/agent-vendored-libweston-verify.sh` (needs a VM with
  this tree installed — see the decision doc).
