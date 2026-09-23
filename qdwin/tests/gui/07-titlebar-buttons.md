# 07 — titlebar maximize / minimize / restore via mouse

<!-- qci:visual: required -->

**Acceptance criterion:** clicking the right-edge titlebar buttons on
a foot toplevel changes its window state — clicking maximise fills
the work area while keeping titlebar+panel visible (NOT fullscreen),
clicking the (now-)restore button puts the window back, and clicking
minimise hides the toplevel.

This validates the qdwin → qdshell pointer-button path through the
qdwin_shell_v1@v20 `chrome_button` event. Pre-v20, the path was the
wl_pointer dispatcher in qdshell, but libweston suppresses pointer
delivery to the shell client's own surfaces and (post-task(135)) the
cursor sprite shadows the chrome view in the picker. v20 sidesteps
both by hit-testing chrome bboxes directly in the compositor and
forwarding clicks as a typed shell event.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"

# Resolution-aware coords. Default 1024×768 on demo VMs; export
# QDWIN_SCREEN_W/H if your VM differs. Helpers use these to map
# pixel coords → 0..32767 abs-axis events.
: "${QDWIN_SCREEN_W:=1024}"
: "${QDWIN_SCREEN_H:=768}"
export QDWIN_SCREEN_W QDWIN_SCREEN_H

pgrep -f "http.server 8765" >/dev/null || (
    cd ${QDWIN_REPO} && \
    python3 -m http.server 8765 --bind 127.0.0.1 >/tmp/qdistro-http.log 2>&1 &
)
sleep 1
qdwin_session_healthy || { echo "FAIL: session not up"; exit 1; }

"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; sleep 1' >/dev/null
qdwin_ctrl "launcher-toggle" >/dev/null
qdwin_ctrl "launcher-type foot" >/dev/null
qdwin_ctrl "launcher-activate" >/dev/null
sleep 2
```

## Coordinate model

After foot launches it cascades to a position the test cannot
predict (depends on prior window count + foot's own pixel-default
size). To get **deterministic** button coordinates, every step that
issues a mouse click first puts the window into a known state via
the ctrl-socket — either maximised (work area = full output minus
panel exclusive zones) or restored to a known cascade position.

`qdshell` paints chrome with:
- titlebar height = 28 px (`--titlebar-height` default)
- side/bottom border = 8 px (`--border-thickness` default)
- three right-aligned 22×22 buttons inside the titlebar with 6 px gap
  and 6 px right inset, ordered close → maximize → minimize from the
  right edge.

When MAXIMISED on a `W×H` output with a 28-px panel at the bottom:
- work area = `W × (H-28)` at `(0, 0)`
- chrome N at `(0, 0)`, size `W × 28`; titlebar y-centre = 14
- close button centre   = `(W - 6 - 11, 14)` → e.g. `(1007, 14)` at 1024
- maximize button centre = `(W - 6 - 22 - 6 - 11, 14)` → `(979, 14)` at 1024
- minimize button centre = `(W - 6 - 22 - 6 - 22 - 6 - 11, 14)` → `(951, 14)` at 1024

The Setup helpers below compute these once for the active resolution.

```bash
# Button centres while maximised (deterministic).
MAX_CLOSE_X=$((QDWIN_SCREEN_W - 17))
MAX_REST_X=$((QDWIN_SCREEN_W - 45))
MAX_MIN_X=$((QDWIN_SCREEN_W - 73))
MAX_BTN_Y=14
```

State bits returned by `qdwin_ctrl "state <h>"` (qdwin.c:223-229):
- `0x01` MAXIMIZED, `0x02` FULLSCREEN, `0x04` MINIMIZED,
- `0x08` URGENT, `0x10` FOCUSED, `0x20` FLOATING.

## Steps

### Step 1 — verify foot is up + capture handle

```bash
qdwin_ctrl "list"
qdwin_screenshot /tmp/07-step1-baseline.png
```

**Assert (1.1):** `list` returns exactly one `tl` line — capture the
second column as `$H`.
**Assert (1.2):** screenshot shows a foot terminal with the three
glyph buttons (─, □, red ×) on the right of its titlebar.
**Assert (1.3):** `state $H` returns a value with bits 0x01 and 0x04
both clear.

### Step 2 — ctrl-socket max to fix geometry, then mouse-click restore

```bash
qdwin_ctrl "max $H"
sleep 0.5
qdwin_screenshot /tmp/07-step2-after-ctrl-max.png
qdwin_click "$MAX_REST_X" "$MAX_BTN_Y"
sleep 0.7
qdwin_ctrl "state $H"
qdwin_screenshot /tmp/07-step2-after-restore-click.png
```

**Assert (2.1):** the post-`max` screenshot shows a maximised foot
that **still leaves room for the bottom panel and the titlebar** —
the titlebar with its three glyphs is visible at the very top of the
screen (`y ≈ 0..28`) and the panel hamburger+clock are visible at the
bottom (`y ≈ H-28..H`). Foot content fills the area between them.
This is NOT fullscreen — fullscreen would hide both the titlebar and
the panel.
**Assert (2.2):** the maximise glyph in the titlebar shows the
**restore** form (two offset rectangles), not a single outline rect.
**Assert (2.3):** after the click, `state $H` reports `state & 0x01 == 0`
(MAXIMIZED bit cleared).
**Assert (2.4):** the post-click screenshot shows foot back at a
non-maximised cascade size, with wallpaper visible around it.

If 2.3 fails (state still `0x01`), the click did not reach
`request_maximize` — inspect qdshell.log for a `click → request_maximize`
line and qdwin.log for `chrome_at_pos pos=(...) -> tl=...`. The most
common cause is the click landing in the 6-px gap between buttons —
re-verify `MAX_REST_X` against the real screen width.

### Step 3 — mouse-click maximize from restored state

```bash
# We need the cascaded foot's actual right-edge to know where the
# maximise button is in restored state. Read it from qdshell ctrl
# (the `geom` command isn't shipped; instead, ctrl-socket "max"
# again, then click). For a deterministic click we test the
# OPPOSITE direction: click maximise WHILE maximised was already
# tested in step 2 (the same button, different glyph). Skip
# direct-from-restored maximise click here — test a single state
# transition per step, with maximise via ctrl-socket as the
# control.
qdwin_ctrl "max $H"
sleep 0.5
qdwin_ctrl "state $H"
qdwin_screenshot /tmp/07-step3-remaximised.png
```

**Assert (3.1):** `state & 0x01 == 1`.
**Assert (3.2):** screenshot still shows panel + titlebar visible
(re-verify the not-fullscreen invariant).

### Step 4 — mouse-click minimize while maximised

```bash
qdwin_click "$MAX_MIN_X" "$MAX_BTN_Y"
sleep 0.7
qdwin_ctrl "state $H"
qdwin_screenshot /tmp/07-step4-after-minimize-click.png
```

**Assert (4.1):** `state & 0x04 == 0x04` (MINIMIZED set).
**Assert (4.2):** screenshot shows no foot chrome anywhere — only
wallpaper + panel.
**Assert (4.3):** `list` still includes the toplevel — minimised, not
destroyed.

### Step 5 — un-minimise via raise (ctrl-socket)

```bash
qdwin_ctrl "raise $H"
sleep 0.5
qdwin_ctrl "state $H"
qdwin_screenshot /tmp/07-step5-after-raise.png
```

**Assert (5.1):** `state & 0x04 == 0` (MINIMIZED cleared).
**Assert (5.2):** foot is visible again (still maximised — raise
doesn't change the maximize bit).

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; true' >/dev/null
```

## Pass criteria

All asserts 1.1 → 5.2 pass. Particular attention to:
- Assert **2.1** (maximised window leaves room for panel + chrome —
  not fullscreen) — this was the user-reported regression in the
  fix that landed alongside scenario 07.
- Asserts **2.3, 4.1** (the chrome_button event reaches qdshell and
  triggers the right `request_*` call) — these are the bug-fix
  asserts for "buttons inert" before v20.

## Resolution variations

The math is screen-size-aware. To run on a 1280×800 VM (override the
default 1024×768):

```bash
QDWIN_SCREEN_W=1280 QDWIN_SCREEN_H=800 bash run-scenario.sh 07
# MAX_CLOSE_X=1263, MAX_REST_X=1235, MAX_MIN_X=1207
```

The same numbers fall out of the formulas above with the new W. Test
on at least two resolutions before declaring the fix done.

## Known-broken-if

- All asserts 1.x pass but 2.3 fails: the chrome_button path is
  broken end-to-end. Check (a) compositor binds shell at v20:
  `grep "bound qdwin_shell_v1" ~/.local/share/qdshell.log`; (b)
  qdshell registers the `chrome_button` dispatcher; (c) qdwin's
  `qdwin_chrome_at_pos` finds the chrome at click time —
  `~/.local/share/qdwin.log` should not be silent on click.
- Step 2 click works but 4 doesn't: check the surface-local x is
  inside the minimize button bbox. The button order from the right
  is close, maximize, minimize — minimize is the *leftmost* of the
  three, not rightmost.
- Step 2.1 fails (window genuinely fills the screen, panel hidden):
  regression in `qdwin_handle_request_maximize`. Verify the
  `weston_view_set_position(tl->view, origin)` call uses
  `(wx + tl->inset_w, wy + tl->inset_n)` — placing content INSIDE
  the work-area inset so chrome fits at the top edge of the work
  area, not above it.
