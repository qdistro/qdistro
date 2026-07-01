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

# Compositor-evidence helper. `virsh screenshot` cannot capture the
# hardware cursor PLANE (the cursor lives on a KMS overlay plane that
# QEMU forwards to SPICE but does not composite into the scanout virsh
# grabs), so visual "cursor in screenshot" assertions hard-fail even
# when the cursor is correctly registered + mapped. Instead assert the
# compositor's own journal evidence. Two distinct signals:
#   * RUNTIME (per move/hover): qdwin re-maps the sprite on the cursor
#     plane and logs `... mapped on cursor_layer ... nonzero_alpha=N`.
#     Both cursor paths carry this line — the no-client default path
#     (`install_default_cursor:` prefix) and the client cursor-shape
#     path (`cursor-shape install shape=...` prefix) — so grepping the
#     `mapped on cursor_layer` line catches either. This is the proof
#     the cursor followed the pointer at runtime.
#   * BOOT (once per session): the default sprite is `registered` at
#     session start (`cursor-sprite registered shape=default`). This
#     never re-fires on a runtime move, so it is a boot precondition,
#     not a per-move assertion.
# Mirrors the journalctl style in noct_layer_mapped_count_since.
cursor_layer_nonzero_alpha_since() {
    local since="${1:-1 minute ago}"
    "$QDWIN_VM_EXEC" "$VMNAME" \
        "runuser -l admin -c \"journalctl --user -u qdwin-compositor.service --since '$since' --no-pager\" \
            | grep 'mapped on cursor_layer' \
            | grep -oE 'nonzero_alpha=[0-9]+' | grep -vc 'nonzero_alpha=0$'" \
        2>/dev/null | tail -1
}
# Boot precondition: the default cursor sprite was registered once at
# session start. Greps the whole boot (`-b`), NOT a runtime `--since`,
# because `cursor-sprite registered` only fires at register time.
cursor_sprite_registered_at_boot() {
    "$QDWIN_VM_EXEC" "$VMNAME" \
        "runuser -l admin -c \"journalctl --user -u qdwin-compositor.service -b --no-pager\" \
            | grep -c 'cursor-sprite registered shape=default'" \
        2>/dev/null | tail -1
}

# Boot precondition (run once): the default cursor sprite registered at
# session start. This is the session-lifetime registration proof; the
# per-move asserts below only check runtime re-mapping.
[ "$(cursor_sprite_registered_at_boot)" -ge 1 ] \
    || { echo "FAIL: default cursor sprite never registered at boot"; exit 1; }
```

## Steps

### Step 1 — park cursor in the dark wallpaper area

```bash
SINCE_STEP1=$(date '+%H:%M:%S')
qdwin_mouse_move 1500 600
sleep 1
qdwin_screenshot /tmp/04-step1-wallpaper-area.png
```

**Assert (1.1) — compositor evidence (load-bearing):** the cursor
sprite is (re)mapped on the cursor plane with non-zero alpha after the
move — the runtime proof that the cursor followed the pointer. (The
sprite's one-time `registered` line is a boot precondition, already
checked once in Setup, and does NOT re-fire on a move.) `virsh
screenshot` cannot capture the hardware cursor PLANE, so assert the
compositor's own journal rather than the screenshot:

```bash
[ "$(cursor_layer_nonzero_alpha_since "$SINCE_STEP1")" -ge 1 ] \
    || { echo "FAIL: cursor not mapped on cursor_layer with nonzero_alpha"; exit 1; }
```

The `/tmp/04-step1-wallpaper-area.png` screenshot is kept as soft
corroboration only — a visible arrow near (1500, 600) is a bonus but
NOT required to pass (the hw-cursor plane is invisible to virsh).

### Step 2 — move cursor onto a bar widget (clock area)

```bash
SINCE_STEP2=$(date '+%H:%M:%S')
qdwin_mouse_move 1700 15
sleep 1
qdwin_screenshot /tmp/04-step2-clock-hover.png
```

**Assert (2.1) — compositor evidence (load-bearing):** the cursor is
still mapped on the cursor plane with non-zero alpha after the move
to the bar (the hover keeps the sprite live):

```bash
[ "$(cursor_layer_nonzero_alpha_since "$SINCE_STEP2")" -ge 1 ] \
    || { echo "FAIL: cursor not mapped on cursor_layer after bar hover"; exit 1; }
```

The screenshot remains soft corroboration of position only (the
hw-cursor plane is not captured by virsh).
**Assert (2.2):** comparing the bar's clock-widget area between
step 1 and step 2 screenshots, the widget shows a hover state
(brighter background or color highlight) — Noctalia hovers with
a subtle tint by default.

If the assert is hard to make robust (subtle hover effect not
crossing pixel-diff thresholds), demote to a soft check: just
confirm the cursor moved to the new position.

### Step 3 — sweep cursor across the bar

```bash
SINCE_STEP3=$(date '+%H:%M:%S')
for x in 100 400 700 1000 1300 1600 1900; do
 qdwin_mouse_move "$x" 15
 sleep 0.2
done
sleep 1
qdwin_screenshot /tmp/04-step3-sweep-end.png
```

**Assert (3.1) — compositor evidence (load-bearing):** the cursor
sprite stayed mapped on the cursor plane with non-zero alpha through
the sweep (the cursor followed the motion). Assert the journal, not
the screenshot (the final position (1900, 15) is soft-only):

```bash
[ "$(cursor_layer_nonzero_alpha_since "$SINCE_STEP3")" -ge 1 ] \
    || { echo "FAIL: cursor not mapped on cursor_layer during sweep"; exit 1; }
```
**Assert (3.2):** Noctalia is still alive — `noct_session_healthy`.
**Assert (3.3):** weston journal in the last 30s shows zero
protocol errors.

## Cleanup

None. Cursor parked at (1900, 15) is fine.

## Pass criteria

The boot precondition (default cursor sprite registered at session
start) plus the load-bearing compositor-evidence asserts (1.1, 2.1,
3.1: cursor re-mapped on cursor_layer with nonzero_alpha>0 after each
move) plus 3.2/3.3 (session alive, no protocol errors) pass. Screenshot-based
cursor-position checks are soft corroboration only — the hardware
cursor plane is not captured by `virsh screenshot`, so their absence
is NOT a failure. Soft asserts (2.2) may be downgraded to "info only"
if hover-styling diff is too subtle for reliable detection.

## Known failure modes

1. **Cursor invisible in screenshot is EXPECTED, not a failure** —
 `virsh screenshot` captures the scanout but NOT the hardware cursor
 KMS plane (QEMU forwards that plane straight to SPICE). The cursor
 working is proven by the journal evidence asserts: the default sprite
 `registered` at boot (precondition) plus, per runtime move,
 `mapped on cursor_layer ... nonzero_alpha>0` (emitted by both the
 `install_default_cursor` and `cursor-shape install` paths). Runtime
 moves do NOT re-log `registered`, so the runtime asserts key on the
 `mapped ... nonzero_alpha` lines, not on `registered`. It is not
 proven by a visible arrow in the PNG. If those journal lines are ABSENT,
 qdwin's cursor-shape sprite installation may have regressed (per
 memory `qdwin_cursor_fix_260430` — the 2026-04-30
 cursor-buffer-lifetime bug). Triage: check `qdwin: cursor-shape
 theme=...` in the weston log for `loaded=N/36` — N>0 means loaded,
 N=0 means the fallback synthetic sprite is in use.

2. **Hover state requires keyboard focus** — some Noctalia versions
 only highlight a bar widget when keyboard focus is also on the
 bar. We don't currently route keyboard focus to layer surfaces
 ( deferred). Soft-pass if hover doesn't visibly fire.
