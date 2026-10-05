# 02 — apps at a real 200% compositor scale: points once, live follow

<!-- qci:visual: required -->
<!-- qci:visual-captures: 6 -->

**Acceptance criterion (pack 07 scenario 2, non-100% scale; 09 item 3):**
with the qdwin output set to **200%** through qdshell's own Settings >
Display (the only client qdwin lets change output scale), a Qt app's
`devicePixelRatio` is 2 and its UI font stays in POINTS (`11 × fonts.uiScale
× metrics.uiScale`), not multiplied by the device scale. Running apps at
200% still follow a dark→light switch without relaunch, and their chrome is
legible (text at a normal visual size relative to the shell bar, not
doubled and not tiny). The scale is restored to 100% at the end.

Machine oracles: `pres_output_scale`, the `presentation-scale.py` PASS lines,
`pres_snapshot`, `pres_app_pids`. Visual oracle: the frames.

## Setup

```bash
source "${QDISTRO_REPO}/tests/integration/presentation-gui/presentation-helpers.sh"
qdwin_set_vm "${VMNAME:?qci sets VMNAME to this scenario's VM}"
qdwin_session_healthy || { echo "FAIL: qdwin session not healthy"; exit 2; }
pres_kill_apps
pres_qs_ipc darkMode setDark
pres_wait_mode dark 30 || { echo "FAIL: shell did not publish dark"; exit 1; }
echo "scale-before=$(pres_output_scale)"
```

**Assert (0.1):** `scale-before=1`.

## Steps

### Step 1 — set Virtual-1 to 200% in Settings > Display

```bash
pres_qs_ipc settings openTab display/0
sleep 2
qdwin_screenshot /tmp/pres02-step1-display-tab.png
```

OPEN the frame. In the Layout sub-tab, find the **Virtual-1** card and its
**Scale** combo (label `S...`, value `100%`). Then, using pixel coordinates
read from the frame you just opened:

1. `qdwin_click X Y` on that Scale combo; `qdwin_screenshot` and OPEN the new
   frame; `qdwin_click` the `200%` entry.
2. Move the pointer over the settings panel and scroll it to the bottom:
   `qdwin_mouse_move X Y` then, eight times,
   `qdwin_mouse_button wheel-down down; qdwin_mouse_button wheel-down up`.
   `qdwin_screenshot` and OPEN it: the **Apply** button is at the bottom
   right of the panel.
3. `qdwin_click` **Apply**, then IMMEDIATELY (no capture or look first)
   run the block below. A "Keep these display settings?" dialog appears and
   reverts on its own after 15 s, which is too short for a look-then-click
   round trip, so `pres_click_keep_changes` finds and clicks its **Keep
   changes** button from one attested frame:

```bash
sleep 1.5
pres_click_keep_changes
```

   It prints `keep-click frame=... at frame-px=X,Y`. If it returns nonzero
   instead, see Known-broken-if 1.1.
4. `sleep 17` (past the revert window), then:

```bash
echo "scale-after=$(pres_output_scale)"
qdwin_send_key KEY_ESC; sleep 1
qdwin_screenshot /tmp/pres02-step1-at-200.png
```

**Assert (1.1):** `scale-after=2` (the change was KEPT, not reverted).
**Assert (1.2):** OPEN `pres02-step1-at-200.png`: the shell bar is drawn at
twice its 100% size (taller, larger text) — the output really is at 200%.

### Step 2 — a Qt client sees dpr 2 and keeps the UI font in points

```bash
# Root-staged probe (fresh-vm-bootstrap 4b) or the source tree; admin
# cannot read /root, so copy it out first.
qdwin_vmx_merged "install -m 0644 /root/presentation-scale.py /tmp/presentation-scale.py 2>/dev/null || install -m 0644 /root/qdistro-src/tests/integration/vm/probes/presentation-scale.py /tmp/presentation-scale.py"
pres_admin "python3 /tmp/presentation-scale.py --expect-dpr 2" | grep -E '^(PASS|FAIL):'
```

The probe publishes its own snapshot (user scale 1.1 × 1.1) and checks the
installed SDK. Republish the shell's snapshot afterwards:

```bash
pres_qs_ipc darkMode setLight; pres_wait_mode light 30 >/dev/null
pres_qs_ipc darkMode setDark;  SNAP_DARK=$(pres_wait_mode dark 30); echo "SNAP_DARK=$SNAP_DARK"
```

**Assert (2.1):** the probe printed `PASS: devicePixelRatio matches
compositor scale 2.0`, `PASS: user scale applied once in points`, and
`PASS: UI font was not multiplied by devicePixelRatio`, and no `FAIL:` line.

### Step 3 — apps at 200% follow dark → light

No raise is needed: qfileman is the only window when it is captured dark and
light; qnotebook is started afterwards (newest window on top).

```bash
pres_launch qfm qfileman
sleep 8
PID_QFM=$(pres_app_pids); echo "PIDS_3=$PID_QFM"
qdwin_screenshot /tmp/pres02-step3-qfileman-dark.png
pres_qs_ipc darkMode setLight
SNAP_LIGHT=$(pres_wait_mode light 30); echo "SNAP_LIGHT=$SNAP_LIGHT"
sleep 3
PIDS_3B=$(pres_app_pids); echo "PIDS_3B=$PIDS_3B"
qdwin_screenshot /tmp/pres02-step3-qfileman-light.png
pres_launch qnb qnotebook
sleep 8
qdwin_screenshot /tmp/pres02-step3-qnotebook-light.png
```

**Assert (3.1):** `SNAP_LIGHT` mode is `light`; the `qfileman=` pid in
`PIDS_3B` equals the one in `PIDS_3`.
**Assert (3.2):** OPEN the dark qfileman frame, then the light one: the same
qfileman window (the only app window) went from DARK to LIGHT chrome. Its
menu/toolbar text is legible and roughly the same visual size as the shell
bar's text (not about twice it, not about half it).
**Assert (3.3):** OPEN the qnotebook frame: the new qnotebook window is in
front with LIGHT chrome and legible text.

### Step 4 — restore 100%

Repeat Step 1 choosing `100%` (Apply, then the `pres_click_keep_changes`
block at once), then:

```bash
sleep 17
echo "scale-restored=$(pres_output_scale)"
```

**Assert (4.1):** `scale-restored=1`.

## Cleanup

```bash
pres_kill_apps
pres_qs_ipc darkMode setDark >/dev/null
echo "scale-cleanup=$(pres_output_scale)"
```

If `scale-cleanup` is not `1` (Step 4 did not run or did not stick), restore
it now exactly as in Step 4 (Settings > Display, `100%`, Apply,
`pres_click_keep_changes`), wait 17 s, and print `pres_output_scale` again. If it is still not
`1`, record the scenario as ERROR with "cleanup could not restore 100%"
regardless of the step verdicts: a VM left at 200% must not look clean.

## Pass criteria

All asserts 0.1 → 4.1 pass.

## Known-broken-if

- 1.1 reads 1 after `pres_click_keep_changes`, or it returned nonzero:
  OPEN the frame it names. If that frame shows no confirm dialog, the click
  ran before the dialog mapped or Apply missed; if it shows the dialog, the
  helper did not find the button. Retry Step 1 once (re-select `200%`,
  Apply, the helper block). A second miss with the dialog visible is ERROR
  (driver), not FAIL. If Apply opens NO dialog at all (Apply visibly
  pressed, the scale changes, then reverts with no prompt), that is a
  product FAIL (fixed by `claude/qdshell-apps-shell-follow` `d208302d3`:
  stale serial, missing ToastService import, dead timer).
- 2.1 dpr 1.0 at scale 2: Qt is not receiving the wl_output scale.
- 2.1 font ≈ 2× expected: a consumer multiplies by devicePixelRatio
  (`apply_logical_ui_font` must not).
- 3.2 text visually doubled: same as above, in an app adapter.
