# qdwin GUI test runner — for graphic-aware subagents

This is the qdwin-on-tty3 sibling of `phase1/gui-tests/AGENTS.md`. The
older guide assumes labwc + XWayland (Phase 1-5 demo); qdwin runs on
bare DRM via libweston with no XWayland in this profile, so the input
and observation primitives are different.

## Roles

- **Orchestrator** — picks scenarios from `qdwin/NN-*.md`, spawns one
  runner per scenario, aggregates PASS/FAIL.
- **Runner** — graphic-aware subagent given a single scenario file +
  a target VM. Executes setup → steps → asserts and returns a report.

## Environment

- Host: openSUSE Tumbleweed with `libvirt` + `virsh`.
- Target: a running libvirt domain on `qemu:///session` running the
  qdwin compositor (greetd-qdwin.service active on tty3 with autologin
  as `admin`). Standard test password: `Pa_ssw0rd45`.
- VM name: `$VMNAME` if set, else `virsh list --name --state-running | head -1`.
- The qdshell ctrl-socket at `/run/user/1000/qdshell.sock` is the
  legacy driver for the launcher / switcher / locker and is still
  needed by the `NN-*.md` scenarios it was authored against. The
  shipping qdshell is Quickshell IPC based: new `agent-*` smokes
  (`agent-click-smoke.sh`, `agent-mvp-session-smoke.sh`,
  `agent-protocol-audit.sh`, `agent-cursor-clickthrough-smoke.sh`) do
  NOT depend on the ctrl-socket. Prefer the new path for new tests;
  port `NN-*.md` scenarios off the ctrl-socket as Quickshell IPC
  hooks land.

## What works (and what doesn't)

| Surface | Driveable how | Notes |
|---|---|---|
| Single keys / non-modifier chords | `qdwin_send_key KEY_*` (virsh send-key) | evdev-layer; --holdtime applied; OK for plain typing |
| Real-keyboard chords (Alt+Tab, Ctrl+Alt+L, etc.) | `qdwin_chord <holds> -- <taps>` (QMP `input-send-event`) | **mandatory** for any chord that depends on a modifier-release transition (e.g. weston modifier_binding fires only when modifier was alone — see B3-switcher post-mortem in ) |
| Single key down/up explicitly | `qdwin_qmp_key <qcode> <down\|up>` | building block of qdwin_chord; reach for it when a scenario needs Alt held while doing other work |
| Input to focused **toplevels** | qdwin_send_key or qdwin_chord | foot/firefox/etc. once on screen and focused |
| Launcher overlay text input | **NOT keyboard** — only `qdwin_ctrl "launcher-type ..."` | known §6.8 gap; see `qdshell/Modules/Launcher (QML):17-23` |
| Launcher click-to-select | **NOT mouse** — only `qdwin_ctrl "launcher-activate"` | same gap |
| Locker password input | **NOT keyboard** (variant of launcher gap) — only `qdwin_ctrl "unlock-password <pw>"` | tracked B4 |
| Mouse pointer move | `qdwin_mouse_move <px_x> <px_y>` (QMP `input-send-event` abs axes; helper converts pixels to 0..32767) | toplevels under cursor receive enter events + can request cursor shape |
| Mouse clicks (left/right/middle) | `qdwin_click <x> <y> [button]` or split `qdwin_mouse_button left down\|up` | left-click on a chrome side or content surface raises + focuses the toplevel (click-to-focus, since task(129)) |
| Mouse drag (button held + motion) | `qdwin_drag <x1> <y1> <x2> <y2> [button]` | move + down + N intermediate moves + up. Drag of a titlebar (within the N chrome strip, outside the close/min/max buttons) translates the whole toplevel. `QDWIN_DRAG_STEPS`/`QDWIN_DRAG_STEP_MS` tune granularity. |
| Visual assertion | `qdwin_screenshot <file.png>` (root-authenticated qdshell capture of `Virtual-1`) | validates PNG and output dimensions; never falls back to the tty console |

### When a capture fails: seat-taken-away vs never-had-master

A capture needs a compositor that can still **repaint** — the in-compositor path
calls `weston_output_damage()` and the pixels are produced during that repaint —
so it requires DRM master. `qdwin_screenshot` therefore gates on
`qdwin_session_healthy` first, and a failure there prints one of two verdicts:

- `VERDICT: seat-taken-away` — the compositor HELD master and then lost it,
  almost always because the VT was switched away (seatd then disables the seat,
  weston drops master and closes every input device, and every later capture in
  the scenario fails too). This is an **ENVIRONMENT** condition: report ERROR,
  not a product FAIL. `qdwin_screenshot` tries once to switch back to the VT it
  stamped while the session was healthy; if that works it logs
  `WARN: recovered-vt-takeaway` and proceeds, and you should keep that WARN in
  the report as an environment note even though the step passed.
- `VERDICT: no seat-deactivation in the journal` — the compositor most likely
  never acquired master (a start-up fault). Treat as a precondition ERROR.

Do not read the older bare `compositor-not-on-vt` wording as "never had master";
it covers both cases, which is why the verdict line above exists.

## qcode reference

`qdwin_qmp_key` and `qdwin_chord` take qemu key codes (qcodes), NOT linux KEY_* names. See `qapi/ui.json` `QKeyCode` enum upstream. Common ones:

| qcode | Linux KEY_ |
|---|---|
| `alt`, `alt_r` | LEFTALT, RIGHTALT |
| `ctrl`, `ctrl_r` | LEFTCTRL, RIGHTCTRL |
| `shift`, `shift_r` | LEFTSHIFT, RIGHTSHIFT |
| `meta_l`, `meta_r` (super) | LEFTMETA, RIGHTMETA |
| `tab`, `esc`, `ret`, `spc`, `backspace` | TAB, ESC, ENTER, SPACE, BACKSPACE |
| `a..z`, `0..9`, `f1..f12` | A..Z, 1..0, F1..F12 |
| `left`, `right`, `up`, `down` | LEFT, RIGHT, UP, DOWN |

## Helper script

Source `qdwin-helpers.sh` from any scenario runner:

```bash
source phase1/gui-tests/qdwin/qdwin-helpers.sh
qdwin_set_vm demo-260430-0912
qdwin_session_healthy || { echo "session not up"; exit 2; }

# Plain typing — virsh send-key is fine
qdwin_send_key KEY_LEFTCTRL KEY_SPACE   # or use ctrl-socket directly:
qdwin_ctrl "launcher-type foot"
qdwin_ctrl "launcher-activate"
sleep 1.5
qdwin_screenshot /tmp/foot-running.png

# Alt+Tab / Ctrl+Alt+L — MUST use qdwin_chord (real-keyboard sequence)
qdwin_chord alt -- tab          # one Alt+Tab cycle
qdwin_chord ctrl alt -- l       # lock screen
qdwin_chord alt -- tab tab      # cycle twice while Alt is held
```

### Why two key paths

`virsh send-key`, `qdwin_send_key`, and `qemu-monitor-command 'sendkey'` all press the listed keys simultaneously, hold for `--holdtime`, then release simultaneously. There is no "Alt held alone, Tab released" intermediate state in the resulting evdev stream. weston's `weston_compositor_add_modifier_binding` (used for the original switcher_commit / lock-on-modifier-release patterns) fires *only* when the modifier was pressed *alone* for the entire hold. Press Tab during the Alt-hold and the binding never fires. Verified 2026-04-30 by capturing /dev/input/event0 with evtest while pressing Alt+Tab on a real keyboard: all four events arrived in correct order, but the binding still did not fire.

`qdwin_chord` instead emits each event independently via QMP `input-send-event` (down for each hold key, then down/up for each tap key, then up for each hold key in reverse). Order matches what a real keyboard produces, and weston's grab `modifiers` callback (the new path used by the switcher in qdwin.c) sees the Alt-released transition cleanly.

For non-chord key tests (typing into a focused toplevel, sending Enter, etc.) `qdwin_send_key` is faster and fine.

The helper pushes ctrl-socket payloads through the same host:8765
HTTP server `fresh-vm-bootstrap.sh` uses (so `vm-exec`'s JSON quoting
never sees embedded quotes). Make sure the HTTP server is running
under `compositor/`:

```
cd ${QDWIN_REPO} && \
  python3 -m http.server 8765 --bind 127.0.0.1 &
```

## Reading screenshots

A vision-capable LLM agent (e.g. Claude with `Read` on a `.png`)
extracts text and shape directly from the framebuffer. For non-vision
agents:

```bash
zypper -n install tesseract-ocr tesseract-ocr-traineddata-english
tesseract /tmp/foo.png - -c tessedit_create_tsv=1
```

yields tab-separated `(text, left, top, width, height)` rows. Match
on text labels, not pixel offsets — qdshell font/theme drift breaks
hard-coded coordinates across clones.

## Hard-learned pitfalls (qdwin-specific)

1. **Launcher cache is built lazily on first toggle and never
   rescanned.** A package installed after the session started won't
   appear in the launcher until qdshell is restarted (or until a
   `launcher-rescan` ctrl command is added — currently a TODO). Add
   any test-only packages to `fresh-vm-bootstrap.sh` so they're
   present before greetd-qdwin starts.

2. **CSD-only toplevels (foot, GTK4 apps without
   server-decoration-v1) double-decorate.** qdshell attaches its own
   chrome + the client also draws a titlebar inside its content area.
   foot specifically logs `"no decoration manager available - using
   CSDs unconditionally"` — the toplevel renders at its CSD-default
   tiny size with both decorations stacked. xdg-decoration-v1 was
   declined per spec/27 (chrome is private-protocol shell-attached);
   tracked as a real gap, not a test-author problem.

3. **Foot launched via the launcher's `setsid Exec=foot --server`
   sometimes exits before chrome attaches; foot launched directly via
   `runuser -u admin -- bash -c 'WAYLAND_DISPLAY=wayland-1 foot'` stays
   alive.** When a scenario needs a known-good toplevel for input
   tests, prefer the direct path; reserve the launcher path for
   testing the launcher itself.

4. **Don't `systemctl restart greetd-qdwin.service` mid-scenario
   without re-waiting for the ctrl-socket.** The socket disappears
   for 3–5s during restart; a too-quick `qdwin_ctrl` returns
   "Connection refused".

5. **virsh send-key doesn't echo errors** for unknown keysyms — it
   just returns 0 and nothing happens. Stick to `KEY_*` constants
   from `/usr/include/linux/input-event-codes.h`.

6. **Cursor visibility is not a screenshot contract.**
   The shell-authorized framebuffer source may force hardware planes off for
   the capture repaint, while the normal scanout may use a DRM cursor plane.
   Do not assert cursor presence or absence from `qdwin_screenshot` across
   renderer profiles. Assert by-effect for behavioural tests ("after click at
   (X, Y), expected toplevel at the front") and use DRM plane state for tests
   whose subject is cursor ownership. `qdwin_screenshot_virsh_diag` is only a
   tty/VM diagnostic and never content evidence.

7. **The pre-fixed VMs autologin as the `greeter` pseudo-user (uid
   471), not admin.** If `qdwin_session_healthy` returns false but the
   service is active, check `loginctl list-sessions` — if you see uid
   471 on tty3 you're on a pre-task(125) image. Re-run
   `bootstrap-qdwin-in-vm.sh` against an updated source tree.

## Available scenarios

| File | What it covers |
|---|---|
| `01-open-terminal.md` | end-to-end: launcher → filter → activate → type into focused terminal (B2 + auto-focus + ctrl-socket launcher path) |
| `02-launcher-keyboard.md` | real-keyboard input into launcher overlay — type, arrows, Esc, Enter (B3) |
| `03-locker-cycle.md` | Ctrl+Alt+L → type password → Enter unlock → keyboard reaches focused toplevel again (B1 + B4 + post-unlock regression) |
| `04-alt-tab-switch.md` | Alt+Tab single-press swaps focus + raises + survives close-and-refocus (switcher_grab + anchor + request_raise re-stack + on_toplevel_removed refocus) |
| `05-launcher-rescan.md` | new app installed mid-session appears in launcher on next toggle (B5) |
| `06-mouse-click-focus.md` | clicking a background window's chrome focuses + raises it (mouse path) |
| `07-titlebar-buttons.md` | clicking maximize / restore / minimize glyphs on the titlebar actually changes window state (mouse path) + ctrl-socket control comparison; also verifies maximised window leaves room for chrome + panel (not fullscreen) |
| `08-titlebar-close-button.md` | clicking the red × on the titlebar destroys the toplevel; chrome_button right-click+left-click cycle for the close action |
| `09-titlebar-context-menu.md` | right-click on titlebar opens qdshell context-menu popup; clicking "Restore"/"Maximise" / "Minimise" / "Close" items dispatches the right action via qdwin_shell_v1@v21 popup_button |
| `10-context-menu-relabel.md` | the maximise/restore row label flips with `tl.state & 1` on every menu open ("Maximise" ↔ "Restore"), proving the label is recomputed not cached |
| `12-bar-no-overdraw.md` | bar `content` and `exclusion-top` agree on height; maximized windows do not have their top row clipped by the bar's bottom row (pixel-mismatch fix + `exclusionZoneBleed` toggle round-trip) |
| `13-focus-events-emitted.md` | every keyboard-focus transition between toplevels emits a `qdwin: focus handle=N (was M) seat=…` line, including the spawn / spawn / close-handoff / last-close-to-no-window sequence |
| `14-bar-content-quiet-when-idle.md` | journal grows by ≤2 bar-content remap lines over 10 s idle (no remap storm) and re-settles to quiet after a window cycle |
| `15-keybinding-events.md` | Ctrl+Space / Alt+Tab / Ctrl+Alt+L / registered hotkeys all emit `qdwin: <event>` log lines independent of shell binding state (silent-drop guard) |
| `16-qdshell-binding-protocol-events.md` | qdshell binds qdwin_shell_v1 at v14 via the Qdistro.Qdwin QML plugin; `hello`, `toplevel_added/removed`, `seat_focus_changed`, and switcher_next/commit all round-trip; alt+tab cycles focus via the protocol |
| `17-qdshell-drives-close.md` | qdshell's `Qdwin.closeWindow` Q_INVOKABLE invokes `qdwin_shell_v1.request_close(handle)`; target toplevel exits cleanly and the removal propagates back through the protocol to the QML side |
| `18-workspace-switch.md` | v24 workspaces: switching hides/shows windows across workspaces and the settings count reconciles to the compositor (ext-workspace-v1 + qdshell `workspace` IPC) |
| `19-wm-policy.md` | v25 window-manager-policy qdshell-driven gate: IPC `capabilities` reports `bound=true wmPolicy=true keybindRegistration=true` (WindowManager tab live-apply) + one visual proof a tile resizes the live client. Direct-compositor proof split out to `21-wm-policy-bystander.md` |
| `20-idle-dpms.md` | v26 idle/DPMS: Path A reads the idle/DPMS capability deterministically via qdshell IPC (`idleDpms=true`, journal `idleDpms -> true` fallback); Path B drives `set_display_power` off/on directly via `qdwin-bystander`. Modern `qdshell.service`/`qdwin-compositor.service` contract (legacy `noctalia-*` units retired) |
| `21-wm-policy-bystander.md` | v25 window-manager-policy direct-compositor functional proof (split from 19): `qdwin-bystander` drives `set_wm_policy`/`request_tile` left/right/restore/`request_fullscreen`/`register_hotkey` over its FIFO; all deterministic journal asserts (tiled client resizes, not just chrome) |
| `agent-mvp-session-smoke.sh` | executable agent/CI smoke for plan2 MVP session invariants: qdwin/qdshell active, qdshell bound to qdwin_shell_v1, legacy LXQt/labwc absent, cursor sprites active, Ctrl+Space logged, and ordinary toplevels released from qdwin's holding layer |
| `agent-click-smoke.sh` | executable agent/CI smoke: launches two known test windows, injects QMP mouse clicks, asserts focus moves to the clicked toplevel, and reports the qdshell launcher-icon click as a named gap unless `QDWIN_REQUIRE_LAUNCHER_CLICK=1` is set |
| `agent-protocol-audit.sh` | executable agent/CI audit: records qdwin Wayland globals, scans qdshell's Quickshell/Wayland usage, and checks that opening the qdshell launcher does not hit the xdg-popup null-parent protocol error |
| `agent-cursor-clickthrough-smoke.sh` | plan3 H3: spawns a known xdg-toplevel, parks the pointer over it (so a cursor sprite maps on cursor_layer), and verifies the click still lands on the toplevel. Discriminator: `qdwin: focus handle=N` matches the test window. `QDWIN_CURSOR_CLICKTHROUGH_FORCE_BREAK=1` flips the expectation for validating the regression itself. |
| `agent-cursor-paint-smoke.sh` | **agent-assisted** smoke for the two regressions that make a qdwin desktop unusable: (A) the qdshell crash-loop (version-skewed Qdwin.qml `onOutputsChanged` ↔ qdwin QML plugin — Quickshell exits 255, black scanout) and (B) the missing/doubled VM cursor (virtio-gpu hw cursor plane). Deterministic asserts: noctalia-shell active + bounded NRestarts + no QML "non-existent property" error since the live `qs` PID started + bind ≥ v14; qdshell bar-content layer surface mapped + non-black scanout; DRM `plane-1` crtc=crtc-0 / real fb / "allocated by weston"; cursor sprite mapped on cursor_layer. Then a **vision LLM** reads a post-pointer-move screenshot and confirms `{painted, top_bar, cursor_count}` — expecting 0 software cursors in the scanout (the cursor is on the off-scanout hw plane; >0 ⇒ double-cursor regression). Knobs: `QDWIN_CURSOR_IN_SCANOUT=1` for the pixman/software-cursor profile, `QDWIN_SKIP_AGENT=1` for deterministic-only, `QDWIN_AGENT_CMD` to swap the vision agent, `QDWIN_MAX_RESTARTS`. Self-tested both negatives: a stopped qdshell and an inverted cursor expectation both FAIL. |
| `agent-shell-capture-smoke.sh` | executable agent/CI smoke for the in-compositor shell-authorized capture path (the only sanctioned visual-evidence source): L1 DRM/session gate, `QDWIN_ENABLE_SHELL_CAPTURE=1` present with `QDWIN_SECCTX_OPEN` absent, ordinary + real SECCTX clients cannot discover `weston_capture_v1`, same-uid ctrl peer denied (root-only `SO_PEERCRED`), non-designated output names refused, stable double capture, mutation-after-idle visibility, capture-while-locked, and qdshell/compositor death → L1 failure with no tty fallback. Registered in the qci `gui` gate. |
| `agent-idle-dpms-recovery-smoke.sh` | executable VM smoke for qdwin internal-idle + qdshell display-power recovery. Requires an ABI-matched qdwin build deployed in the guest. Saves qdshell settings, sets display-off to 1 minute and a safe inactivity action to 2 minutes, keeps pointer motion active past the display-off timeout to assert it does not blank mid-use, waits for a black/inactive scanout, injects real QMP key+mouse input, and asserts a recovered non-black screenshot plus `set_display_power on=0` then `on=1` logs. Also fails if the longer inactivity notification fires at the shorter display-off timeout. Restores original qdshell settings on exit. |

Run each scenario sequentially against the same VM; each cleans up after itself. For a full smoke pass, an orchestrator can spawn one runner per scenario in series.

## Running a scenario

1. Read `qdwin/NN-*.md` top to bottom before acting.
2. Source `qdwin-helpers.sh`.
3. Follow the **Setup** block. Stop and FAIL if any step exits
   nonzero or `qdwin_session_healthy` returns false.
4. Execute **Steps** in order. After each step that mutates state,
   capture a screenshot to `/tmp/<scenario-stem>-<step>.png` AND OPEN
   it (`view_image` or equivalent) before moving to the next step. OCR
   is not a substitute — it cannot see a colour, a layout, or that
   something is ABSENT. Don't trust the prior step's "ok" — qdshell
   logs success on dispatch, not on completion.
5. For each **Assert**, decide PASS/FAIL by looking at the most
   recent screenshot AND/OR the qdshell `list` snapshot. Report
   concretely: quote the OCR'd text or the snapshot fields.
6. Cleanup: kill spawned processes, ensure the launcher is hidden,
   reset the locker if engaged.

## Every scenario MUST declare `qci:visual`

Put exactly one of these HTML comments near the top of every scenario file:

```
<!-- qci:visual: required -->   a REQUIRED assertion is decided by reading a captured frame
<!-- qci:visual: none -->       no required assertion is decided by pixels
```

`none` is correct even when the scenario captures screenshots, as long as every
required assertion is settled by a non-visual oracle (journal line, D-Bus reply,
sqlite row, exit code, IPC response). Screenshots kept purely as run artifacts
do not make a scenario `required`. A rejected, near-black, or missing
screenshot on `none` is not ERROR and not FAIL. Do not take screenshots as a
substitute for those oracles, and do not record ERROR because screenshot-fresh
refused a diagnostic frame.

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
