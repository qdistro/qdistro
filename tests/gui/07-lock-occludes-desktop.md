# 07 — locked screen fully occludes desktop pixels

**Acceptance criterion (security):** once qdlocker reports
`locked=True`, no pixel from a normal desktop toplevel is visible at
the screen edges or corners. This catches the historical bug where the
Qt lock window was promoted to the LOCK layer but kept its normal
toplevel cascade offset, leaving strips of desktop visible above and
to the left.

This scenario is intentionally partly mechanical: the sentinel window
is solid magenta (`#ff00ff`), and the asserts scan screenshots for
that exact color. Do not replace these with "looks fine" visual-only
checks.

## Setup

```bash
source "$(dirname "$0")/qdlocker-helpers.sh"
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdlocker_session_healthy || { echo "FAIL: session not up"; exit 2; }

# Drain a stale locked state from a prior scenario.
qdlocker_drain_lock_state

# Make the desktop behind the locker unmistakable. A 1920x1080 normal
# xdg_toplevel maps at output origin when it is the only app toplevel,
# so any top/left lock-window offset exposes magenta immediately.
"$QDWIN_VM_EXEC" "$VMNAME" '
  pkill -u admin -x qdistro-test-window 2>/dev/null || true
  pkill -u admin -x foot 2>/dev/null || true
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
    qdistro-test-window --title qdlocker-sentinel \
      --width 1920 --height 1080 --color 0xffff00ff \
      >/tmp/qdlocker-sentinel.log 2>&1 &
'
sleep 1.5
```

## Steps

### Step 1 — baseline sentinel is visible

```bash
qdwin_screenshot /tmp/qdlocker-07-step1-sentinel.png
qdlocker_assert_color_present_in_crop \
  /tmp/qdlocker-07-step1-sentinel.png '#ff00ff' '1920x1080+0+0' whole-screen
```

**Assert (1.1):** the screenshot contains magenta. If this fails,
the sentinel window did not map and the occlusion test is invalid.

### Step 2 — engage qdlocker

```bash
qdlocker_ctrl lock
qdlocker_wait_for_lock 5
sleep 0.5
qdwin_screenshot /tmp/qdlocker-07-step2-locked.png
qdlocker_ctrl status
```

**Assert (2.1):** `qdlocker_ctrl status` reports `locked=True`.
**Assert (2.2):** screenshot shows qdlocker UI.

### Step 3 — no sentinel pixels remain in edge bands

```bash
qdlocker_assert_color_absent_in_crop \
  /tmp/qdlocker-07-step2-locked.png '#ff00ff' '1920x96+0+0' top-edge
qdlocker_assert_color_absent_in_crop \
  /tmp/qdlocker-07-step2-locked.png '#ff00ff' '160x1080+0+0' left-edge
qdlocker_assert_color_absent_in_crop \
  /tmp/qdlocker-07-step2-locked.png '#ff00ff' '160x160+0+0' top-left-corner
qdlocker_assert_color_absent_in_crop \
  /tmp/qdlocker-07-step2-locked.png '#ff00ff' '1920x1080+0+0' whole-screen
```

**Assert (3.1):** all four commands exit zero. Any magenta pixel means
normal desktop content leaked through the lock screen.

### Step 4 — compositor journal corroborates fullscreen origin

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "journalctl --user -u qdwin-compositor.service --since \"1 minute ago\" --no-pager"' \
  | grep -E 'set_fullscreen handle=.* outer=1920x1080 at \(0,0\)|promoted locker toplevel'
```

**Assert (4.1):** journal contains both the locker promotion and a
fullscreen placement at `(0,0)`. This is not a substitute for the
pixel check; it narrows the failure if Step 3 fails.

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" '
  pkill -u admin -x qdistro-test-window 2>/dev/null || true
'
qdlocker_drain_lock_state
sleep 2
```

## Known-broken-if

- Step 1 has no magenta — `qdistro-test-window` is missing, did not
  connect to `wayland-1`, or another toplevel covered it. Check
  `/tmp/qdlocker-sentinel.log` in the guest and rerun on a clean
  desktop.
- Step 2 reports `locked=True` but Step 3 finds magenta in the
  `top-edge` or `left-edge` crop — qdwin is still applying normal
  first-map placement to the fullscreen locker toplevel. The fix is
  in qdwin's first-commit/fullscreen geometry path.
- Step 3 only fails in `whole-screen` — the lock UI itself is using
  the sentinel color or another magenta surface is above the locker.
  Change the sentinel color and rerun before treating it as a qdwin
  leak.
