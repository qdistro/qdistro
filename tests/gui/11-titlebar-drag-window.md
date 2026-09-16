# 11 — drag window by its titlebar

<!-- qci:visual: required -->

**Acceptance criterion:** pressing the left mouse button on the
titlebar background (NOT on close/min/max), holding it down while the
pointer moves, and releasing translates the toplevel — content view
plus all four chrome sides — by the same delta. Releasing the button
ends the drag.

This validates the new `begin_interactive_move` path:

- qdshell's `dispatch_chrome_click` calls `sh.begin_interactive_move`
  on a left-press inside the N strip but outside the button regions.
- qdwin's `qdwin_handle_begin_interactive_move` starts a custom
  `weston_pointer_grab` whose `motion` translates the view +
  reposition chrome on every motion event.
- A button-release ends the grab and emits a fresh
  `toplevel_geometry` event so the shell sees the new outer x/y.

Maximised toplevels refuse silently per the XML contract; this
scenario covers both the floating-success path and the maximised-noop
guard.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"

# Detect screen size from the panel ctrl reply (panel spans full width
# along its edge; the geom field is `x,y,w,h`). Falls back to 1024×768
# only if both detection paths fail.
if [ -z "${QDWIN_SCREEN_W:-}" ] || [ -z "${QDWIN_SCREEN_H:-}" ]; then
    PANEL=$(qdwin_ctrl "panel" 2>/dev/null)
    PW=$(echo "$PANEL" | grep -oE 'geom=[0-9]+,[0-9]+,[0-9]+,[0-9]+' | head -1 | awk -F'[=,]' '{print $4}')
    PY=$(echo "$PANEL" | grep -oE 'geom=[0-9]+,[0-9]+,[0-9]+,[0-9]+' | head -1 | awk -F'[=,]' '{print $3}')
    PH=$(echo "$PANEL" | grep -oE 'geom=[0-9]+,[0-9]+,[0-9]+,[0-9]+' | head -1 | awk -F'[=,]' '{print $5}')
    : "${QDWIN_SCREEN_W:=${PW:-1024}}"
    : "${QDWIN_SCREEN_H:=$(( ${PY:-768} + ${PH:-0} ))}"
    [ "$QDWIN_SCREEN_H" = "0" ] && QDWIN_SCREEN_H=768
fi
export QDWIN_SCREEN_W QDWIN_SCREEN_H

pgrep -f "http.server 8765" >/dev/null || (
    cd ${QDWIN_REPO} && \
    python3 -m http.server 8765 --bind 127.0.0.1 >/tmp/qdistro-http.log 2>&1 &
)
sleep 1
qdwin_session_healthy || { echo "FAIL: session not up"; exit 1; }

"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; sleep 1' >/dev/null

# Single foot toplevel — handle will be 1 if no other windows exist.
qdwin_ctrl "launcher-toggle" >/dev/null
qdwin_ctrl "launcher-type foot" >/dev/null
qdwin_ctrl "launcher-activate" >/dev/null
sleep 2

HANDLE=$(qdwin_ctrl list | awk '/^tl /{print $2; exit}')
[ -n "$HANDLE" ] || { echo "FAIL: no toplevel"; exit 1; }
```

## Coordinate model

qdshell paints the N chrome 28 px tall, with 8 px W/E/S borders. The
three buttons (close/maximize/minimize) live on the right side of
the titlebar; their centres are within the rightmost ~80 px of the N
strip. The **safe drag-grab zone** is the left half of the titlebar:

- y = 14 (titlebar centre, restored window — pre-drag pos)
- x ∈ [outer.x + 30, outer.x + outer.w/2] avoids the icon + buttons

The cascade-default position of the first foot is roughly
(outer.x ≈ 250, outer.y ≈ 130) on a fresh session, but we don't have
to guess: the `geom` ctrl reports the actual rect.

## Steps

### Step 1 — record baseline geometry + screenshot

```bash
BASE=$(qdwin_ctrl "geom $HANDLE")
echo "baseline: $BASE"
qdwin_screenshot /tmp/11-step1-baseline.png
```

**Assert (1.1):** `geom` reply is of the form
`ok geom <H> x=<X> y=<Y> w=<W> h=<H>` with X, Y both > 0 and
(X + W) < QDWIN_SCREEN_W, (Y + H) < QDWIN_SCREEN_H.
**Assert (1.2):** screenshot shows a foot terminal at that position
with cyan chrome around it.

Extract the baseline outer-rect coords:

```bash
read BX BY BW BH < <(echo "$BASE" | awk '
    {
      for (i=1;i<=NF;i++) {
        if ($i ~ /^x=/) x=substr($i,3);
        if ($i ~ /^y=/) y=substr($i,3);
        if ($i ~ /^w=/) w=substr($i,3);
        if ($i ~ /^h=/) h=substr($i,3);
      }
      # geom returns outer-rect coords directly: qdwin_shell_v1
      # `toplevel_geometry` sends content_pos minus inset, so x/y
      # are already the chrome top-left and w/h cover the full
      # outer rect including the 28-px titlebar + 8-px borders.
      # Earlier revisions of this scenario subtracted 8/28 here,
      # which placed GRAB_Y above the titlebar and made every
      # drag miss the chrome hit-test.
      print x, y, w, h
    }')
echo "outer rect: ($BX,$BY) ${BW}x${BH}"
```

### Step 2 — drag titlebar 200 px right and 80 px down

```bash
# Pick a point inside the N strip, in the leftmost ~30% of the
# titlebar (clear of icon + buttons).
GRAB_X=$((BX + 60))
GRAB_Y=$((BY + 14))
DROP_X=$((GRAB_X + 200))
DROP_Y=$((GRAB_Y + 80))

qdwin_drag "$GRAB_X" "$GRAB_Y" "$DROP_X" "$DROP_Y"
sleep 0.3
qdwin_screenshot /tmp/11-step2-after-drag.png
AFTER=$(qdwin_ctrl "geom $HANDLE")
echo "after: $AFTER"
```

**Assert (2.1):** weston log on the VM contains both
`begin_interactive_move handle=$HANDLE` and
`end_interactive_move handle=$HANDLE` lines from this run.
Verify with:

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
    'tail -200 /var/log/qdwin.log 2>/dev/null || \
     journalctl --no-pager -u greetd-qdwin -n 200' \
    | grep -E "begin_interactive_move|end_interactive_move" | tail -4
```

**Assert (2.2):** new `geom` reply has x ≈ baseline.x + 200 and
y ≈ baseline.y + 80 (allow ±5 px for chrome inset rounding). w/h
unchanged.
**Assert (2.3):** screenshot shows the foot window AND its cyan
chrome moved together by ~200×80 px from baseline. No chrome
fragments left at the old position (qdwin should have damaged the
full output via `weston_compositor_schedule_repaint`).

### Step 3 — drag back to original position

```bash
# Re-read geom because the window has moved by (+200, +80).
# Pickup point must be on the *current* titlebar, not the old one;
# computing offset from the post-step-2 outer-rect keeps the grab
# anchor consistent with step 2 so dropping at (GRAB_X, GRAB_Y)
# returns the window to baseline.
AFTER_GEOM=$(qdwin_ctrl "geom $HANDLE")
read NBX NBY _ _ < <(echo "$AFTER_GEOM" | awk '
    { for (i=1;i<=NF;i++) {
        if ($i ~ /^x=/) x=substr($i,3);
        if ($i ~ /^y=/) y=substr($i,3);
      }
      print x, y, "_", "_"
    }')
qdwin_drag $((NBX + 60)) $((NBY + 14)) \
           "$GRAB_X"     "$GRAB_Y"
sleep 0.3
RETURN=$(qdwin_ctrl "geom $HANDLE")
echo "return: $RETURN"
qdwin_screenshot /tmp/11-step3-back.png
```

**Assert (3.1):** `geom` x/y are within ±5 px of the **baseline**
geom from Step 1.
**Assert (3.2):** screenshot is visually equivalent to
`/tmp/11-step1-baseline.png` (window in original position).

### Step 4 — maximised toplevel refuses drag

```bash
qdwin_ctrl "max $HANDLE" >/dev/null
sleep 0.3
MAX_GEOM=$(qdwin_ctrl "geom $HANDLE")
echo "maximised: $MAX_GEOM"

# Click + drag the centre of the titlebar (no buttons there in our
# layout). qdwin must refuse and the toplevel must NOT move.
qdwin_drag $((QDWIN_SCREEN_W/2)) 14 \
           $((QDWIN_SCREEN_W/2 + 150)) 100
sleep 0.3
POST=$(qdwin_ctrl "geom $HANDLE")
echo "post-drag (maximised): $POST"
```

**Assert (4.1):** weston log contains
`begin_interactive_move handle=$HANDLE ignored — toplevel is
maximised/fullscreen`. Use the same tail|grep as Step 2.
**Assert (4.2):** `geom` x/y unchanged from `MAX_GEOM` (still
0,0 modulo top panel).

### Step 5 — restore + drag works again (recovery path)

```bash
qdwin_ctrl "restore $HANDLE" >/dev/null
sleep 0.3
PRE=$(qdwin_ctrl "geom $HANDLE")

# Re-extract outer.x for the cursor pickup point.
read BX BY _ _ < <(echo "$PRE" | awk '
    { for (i=1;i<=NF;i++) {
        if ($i ~ /^x=/) x=substr($i,3);
        if ($i ~ /^y=/) y=substr($i,3);
      }
      print x, y, "_", "_"
    }')

qdwin_drag $((BX + 60)) $((BY + 14)) \
           $((BX + 60 + 100)) $((BY + 14 + 50))
sleep 0.3
FINAL=$(qdwin_ctrl "geom $HANDLE")
echo "final: $FINAL"
qdwin_screenshot /tmp/11-step5-restored-and-dragged.png
```

**Assert (5.1):** `geom` x ≈ PRE.x + 100, y ≈ PRE.y + 50 (±5 px).
Confirms maximised-refusal in step 4 didn't break the grab state
machine.

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; true' >/dev/null
```

## Pass criteria

All asserts 1.1 → 5.1 pass. Confirms:

- begin_interactive_move enters the move grab on press (qdshell
  side: dispatch_chrome_click; qdwin side:
  qdwin_handle_begin_interactive_move).
- Motion translates content + chrome views together
  (qdwin_move_grab_motion → weston_view_set_position +
  qdwin_toplevel_position_chrome).
- Button release ends the grab and emits toplevel_geometry with the
  new outer position.
- Maximised/fullscreen toplevels refuse silently and don't leak
  grab state.

## Known-broken-if

- Step 2 weston log shows `begin_interactive_move handle=N (stub)`
  → the v1 stub is still in place; check the diff against
  `qdwin.c` for `qdwin_move_grab_motion`.
- Step 2 chrome stays at old position while content moves → the
  motion handler isn't calling `qdwin_toplevel_position_chrome`.
- Step 2 motion works but window snaps back on release → the
  shell client may be re-asserting an absolute position via a
  configure round-trip. Check qdshell's `on_toplevel_geometry`
  for any `request_*_position` calls (there shouldn't be any).
- Step 4 toplevel moves while maximised → the
  QDWIN_TS_MAXIMIZED guard in `qdwin_handle_begin_interactive_move`
  is missing or wrong.
- Step 5 second drag does nothing → grab state wasn't cleared
  after the maximised-refusal path. Check
  `qdwin->move_grab_active` is only set on the success branch.

## Coordinate notes

The 28 px titlebar / 8 px border defaults are wired in
`qdshell shell.qml:_paint_titlebar` (constants near top
of file). If a future qdshell change moves them, update the
arithmetic in Step 1 / Step 5 — the test asserts on outer-rect
coords, not on the constants directly, so it stays robust to a
±few-pixel drift but a wholesale theme change will need updating.
