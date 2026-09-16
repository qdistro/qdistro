# 04 — Alt+Tab moves focus + raises (B3-switcher + auto-focus + raise)

<!-- qci:visual: required -->

**Acceptance criterion:** Alt+Tab on a 2-window setup switches both
keyboard focus and z-order to the other window. The first press
should already swap (no double-press required). Validates: the
qdwin switcher_grab modifier-release path, qdshell switcher_commit
focus + raise calls, qdwin's stack-raise on request_raise, and the
launcher.py initial-selection-anchor at the focused index.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"

pgrep -f "http.server 8765" >/dev/null || (
    cd ${QDWIN_REPO} && \
    python3 -m http.server 8765 --bind 127.0.0.1 >/tmp/qdistro-http.log 2>&1 &
)
sleep 1
qdwin_session_healthy || { echo "FAIL: session not up"; exit 1; }

"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; sleep 1' >/dev/null

# Spawn 2 foots cascaded.
for i in 1 2; do
    qdwin_ctrl "launcher-toggle" >/dev/null
    qdwin_ctrl "launcher-type foot" >/dev/null
    qdwin_ctrl "launcher-activate" >/dev/null
    sleep 1.5
done
```

## Steps

### Step 1 — verify two foots exist, focused on most-recent

```bash
qdwin_ctrl "list"
qdwin_ctrl "focus"
```

**Assert (1.1):** `list` reports two `tl` lines.
**Assert (1.2):** `focus` reports `handle=2` (the second-spawned
foot, since auto-focus on map landed it there).

### Step 2 — type into focused foot to label it

```bash
qdwin_type_lower "echo i am foot two"
qdwin_send_key KEY_ENTER
sleep 1
qdwin_screenshot /tmp/04-step2-foot2-labelled.png
```

**Assert (2.1):** screenshot shows `i am foot two` echoed in the
foreground foot. The other foot is partially visible behind (cascade
offset ~40px).

### Step 3 — Alt+Tab should swap focus AND raise the other foot

```bash
qdwin_chord alt -- tab
sleep 0.5
qdwin_ctrl "focus"
qdwin_screenshot /tmp/04-step3-after-alttab.png
```

**Assert (3.1):** `focus` reports `handle=1` (was 2). Single Alt+Tab
moved focus to the OTHER foot — confirms launcher.py
switcher_cycle anchor fix (selection starts at focused index).
**Assert (3.2):** screenshot shows foot1 in the foreground (the one
WITHOUT `i am foot two`) — confirms qdwin's stack-raise in
request_raise is doing actual layer reordering.

### Step 3.5 — type into newly-focused foot to label it

```bash
qdwin_type_lower "echo i am foot one"
qdwin_send_key KEY_ENTER
sleep 1
qdwin_screenshot /tmp/04-step3b-foot1-labelled.png
```

**Assert (3.5.1):** screenshot shows `i am foot one` in the foreground
foot (foot1) — text routes to the now-focused window, not still
to foot2.

### Step 4 — Alt+Tab again returns to foot2

```bash
qdwin_chord alt -- tab
sleep 0.5
qdwin_ctrl "focus"
qdwin_screenshot /tmp/04-step4-back-foot2.png
```

**Assert (4.1):** `focus` reports `handle=2`.
**Assert (4.2):** screenshot shows foot2 (with `i am foot two`) in
the foreground.

### Step 5 — exit foot2; focus auto-falls-back to foot1

```bash
qdwin_type_lower "exit"
qdwin_send_key KEY_ENTER
sleep 1.5
qdwin_ctrl "list"
qdwin_ctrl "focus"
qdwin_type_lower "echo only foot one alive"
qdwin_send_key KEY_ENTER
sleep 1
qdwin_screenshot /tmp/04-step5-after-exit.png
```

**Assert (5.1):** `list` reports only one `tl` line (foot2 is gone).
**Assert (5.2):** `focus` reports `handle=1` — auto-refocus on close
worked (regression guard against the dangling-focus bug).
**Assert (5.3):** screenshot shows `only foot one alive` echoed in
foot1 — keyboard reaches it without an explicit re-focus.

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; true' >/dev/null
```

## Pass criteria

All asserts 1.1 → 5.3 pass. Confirms:
- B3-switcher: Alt+Tab fires switcher_commit via the keyboard grab's
  modifier-release callback (not the broken weston modifier_binding).
- launcher.py switcher_cycle initial-selection anchor: starts at
  focused index so first cycle moves AWAY.
- qdshell switcher_commit calls set_keyboard_focus_v2 after raise.
- qdwin request_raise actually re-stacks the view (not the no-op it
  used to be for non-minimised toplevels).
- qdshell on_toplevel_removed picks the most-recent surviving
  toplevel and focuses it.

## Known-broken-if

- Step 3 focus stays on handle=2 → switcher anchor regression. Check
  that switcher_cycle starts `sw.selection = focused_idx` BEFORE the
  `(sw.selection + direction) % len(...)` advance.
- Step 3 focus moves but step 3.5 text appears in foot2 not foot1 →
  switcher_commit didn't call set_keyboard_focus_v2. Check
  `qdshell/Modules/Launcher (QML)` switcher_commit().
- Step 3 focus moves but visual front stays on foot2 → request_raise
  is a no-op again. Check `qdwin/qdwin.c`
  qdwin_handle_request_raise — must call qdwin_toplevel_move_to_layer
  even for non-minimised toplevels.
- Step 5 keyboard goes nowhere after exit → on_toplevel_removed
  doesn't refocus. Check qdshell.py on_toplevel_removed.
