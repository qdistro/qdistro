# 04 — cursor follows mouse moves

<!-- qci:visual: none -->

**Acceptance criterion:** moving the mouse via QMP `input-send-event`
across Noctalia surfaces makes qdwin (re)install a visible cursor sprite
(non-zero alpha) on the cursor plane at each pointer-enter transition
(bar, wallpaper). Hovering over a bar widget triggers the appropriate
hover state (color shift or icon highlight), soft-checked.

Scope limit: the compositor journal records shape, payload and alpha,
not the pointer or cursor-plane position, and `virsh screenshot` does
not capture the hardware cursor plane. This scenario therefore proves
pointer delivery to the right surfaces (their enter handlers fire) and
a visible sprite, NOT the cursor's pixel position.

This exercises:
- Pointer event delivery (already proven in scenario 02 by the
 click; this scenario isolates motion without click)
- Cursor sprite installation on a layer surface (Noctalia uses
 cursor-shape-v1)
- wp_cursor_shape_manager_v1 binding by Noctalia

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
source ${QDISTRO_REPO}/tests/integration/qdwin-noctalia/noctalia-helpers.sh
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
#     `mapped on cursor_layer` line catches either. It proves a visible
#     sprite was (re)installed on a pointer transition, not a position.
#   * BOOT (once per session): the default sprite is `registered` at
#     session start (`cursor-sprite registered shape=default`). This
#     never re-fires on a runtime move, so it is a boot precondition,
#     not a per-move assertion.
# Mirrors the journalctl style in noct_layer_mapped_count_since.
# cursor_layer_nonzero_alpha_after and noct_wait_cursor_layer_nonzero_alpha
# live in noctalia-helpers.sh (sourced above). The count is cursor-scoped,
# not a --since window: a same-second stale remap from a prior move cannot
# satisfy it. The waiter polls that same predicate (mapped on cursor_layer,
# nonzero_alpha>0) until it is true or NOCT_CURSOR_WAIT_S (default 30s,
# every NOCT_CURSOR_POLL_S, default 0.25s). A fixed sleep races journal
# visibility when many GUI workers share the host.
# Capture the current qdwin-compositor user-journal cursor (empty string on
# failure). Call immediately BEFORE a cursor move to scope the per-move assert.
compositor_journal_cursor() {
    "$QDWIN_VM_EXEC" "$VMNAME" \
        "runuser -l admin -c \"journalctl --user -u qdwin-compositor.service -n0 --show-cursor 2>/dev/null\" \
            | sed -n 's/^-- cursor: //p'"
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

All coordinates below are authored for the GUI CI display profile of
**1280x800** (`QDWIN_SCREEN_W`/`QDWIN_SCREEN_H` defaults in
`qdwin-helpers.sh`). Keep every move strictly in-bounds — an
off-screen QMP move lands nowhere, produces no cursor remap, and
hard-fails the journal asserts even though the cursor code is fine.

### Step 1 — park cursor in the dark wallpaper area

qdwin logs `mapped on cursor_layer` only when a sprite is (re)installed:
the no-client default path, or a client's `wp_cursor_shape` set_shape.
Motion alone logs nothing. Measured on a golden clone of the full run
(F5 probe, 2026-09-30): wallpaper→wallpaper moves (first move, same
point, another point) never log; every move that enters a Quickshell
surface does, because the client sets its shape on pointer enter:
wallpaper→bar logs `cursor-shape install shape=default` then
`shape=pointer` in the same millisecond, and bar→bar and bar→wallpaper
each log `cursor-shape install shape=default`. (The wallpaper is the
`qdshell-wallpaper` client, not bare desktop.)

So step 1 first enters the bar (an asserted precondition whose remap
lands before the step-1 journal cursor is taken), then moves onto the
wallpaper, whose enter must log a fresh `cursor-shape install
shape=default`. Every attempt, including a retry that starts parked at
(1000, 600), therefore has a real transition.
full-20260930T051422Z-65193 failed 1.1 because its retries moved
wallpaper→same wallpaper point, which can never remap.

```bash
CUR_PRE1=$(compositor_journal_cursor)
qdwin_mouse_move 640 15
# Precondition: entering the bar remaps, and those lines land BEFORE the
# step-1 journal cursor, so none of them can count for the wallpaper move.
noct_wait_cursor_layer_nonzero_alpha "$CUR_PRE1" \
    || { echo "FAIL: entering the bar produced no cursor remap (step 1 precondition)"; exit 1; }
CUR_STEP1=$(compositor_journal_cursor)
qdwin_mouse_move 1000 600
# Bounded journal poll (default 30s), not a fixed sleep, for the SAME
# predicate as assert 1.1: the wallpaper's default-shape install with
# nonzero alpha on one line (a late bar `shape=pointer` remap cannot end the
# wait early). Screenshot stays soft corroboration and still runs if the
# waiter times out; the exit below is the loud failure.
STEP1_SHAPE='cursor-shape install shape=default: mapped on cursor_layer'
noct_wait_cursor_layer_nonzero_alpha "$CUR_STEP1" "" "$STEP1_SHAPE" || step1_cursor_rc=$?
qdwin_screenshot /tmp/04-step1-wallpaper-area.png
[ "${step1_cursor_rc:-0}" -eq 0 ] || exit 1
```

**Assert (1.1) — compositor evidence (load-bearing):** the cursor
sprite is (re)mapped on the cursor plane with non-zero alpha after the
move, by the wallpaper client's default-shape install on pointer enter. (The
sprite's one-time `registered` line is a boot precondition, already
checked once in Setup, and does NOT re-fire on a move.) `virsh
screenshot` cannot capture the hardware cursor PLANE, so assert the
compositor's own journal rather than the screenshot:

```bash
# The wallpaper's default-shape install itself must be visible: shape and
# non-zero alpha counted on the SAME line (a late bar `shape=pointer` remap
# must not stand in for a transparent wallpaper default).
[ "$(cursor_layer_nonzero_alpha_after "$CUR_STEP1" 'cursor-shape install shape=default: mapped on cursor_layer')" -ge 1 ] \
    || { echo "FAIL: no visible wallpaper-enter default-shape install after the move"; exit 1; }
```

What 1.1 proves: the pointer left the bar and entered the wallpaper
client, whose enter installed the default shape. The journal line
carries no coordinates, so it is not a pixel-position proof of
(1000, 600).

The `/tmp/04-step1-wallpaper-area.png` screenshot is kept as soft
corroboration only — a visible arrow near (1000, 600) is a bonus but
NOT required to pass (the hw-cursor plane is invisible to virsh).

### Step 2 — move cursor onto a bar widget (clock area)

```bash
CUR_STEP2=$(compositor_journal_cursor)
qdwin_mouse_move 1130 15
noct_wait_cursor_layer_nonzero_alpha "$CUR_STEP2" || step2_cursor_rc=$?
qdwin_screenshot /tmp/04-step2-clock-hover.png
[ "${step2_cursor_rc:-0}" -eq 0 ] || exit 1
```

**Assert (2.1) — compositor evidence (load-bearing):** the cursor is
still mapped on the cursor plane with non-zero alpha after the move
to the bar (the hover keeps the sprite live):

```bash
[ "$(cursor_layer_nonzero_alpha_after "$CUR_STEP2")" -ge 1 ] \
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
CUR_STEP3=$(compositor_journal_cursor)
for x in 100 300 500 700 900 1100 1260; do
 qdwin_mouse_move "$x" 15
 sleep 0.2
done
noct_wait_cursor_layer_nonzero_alpha "$CUR_STEP3" || step3_cursor_rc=$?
qdwin_screenshot /tmp/04-step3-sweep-end.png
[ "${step3_cursor_rc:-0}" -eq 0 ] || exit 1
```

**Assert (3.1) — compositor evidence (load-bearing):** the cursor
sprite was re-installed on the cursor plane with non-zero alpha during
the sweep (pointer transitions reached the bar; not a position proof,
see Scope limit). Assert the journal, not
the screenshot (the final position (1260, 15) is soft-only):

```bash
[ "$(cursor_layer_nonzero_alpha_after "$CUR_STEP3")" -ge 1 ] \
    || { echo "FAIL: cursor not mapped on cursor_layer during sweep"; exit 1; }
```
**Assert (3.2):** Noctalia is still alive — `noct_session_healthy`.
**Assert (3.3):** weston journal in the last 30s shows zero
protocol errors.

## Cleanup

None. Cursor parked at (1260, 15) is fine.

## Pass criteria

The boot precondition (default cursor sprite registered at session
start) plus the load-bearing compositor-evidence asserts (1.1, 2.1,
3.1: cursor re-mapped on cursor_layer with nonzero_alpha>0 after each
move) plus 3.2/3.3 (session alive, no protocol errors) pass. Each
load-bearing assert is preceded by `noct_wait_cursor_layer_nonzero_alpha`,
which polls that same journal predicate until it holds or
`NOCT_CURSOR_WAIT_S` (default 30s) expires, then fails loud. Screenshot-based
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
