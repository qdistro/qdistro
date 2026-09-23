# 07 — locked screen fully occludes desktop pixels

<!-- qci:visual: required -->

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

# Detect actual output resolution from a baseline screenshot so this
# scenario is not tied to 1920x1080.
qdwin_screenshot /tmp/qdlocker-07-step0-baseline.png
read -r SW SH < <(qdlocker_screenshot_dimensions /tmp/qdlocker-07-step0-baseline.png)

# Make the desktop behind the locker unmistakable. A full-output normal
# xdg_toplevel maps at output origin when it is the only app toplevel,
# so any top/left lock-window offset exposes magenta immediately.
"$QDWIN_VM_EXEC" "$VMNAME" "
  pkill -u admin -x qdistro-test-window 2>/dev/null || true
  pkill -u admin -x foot 2>/dev/null || true
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
    qdistro-test-window --title qdlocker-sentinel \
      --width ${SW} --height ${SH} --color 0xffff00ff \
      >/tmp/qdlocker-sentinel.log 2>&1 &
"
sleep 1.5
```

## Steps

### Preflight — VM graphics backend is compositing

Before any visual assertion, confirm the compositor is actually scanning
out. On some VM graphics stacks libweston's DRM backend rejects every
atomic KMS commit — the journal fills with `atomic: couldn't commit new
state: Invalid argument` and `repaint-flush failed: Invalid argument`,
the scanout goes black, and every screenshot below would be a false
FAIL. That is an environment condition (VM graphics / KMS backend), not
a qdlocker or qdwin defect: `doc/compositor.md` requires VM targets to
work on virtio-gpu/virgl or pixman software rendering and forbids qdwin
from depending on GPU acceleration. Detect it here and stop as ERROR so
a pure graphics flake is not mistaken for an occlusion leak.

```bash
DRM_LOG=$("$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "journalctl --user -u qdwin-compositor.service --boot --no-pager"' \
  | grep -E "atomic: couldn't commit new state: Invalid argument|repaint-flush failed: Invalid argument" || true)
DRM_FAILS=$(printf '%s\n' "$DRM_LOG" | grep -cE "atomic: couldn't commit new state: Invalid argument|repaint-flush failed: Invalid argument" || true)
printf '%s\n' "${DRM_FAILS:-0}" \
  > "${QCI_SCENARIO_TMPDIR:-/tmp}/07-drm-baseline-count"

if [ "${DRM_FAILS:-0}" -ge 5 ]; then
    echo "ERROR: VM graphics backend is failing DRM atomic commits" \
         "($DRM_FAILS repeated 'atomic: couldn't commit new state' /" \
         "'repaint-flush failed: Invalid argument' in the last 2 minutes)." \
         "The compositor is not scanning" \
         "out, so every screenshot below would be black — this is a VM" \
         "graphics/KMS backend condition, NOT a qdlocker/qdwin occlusion" \
         "defect. See doc/compositor.md (virtio-gpu/virgl or pixman must" \
         "work; qdwin must not require GPU accel)." >&2
    echo "--- offending compositor journal lines ---" >&2
    printf '%s\n' "$DRM_LOG" | tail -20 >&2
    exit 78  # hard ERROR (VM graphics backend) — BLOCKED, not a product FAIL
fi
```

**Preflight gate:** fewer than 5 repeated `atomic: couldn't commit new
state: Invalid argument` failures in the current boot (a healthy
compositor emits zero; the flake bursts dozens per second). The count is saved
so Step 2 can also detect a graphics failure triggered by mapping the lock UI.
If the threshold is exceeded, the scenario stops here classified **ERROR (VM
graphics backend)** with the offending journal lines dumped — it is not
a qdlocker occlusion FAIL. A clean journal proceeds to Step 1.

### Step 1 — baseline sentinel is visible

```bash
qdwin_screenshot /tmp/qdlocker-07-step1-sentinel.png
qdlocker_assert_color_present_in_crop \
  /tmp/qdlocker-07-step1-sentinel.png '#ff00ff' "${SW}x${SH}+0+0" whole-screen
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

# A virtio-gpu GL/KMS failure can begin only when the full-output lock surface
# maps, after the preflight passed. Detect new failures before judging pixels.
DRM_BASELINE=$(cat "${QCI_SCENARIO_TMPDIR:-/tmp}/07-drm-baseline-count")
DRM_AFTER=$("$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "journalctl --user -u qdwin-compositor.service --boot --no-pager"' \
  | grep -cE "atomic: couldn't commit new state: Invalid argument|repaint-flush failed: Invalid argument" || true)
if [ $(( DRM_AFTER - DRM_BASELINE )) -ge 5 ]; then
    echo "ERROR: VM graphics backend began rejecting DRM commits after lock" \
         "($(( DRM_AFTER - DRM_BASELINE )) new failures); screenshot evidence is invalid" >&2
    exit 78
fi
```

**Assert (2.1):** `qdlocker_ctrl status` reports `locked=True`.
**Assert (2.2):** screenshot shows qdlocker UI.
**Graphics gate:** no burst of new DRM atomic/repaint failures appeared after
the lock surface mapped. A burst is an environment **ERROR**, not a black-UI
product failure.

### Step 3 — no sentinel pixels remain in edge bands

```bash
edge_h=$(( SH / 11 > 96 ? SH / 11 : 96 ))
edge_w=$(( SW / 12 > 160 ? SW / 12 : 160 ))
qdlocker_assert_color_absent_in_crop \
  /tmp/qdlocker-07-step2-locked.png '#ff00ff' "${SW}x${edge_h}+0+0" top-edge
qdlocker_assert_color_absent_in_crop \
  /tmp/qdlocker-07-step2-locked.png '#ff00ff' "${edge_w}x${SH}+0+0" left-edge
qdlocker_assert_color_absent_in_crop \
  /tmp/qdlocker-07-step2-locked.png '#ff00ff' "${edge_w}x${edge_h}+0+0" top-left-corner
qdlocker_assert_color_absent_in_crop \
  /tmp/qdlocker-07-step2-locked.png '#ff00ff' "${SW}x${SH}+0+0" whole-screen
```

**Assert (3.1):** all four commands exit zero. Any magenta pixel means
normal desktop content leaked through the lock screen.

### Step 4 — compositor journal corroborates fullscreen origin

```bash
LOCKER_LOG=$("$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "journalctl --user -u qdwin-compositor.service --boot --no-pager"')
printf '%s\n' "$LOCKER_LOG" \
  | grep -E "set_fullscreen handle=.* outer=${SW}x${SH} at \\(0,0\\)"
printf '%s\n' "$LOCKER_LOG" | grep "promoted locker toplevel"
```

**Assert (4.1):** journal contains both the locker promotion and a
fullscreen placement at `(0,0)`. This is not a substitute for the
pixel check; it narrows the failure if Step 3 fails. Use the current boot
because visual inspection can take longer than a wall-clock journal window.

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
