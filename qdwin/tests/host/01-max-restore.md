# 01 — maximise / restore round-trip on a single window

**What**: open a weston-terminal, capture the baseline (windowed),
maximise via the ctrl-socket, capture (full screen), restore, capture
(windowed again). Verify the window grows to fill the output and
shrinks back.

**Why**: the §6.3 `request_maximize` handler is the load-bearing
piece for a desktop-feel window manager. Visual confirmation that
both directions of the toggle reach the screen, plus that
`apply_inset` shrinks the content surface to leave room for chrome
on the maximised geometry.

## Setup

```bash
ID=01-max-restore
HT=tests/host
$HT/start.sh $ID
```

After Setup the harness has spawned: weston (headless, 1024×640),
weston-terminal, qdshell with default uid colour (`$UID=#22aaff`).

## Steps

### S1 — baseline screenshot

```bash
$HT/ctrl.sh $ID list
SHOT=$($HT/screenshot.sh $ID 01-baseline)
```

**Read `$SHOT`.**

**Assert (baseline):**
- A single window is visible, NOT filling the screen — the bottom
  third (or so) of the framebuffer is black background.
- Window has a cyan/blue (#22aaff-ish) border on at least the left
  and bottom edges (qdshell chrome). The exact thickness is 8px,
  but vision-level "thin colored strip" is enough.
- A grey titlebar reading "zbook" (or the host's hostname) sits at
  the top of the window — that's weston-terminal's own client-side
  decoration drawn on top of the chrome's N edge.
- The window contains a shell prompt (`user@host:~/path>`).

### S2 — maximise

```bash
$HT/ctrl.sh $ID max 1
sleep 0.4
SHOT=$($HT/screenshot.sh $ID 02-max)
$HT/ctrl.sh $ID state 1
```

**Read `$SHOT`.**

**Assert (maximised):**
- The window now fills the entire framebuffer (or close to it — the
  outer rectangle reaches all four screen edges).
- Cyan border still visible at the screen edges (qdshell chrome
  follows the resize).
- `ctrl.sh $ID state 1` prints `ok state=0x1` (bit 0 = QDWIN_TS_MAXIMIZED).
  Note: `state` is a query synthesised by ctrl.sh — it scrapes the
  most recent `toplevel_state handle=1 state=0x..` line from
  `qdshell.log` (where the bystander logs every state change emitted
  by the compositor). It does NOT round-trip through the command FIFO.
- Black background outside the window is gone — there's no
  unfilled space.

### S3 — restore

```bash
$HT/ctrl.sh $ID restore 1
sleep 0.4
SHOT=$($HT/screenshot.sh $ID 03-restored)
$HT/ctrl.sh $ID state 1
```

**Read `$SHOT`.**

**Assert (restored):**
- Window is back at the small windowed size from baseline (S1).
- Black background is visible again on the right + bottom.
- Cyan chrome present on left + bottom edges.
- `ctrl.sh $ID state 1` prints `ok state=0x0` (no flags). See the
  scraping note under S2.

## Teardown

```bash
$HT/stop.sh $ID
```

## Notes for the runner

- All three screenshots live under `/tmp/qdwin-host-tests/$ID/shots/`.
  Reference them by full path in your report.
- "Fills the entire framebuffer" can be soft — qdwin maximises to
  the headless output's logical size (1024×640 here). If the cyan
  border is at x=0 and x=1023 (or near), that counts.
- If the cyan border isn't visible in any frame, FAIL — that means
  qdshell either didn't bind chrome or didn't repaint after
  `toplevel_state`.
