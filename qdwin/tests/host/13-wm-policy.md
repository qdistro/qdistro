# 13 — window-manager policy: set_wm_policy / tile / fullscreen / hotkey (v25)

**What**: drive the v25 window-manager-policy surface against the headless
compositor via the bystander's command FIFO: push a `set_wm_policy`
snapshot, half-screen-tile a window left and restore it, toggle
fullscreen, and register a WM-shortcut hotkey. Assert on the compositor's
own `weston.log` lines (the geometry-changing actions log their resulting
rectangle).

**Why**: v25 is what flips the qdshell WindowManager settings tab from
persist-only to live-apply (`CapabilityService.wmPolicy`). The
load-bearing pieces are `set_wm_policy` (focus model / placement / snap),
`request_tile` (half-screen tiling with save/restore), `request_fullscreen`
(shell-driven fullscreen toggle), and the now-wired v19 `register_hotkey`
path the WM keyboard shortcuts ride on. This exercises the request
plumbing + the geometry math headlessly, independent of qdshell.

**Non-visual**: headless weston has no input backend, so this scenario
canNOT exercise focus-follows-mouse retargeting, edge snapping during an
interactive move, or actual hotkey *delivery* (key press → hotkey_pressed)
— those need real input and are covered by the agent VM GUI scenario
`tests/gui/19-wm-policy.md`. Here we assert that the requests are accepted
(no protocol error / client disconnect) and that tile/fullscreen produce
the expected work-area rectangles.

## Setup

```bash
ID=13-wm-policy
HT=tests/host
$HT/start.sh $ID
```

After Setup the harness has spawned: weston (headless, 1024×640),
weston-terminal (handle 1), and qdwin-bystander as the shell helper
bound at qdwin_shell_v1 v25.

```bash
RUN() { $HT/ctrl.sh $ID "$@" >/dev/null; sleep 0.4; }
WLOG=$($HT/.. 2>/dev/null; printf '%s' "/tmp/qdwin-host-tests/$ID/weston.log")
# (weston.log path: ht_log_weston $ID — see lib.sh)
```

## Steps

### S1 — push a WM policy snapshot

```bash
# wmpolicy <focus> <ffm_ms> <raise_click> <raise_hover> <place> <snap_en> <snap_dist>
# focus 1 = follow-mouse, placement 2 = smart
RUN wmpolicy 1 200 1 0 2 1 24
grep -q 'set_wm_policy focus=1 ffm_delay=200 raise_click=1 raise_hover=0 placement=2 snap=1 dist=24' "$WLOG" \
  && echo "S1 PASS" || echo "S1 FAIL"
```

**Assert:** weston.log has the `set_wm_policy …` line with the exact
clamped/normalised fields. The request did NOT disconnect the client
(no `libwayland: error` line, no `is NULL` opcode abort).

### S2 — tile left, then restore

```bash
RUN tile 1 left
# work area is the full 1024×640 output (no panels in --no-shell-less run);
# left half = 512×640 at (0,0).
grep -q 'qdwin: tile handle=1 edge=left outer=512x640 at (0,0)' "$WLOG" \
  && echo "S2a PASS" || echo "S2a FAIL"
RUN tile 1 none
grep -q 'qdwin: tile handle=1 restored' "$WLOG" && echo "S2b PASS" || echo "S2b FAIL"
```

**Assert:** tiling left sizes the window to the left half of the work
area; `tile none` restores its pre-tile geometry (the `restored WxH@(x,y)`
line echoes the saved floating rect).

### S3 — fullscreen toggle

```bash
RUN fullscreen 1 1
grep -q 'set_fullscreen handle=1 fs=1 outer=1024x640 at (0,0)' "$WLOG" \
  && echo "S3a PASS" || echo "S3a FAIL"
RUN fullscreen 1 0
grep -q 'set_fullscreen handle=1 fs=0 restored=' "$WLOG" && echo "S3b PASS" || echo "S3b FAIL"
```

**Assert:** fullscreen fills the whole output (covering any panels);
un-fullscreen restores the saved rect.

### S4 — register a WM-shortcut hotkey

```bash
# id=7101 (WindowManagerService hkClose), mods=0x2 (Alt), key=62 (KEY_F4) → Alt+F4
RUN hotkey 7101 2 62
grep -q 'register_hotkey id=7101 mods=0x2 key=62' "$WLOG" && echo "S4 PASS" || echo "S4 FAIL"
```

**Assert:** the hotkey registration is accepted and logged. (Press
delivery is VM-only — see Non-visual above.)

## Teardown

```bash
$HT/stop.sh $ID
```

## Result (validated 2026-05-29, headless)

S1–S4 all PASS: `set_wm_policy focus=1 … placement=2 snap=1 dist=24`,
`tile handle=1 edge=left outer=512x640 at (0,0)`, `tile handle=1 restored
806x539@(109,50)`, `set_fullscreen … fs=1 outer=1024x640 at (0,0)` /
`fs=0 restored=…`, `register_hotkey id=7101 mods=0x2 key=62`. No
`libwayland: error` / NULL-opcode abort (bystander bound at v25).
