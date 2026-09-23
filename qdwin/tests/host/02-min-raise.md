# 02 — minimise hides the window; raise brings it back

**What**: with a windowed terminal visible, send `min 1` and assert
the window disappears (only black framebuffer remains). Then send
`raise 1` and assert the window comes back unchanged.

**Why**: §6.3 minimise routes the content view + chrome views to
`minimized_layer` (HIDDEN); un-minimise (currently double-purposed
on `request_raise` until §6.4 splits them) routes them back to
`normal_layer`. Visual confirmation that nothing leaks through when
hidden, and that the window restores cleanly.

## Setup

```bash
ID=02-min-raise
HT=tests/host
$HT/start.sh $ID
```

## Steps

### S1 — baseline

```bash
SHOT=$($HT/screenshot.sh $ID 01-baseline)
```

**Read `$SHOT`.**

**Assert (window present):**
- Same baseline shape as scenario 01 — windowed terminal with cyan
  chrome on left/bottom, terminal prompt visible inside.

### S2 — minimise

```bash
$HT/ctrl.sh $ID min 1
sleep 0.4
SHOT=$($HT/screenshot.sh $ID 02-minimised)
$HT/ctrl.sh $ID state 1
```

**Read `$SHOT`.**

**Assert (window gone):**
- The framebuffer is **entirely black** (or the compositor's
  background colour) — no terminal text, no titlebar, no cyan
  border, no chrome of any kind.
- `ctrl.sh $ID state 1` prints `ok state=0x4` (bit 2 = QDWIN_TS_MINIMIZED).
  Note: `state` is a query synthesised by ctrl.sh — it scrapes the
  most recent `toplevel_state handle=1 state=0x..` line from
  `qdshell.log` rather than round-tripping through the command FIFO.

### S3 — raise (un-minimise)

```bash
$HT/ctrl.sh $ID raise 1
sleep 0.4
SHOT=$($HT/screenshot.sh $ID 03-raised)
$HT/ctrl.sh $ID state 1
```

**Read `$SHOT`.**

**Assert (window back):**
- Window is back at the same position + size as the baseline.
- Cyan chrome visible on the same edges.
- Terminal prompt visible inside.
- `ctrl.sh $ID state 1` prints `ok state=0x0`. See the scraping note
  under S2.

## Teardown

```bash
$HT/stop.sh $ID
```

## Notes for the runner

- "Entirely black" assertion is strong on purpose. If you see ANY
  pixels that aren't background colour in the minimised screenshot
  (a chrome strip, a titlebar peeking through, a terminal-cursor
  artefact), FAIL. Hidden means hidden.
- The "raise as un-minimise" double duty is temporary — §6.4 will
  add a dedicated `request_unminimize`. For now `raise` is the only
  way back. Don't be alarmed by the name.
