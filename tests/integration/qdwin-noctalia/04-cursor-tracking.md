# 04 — cursor follows mouse moves

**Acceptance criterion:** moving the mouse via QMP `input-send-event`
results in the cursor visibly following on screen. Hovering over a
bar widget triggers the appropriate hover state (color shift or
icon highlight).

This exercises:
- Pointer event delivery (already proven in scenario 02 by the
 click; this scenario isolates motion without click)
- Cursor sprite installation on a layer surface (Noctalia uses
 cursor-shape-v1)
- wp_cursor_shape_manager_v1 binding by Noctalia

## Setup

```bash
source qdwin/tests/gui/qdwin-helpers.sh
source tests/integration/qdwin-noctalia/noctalia-helpers.sh
qdwin_set_vm "${VMNAME:-noctalia-vis-260503-1021}"
noct_session_healthy || { echo "FAIL: noctalia not healthy"; exit 1; }
```

## Steps

### Step 1 — park cursor in the dark wallpaper area

```bash
qdwin_mouse_move 1500 600
sleep 1
qdwin_screenshot /tmp/04-step1-wallpaper-area.png
```

**Assert (1.1):** the screenshot has a visible cursor near
(1500, 600). Cursor sprite check: there should be a small
arrow-shaped artifact around the pixel coordinate (cursor lives on
a KMS plane and IS visible in virsh screenshots — different from
qdshell's headless setup).

### Step 2 — move cursor onto a bar widget (clock area)

```bash
qdwin_mouse_move 1700 15
sleep 1
qdwin_screenshot /tmp/04-step2-clock-hover.png
```

**Assert (2.1):** the cursor is now in the top-right corner near
(1700, 15).
**Assert (2.2):** comparing the bar's clock-widget area between
step 1 and step 2 screenshots, the widget shows a hover state
(brighter background or color highlight) — Noctalia hovers with
a subtle tint by default.

If the assert is hard to make robust (subtle hover effect not
crossing pixel-diff thresholds), demote to a soft check: just
confirm the cursor moved to the new position.

### Step 3 — sweep cursor across the bar

```bash
for x in 100 400 700 1000 1300 1600 1900; do
 qdwin_mouse_move "$x" 15
 sleep 0.2
done
sleep 1
qdwin_screenshot /tmp/04-step3-sweep-end.png
```

**Assert (3.1):** the cursor is at (1900, 15) (right edge of bar).
**Assert (3.2):** Noctalia is still alive — `noct_session_healthy`.
**Assert (3.3):** weston journal in the last 30s shows zero
protocol errors.

## Cleanup

None. Cursor parked at (1900, 15) is fine.

## Pass criteria

All asserts pass. Soft asserts (2.2) may be downgraded to "info
only" if hover-styling diff is too subtle for reliable detection.

## Known failure modes

1. **Cursor invisible in screenshot** — qdwin's cursor-shape
 sprite installation has known quirks (per memory
 `qdwin_cursor_fix_260430` — the 2026-04-30 cursor-buffer-lifetime
 bug). If cursor doesn't show at all, the install path may have
 regressed. Triage: check `qdwin: cursor-shape theme=...` in
 weston log for `loaded=N/36` — N>0 means loaded, N=0 means
 fallback synthetic sprite is in use.

2. **Hover state requires keyboard focus** — some Noctalia versions
 only highlight a bar widget when keyboard focus is also on the
 bar. We don't currently route keyboard focus to layer surfaces
 ( deferred). Soft-pass if hover doesn't visibly fire.
