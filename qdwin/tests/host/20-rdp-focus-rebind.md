# 20 — RDP-headless focus-listener rebind (autofocus propagates focus_signal)

**What**: lock the structural invariants that make `qdwin: focus handle=` /
`seat_focus_changed` fire on autofocus over the **RDP-headless** backend,
driven by the static-source test `qdwin/test_rdp_focus_rebind.py` (run as the
meson test `rdp-focus-rebind`). Covers, in `qdwin/qdwin.c`:

- `struct qdwin_seat_tracker` records the `weston_keyboard *` its
  `focus_signal` listener is bound to (`kbd_focus_listener_kbd`), so a backend
  keyboard swap is detectable;
- `qdwin_seat_tracker_rebind_focus_listener` reconciles the listener against
  the seat's *live* keyboard: no-op when already bound (idempotent / dedupe),
  removes any stale link before re-arming (never on two `focus_signal` lists
  at once — the weston-rdp re-init wedge), clears the tracked keyboard on
  removal;
- `qdwin_toplevel_autofocus_if_ready` rebinds the listener **before**
  `weston_view_activate_input` (so the emitted `focus_signal` reaches a live
  listener and XWayland receives real X11 focus), then calls the dedupe-safe
  immediate-emit backstop **after**;
- `qdwin_seat_emit_focus_now` keeps its `last_focused_handle` dedupe so the
  immediate-emit and the listener never both emit `seat_focus_changed` for the
  same handle (no double-emit on DRM or RDP);
- `qdwin_on_seat_updated_caps` routes keyboard-appeared/swapped through the
  same rebind helper.

**Why**: the RDP backend brings its seat up lazily inside
`rdp_peer_activate()` — `weston_seat_init()` fires `seat_created_signal`
(qdwin tries to install the focus listener) BEFORE
`weston_seat_init_keyboard()` exists, and the wl_keyboard can later be
swapped. If qdwin's `focus_signal` listener stays on a stale keyboard,
`weston_keyboard_set_focus` from autofocus never reaches it, so
`qdwin: focus handle=` never logs and qdshell's `focusedHandle` never updates
from autofocus alone (explicit `Tier3FocusIPC.injectFocus` was required — see
`todo/issues/qdwin/qdwin-rdp-focus-signal-not-propagating.md`). The rebind
makes the binding self-healing and ordering-independent; the dedupe keeps the
DRM path (which already has a live binding) from regressing into a
double-emit.

**Non-visual**: asserts on source structure + the meson test exit code.

## Headless limitation — live propagation is VM-only (read first)

The weston **headless** backend has no input backend and exposes **no
`wl_seat`/`wl_keyboard`** (verified — see tests/host/17-cursor-shape.md and
19-hotkey-edges.md). So there is no keyboard object to bind, swap, or fire a
real `focus_signal` on without the RDP backend. The end-to-end assertion —
"map the only toplevel over rdp-backend.so and observe `qdwin: focus
handle=...` WITHOUT `Tier3FocusIPC.injectFocus`" — therefore requires a live
RDP-headless VM and is PENDING residue. Here we pin the enforcement so a
regression that drops the rebind or the dedupe fails the host suite without a
VM.

## Procedure (headless, static-invariant half)

```
meson test -C build rdp-focus-rebind
# or, directly:
python3 qdwin/test_rdp_focus_rebind.py qdwin/qdwin.c
```

Expected: `PASS: RDP focus rebind (...)`, exit 0.

## Procedure (live, VM-only — PENDING)

On a VM with weston using `rdp-backend.so`:

```
weston-terminal &
sleep 2
journalctl --user -u noctalia-session | grep "qdwin: focus"   # must be non-empty
# and qdshell focusedHandle must update WITHOUT Tier3FocusIPC.injectFocus
```

vs the prior behaviour where that grep was empty on RDP but populated on DRM.

## Status

PASS (static-invariant half, headless). Live RDP-headless propagation:
VM-only, PENDING.
