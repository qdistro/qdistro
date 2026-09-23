# qdwin libweston-vendored rebase watchpoints

Single source of truth for vendored libweston patches that must be
re-checked on every libweston rebase. Each entry names the upstream
file, the qdistro divergence, and a short test that proves the
divergence is still in force.

Rebase procedure: bring in upstream changes, run this list top to
bottom, fix anything that lost a divergence, then update the
"Last verified" date.

Last verified against vendored libweston: 2026-05-20 (plan3 landing).

---

## 1. Cursor input-region invariant

- Upstream: `libweston/input.c:pointer_cursor_surface_committed` clears
  `pending.input` and `input` on every commit (input.c:3521-3522).
- qdwin divergence: qdwin's `qdwin_install_cursor_sprite_view`
  (qdwin.c:8909+) clears the regions at install time, and the per-
  sprite commit listener added by plan3 M2 re-clears on every commit so
  shell-helper-driven recommits cannot leak a pickable input region.
- Test: `tests/host/test_zwlr_layer_shell.py::test_handshake` is not
  the right discriminator; the load-bearing assertion lives in
  `tests/gui/agent-cursor-clickthrough-smoke.sh` (clicks through a
  visible cursor sprite onto a known toplevel) and in the journal log
  line `qdwin: cursor-sprite commit re-cleared input` which fires only
  if the helper ever ships a non-empty input region.

## 2. NULL-parent xdg_popup at construction (layer-shell compatibility)

- Upstream: `libweston/desktop/xdg-shell.c:weston_desktop_xdg_surface_protocol_get_popup`
  posts `XDG_WM_BASE_ERROR_INVALID_POPUP_PARENT` whenever
  `parent_resource == NULL`.
- qdistro divergence: accepts NULL at construction; defers the
  spec-required error to commit time. The commit handler
  (`weston_desktop_xdg_popup_committed`, xdg-shell.c:1069+) gates on
  `popup->parent == NULL && popup->layer_parent_surface == NULL`,
  letting layer-shell-parented popups through.
- Test: `qdwin/qdwin/test_zwlr_layer_shell.py::test_null_parent_popup`
  reads `QDWIN_USE_VENDORED_LIBWESTON` and asserts the opposite outcome
  per env.

## 3. Layer-shell popup attachment helper

- Upstream: no `weston_desktop_xdg_popup_attach_layer_parent` exists.
- qdistro divergence: public helper exported from
  `libweston/desktop/xdg-shell.c:weston_desktop_xdg_popup_attach_layer_parent`,
  declared in `include/libweston/desktop.h`. Stores
  `popup->layer_parent_surface` so the commit gate accepts the popup.
- Test: any layer-shell `get_popup` path implicitly uses it; absence
  is observable in `qdwin: layer-shell get_popup` not appearing in the
  journal during `agent-protocol-audit.sh`.

## 4. Layer-shell popup geometry helper

- Upstream: no `weston_desktop_xdg_popup_get_geometry` exists.
- qdistro divergence: public read of `popup->geometry` so qdwin's
  layer-popup commit listener can position the view via
  `weston_view_set_position` (qdwin.c:qdwin_layer_popup_update_position).
- Test: layer-popup position relative to its layer-shell parent. The
  GUI scenarios open Quickshell popups; if geometry is read as `{0,0}`
  the popup floats at the top-left corner.

## 5. Layer-shell xdg_popup.grab handler (plan3 H1)

- Upstream: `weston_desktop_xdg_popup_protocol_grab` posts
  `XDG_POPUP_ERROR_INVALID_GRAB` for any `popup->parent == NULL`.
- qdistro divergence: when `layer_parent_surface != NULL` and a handler
  is registered via `weston_desktop_xdg_popup_set_layer_grab_handler`,
  libweston delegates to the compositor. qdwin's handler installs a
  pointer grab that dismisses on outside click via
  `weston_desktop_xdg_popup_dismiss_layer_grab`.
- Test:
  `qdwin/qdwin/test_zwlr_layer_shell.py::test_layer_popup_grab_stale_serial`
  proves stale serials are rejected with `xdg_popup` error 0
  (`INVALID_GRAB`).
  Live behaviour is covered by Quickshell tooltip/menu paths once they
  are scripted; until then the GUI gate watches the journal for
  `qdwin: layer-popup grab started`.

## 6. Layer-shell xdg_popup.reposition for layer-parented popups (plan3 M1)

- Upstream: `weston_desktop_xdg_popup_protocol_reposition` dereferences
  `popup->parent`.
- qdistro divergence: for layer-parented popups, geometry is computed
  from positioner alone (the parent argument is unused by
  `weston_desktop_xdg_positioner_get_geometry`) and a configure is
  scheduled. qdwin's commit listener re-positions the view on next
  commit.
- Test: `test_layer_popup_reposition` in `test_zwlr_layer_shell.py`.

## 7. qdwin layer-shell `ON_DEMAND` keyboard interactivity (plan3 M4)

- Not strictly a vendored-libweston watchpoint, but rebases of qdwin's
  default-pointer-grab need to preserve the ON_DEMAND focus transfer
  in `qdwin_proxy_default_grab_button` →
  `qdwin_layer_surface_handle_on_demand_button`. EXCLUSIVE focus is
  unconditional at map; NONE is a no-op.
- Test: any Quickshell popup that takes text input — the journal must
  show `qdwin: layer-shell ON_DEMAND focus -> ns=...`.

---

## Adding new entries

If you add a new patch to vendored libweston, add an entry here before
landing. Each entry should answer:

1. What does upstream do?
2. What does qdistro do differently?
3. What test fails if the divergence is lost?
