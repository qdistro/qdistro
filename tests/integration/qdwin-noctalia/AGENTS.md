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
 installed and `qdshell.service` (user unit) enabled.
- Default VM name pattern: `noctalia-vis-YYMMDD-HHMM`. The reference
 setup VM as of 2026-05-03 is `noctalia-vis-260503-1021` (clone of
 `weston-desktop-260422-1604`-derived `layershell-260502-2322`).
- Resolution: 1280×800 at 60 Hz, pinned by qci's GUI golden build.
- Backend: `drm-backend.so` with `renderer=pixman` (no GL — virtio-gpu
 in our VMs has no accel3d; Mesa GLES backend segfaults).
- Auth: admin / `Pa_ssw0rd45`; admin user must be in `video,input,render,seat`
 groups; `seatd.service` must be running on the VM.

## Helper script

Reuse `${QDWIN_REPO}/tests/gui/qdwin-helpers.sh` (source it via the
exported `${QDWIN_REPO}`/`${QDISTRO_REPO}` paths, NOT a cwd-relative
path — the qci runner exports a `<NAME>_REPO` for every project so the
anchored form resolves wherever the agent runs). The qdshell-specific
helpers (`qdwin_ctrl`, `qdwin_session_healthy`) don't apply here —
Noctalia has no equivalent ctrl-socket. Instead use:

- `qdwin_screenshot <out.png>` — virsh screenshot wrapper, generic
- `qdwin_send_key`, `qdwin_qmp_key`, `qdwin_chord` — keyboard
- `qdwin_mouse_move`, `qdwin_click`, `qdwin_mouse_button` — pointer
- `noct_session_healthy` — defined in `noctalia-helpers.sh` (this
 dir) — checks the `qdshell.service` user unit is active +
 qs process is alive

## What works (and what doesn't) on Noctalia

| Surface | Works? | Notes |
|---|---|---|
| Bar visible at top | ✅ | layer 2, 1280×31 |
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
2. Source the helpers via their exported-repo paths:
 `source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh` and
 `source ${QDISTRO_REPO}/tests/integration/qdwin-noctalia/noctalia-helpers.sh`,
 then `qdwin_set_vm noctalia-vis-…`.
3. `noct_session_healthy` should return 0.
4. Pick a scenario, follow its setup → steps → asserts.

## Every scenario MUST declare `qci:visual`

Put exactly one of these HTML comments near the top of every scenario file:

```
<!-- qci:visual: required -->   a REQUIRED assertion is decided by reading a captured frame
<!-- qci:visual: none -->       no required assertion is decided by pixels
```

`none` is correct even when the scenario captures screenshots, as long as every
required assertion is settled by a non-visual oracle (journal line, D-Bus reply,
sqlite row, exit code, IPC response). Screenshots kept purely as run artifacts
do not make a scenario `required`.

`qci gui` REFUSES to run a scenario with no declaration, an unknown value, or
two conflicting declarations (`gui_validate_scenarios`) - before any golden
bake, VM, or agent. There is no content-sniffing fallback.

**OPEN EVERY FRAME YOU ASSERT ON.** Use your image-viewing tool (`view_image`
or equivalent). OCR is NOT a substitute: it reads text and nothing else, so it
cannot establish a colour, a geometry/layout claim, focus, z-order, or the
ABSENCE of a control - and "the pane is empty" / "no dialog appeared" / "the
badge is gone" are the commonest assertions here. Text OCR does not find is
indistinguishable from text it could not read, so OCR on an unrendered frame
produces a confident wrong verdict in either direction. Run OCR only to pull
long text out of a frame you have ALSO opened. If you cannot open images at
all, record ERROR naming the missing capability - never PASS, never FAIL, and
never fall back to OCR and grade anyway.

For a `required` scenario the gate also reads the frames itself, host-side,
after the agent exits: it checks every attested frame is DECODABLE and, when a
tesseract backend is present, records what text it finds. Both are recorded in
`visual-evidence/manifest.tsv`; the OCR column is corroboration and changes no
verdict. So the one thing a `required` scenario must do is SAVE THE FRAMES IT
ASSERTS ON into `$QCI_GUI_ARTIFACT_DIR`. A `required` PASS/FAIL whose artifact
directory holds NO attested frame, or whose frames are ALL undecodable, is
recorded ERROR - that is the harness failing to capture, not a verdict. Nothing
the agent writes (its own OCR output, its transcript) is accepted as evidence,
and artifact timestamps are never compared. Whether you opened a frame is
RECORDED per attempt as a diagnostic; it does not change your verdict, but it
is the first thing anyone reads when a visual verdict is disputed.
