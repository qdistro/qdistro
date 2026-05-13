# Noctalia-on-qdwin GUI test runner

Sibling of `qdwin/tests/gui/AGENTS.md`. The qdwin/ harness
drives the Python qdshell.py (current shell). This harness drives
**Noctalia** as the QML shell layer running on top of qdwin's
zwlr_layer_shell_v1.

Used to validate layer-shell completeness and (later) the
 strip pass against the qdshell fork.

## Roles

Same orchestrator/runner split as the sibling harness.

## Environment

- Target VM: a qdwin VM with `noctalia-shell` + `noctalia-qs` packages
 installed and `noctalia-shell.service` (user unit) enabled.
- Default VM name pattern: `noctalia-vis-YYMMDD-HHMM`. The reference
 setup VM as of 2026-05-03 is `noctalia-vis-260503-1021` (clone of
 `weston-desktop-260422-1604`-derived `layershell-260502-2322`).
- Resolution: 1920×1080 (set in `weston.ini` `[output]`).
- Backend: `drm-backend.so` with `renderer=pixman` (no GL — virtio-gpu
 in our VMs has no accel3d; Mesa GLES backend segfaults).
- Auth: admin / $QDISTRO_VM_PASSWORD; admin user must be in `video,input,render,seat`
 groups; `seatd.service` must be running on the VM.

## Helper script

Reuse `qdwin/tests/gui/qdwin-helpers.sh`. The qdshell-specific
helpers (`qdwin_ctrl`, `qdwin_session_healthy`) don't apply here —
Noctalia has no equivalent ctrl-socket. Instead use:

- `qdwin_screenshot <out.png>` — virsh screenshot wrapper, generic
- `qdwin_send_key`, `qdwin_qmp_key`, `qdwin_chord` — keyboard
- `qdwin_mouse_move`, `qdwin_click`, `qdwin_mouse_button` — pointer
- `noct_session_healthy` — defined in `noctalia-helpers.sh` (this
 dir) — checks the `noctalia-shell.service` user unit is active +
 qs process is alive

## What works (and what doesn't) on Noctalia

| Surface | Works? | Notes |
|---|---|---|
| Bar visible at top | ✅ | layer 2, 1920×31 |
| Wallpaper visible | ✅ | layer 0 (BACKGROUND) |
| Left-click bar widgets opening **layer-surface panels** | ✅ | Noctalia opens settings/control-center as new layer surfaces |
| Cursor visible / moves | ✅ | virtio-gpu cursor plane |
| Keyboard typing into bar's textbox widget | ⚠️ untested | should work via `qdwin_send_key` |
| **Right-click → context menu (xdg_popup)** | ❌ **BLOCKED** | weston rejects NULL-parent xdg_popup; see . Triggering it crashes Noctalia. **Avoid in scenarios.** |
| **Tray dropdown menus** | ❌ **BLOCKED** | same weston gap |
| DPMS off after 5min idle | ✅ | wake with mouse motion |
| Idle re-mapping | ✅ | qdwin's configure→ack handles re-renders cleanly |

## Scenario list

| # | Title | Tests |
|---|---|---|
| [01](./01-bar-visible.md) | bar + wallpaper render | smoke: layer-surface mapping at all |
| [02](./02-dismiss-privacy-modal.md) | dismiss first-run privacy modal | left-click on layer-surface modal works |
| [03](./03-clock-updates.md) | clock widget shows correct time | bar widget rendering, screenshot OCR |
| [04](./04-cursor-tracking.md) | cursor follows mouse moves | pointer events into Noctalia |
| [05](./05-bar-stays-after-idle.md) | bar still visible after DPMS wake | configure/ack cadence post-idle |

(Add 06+ as strips need new validations.)

## Resume points

If picking up this harness from scratch:

1. Check the VM is up: `virsh list --all | grep noctalia-vis`.
 If not, clone from `layershell-260502-2322` per
 `scripts/noctalia/setup-noctalia-graphical.sh`.
2. Source the helper: `source qdwin/tests/gui/qdwin-helpers.sh`
 then `qdwin_set_vm noctalia-vis-…`.
3. `noct_session_healthy` should return 0.
4. Pick a scenario, follow its setup → steps → asserts.
