# 09 — titlebar right-click context menu via mouse

<!-- qci:visual: required -->

**Acceptance criterion:** right-clicking the titlebar of a foot
toplevel opens the qdshell context menu, and clicking a menu item
(e.g. "Toggle maximised", "Minimise", "Close") fires the matching
action on the toplevel.

This validates two paths:
1. **chrome_button right-click → show_popup** — the qdwin_shell_v1@v20
   typed-event path delivers `BTN_RIGHT (0x111)` clicks on the
   titlebar to qdshell, which calls `show_context_menu` →
   `shell.show_popup`.
2. **popup_button → popup_dispatch_action** — the qdwin_shell_v1@v21
   typed-event path delivers left-clicks on popup surfaces. Same
   bypass-libweston-same-client trick as chrome_button, applied to
   the qdwin_popup_v1 surface. Without v21 the popup opens but
   menu items are inert.

The popup also has **state-aware labels**: the maximise/restore
row shows "Maximise" when the window is at its non-maximised
cascade size and "Restore" when it's filling the work area. The
label flip is driven by `tl.state & 1` (the MAXIMIZED bit) inside
`popup_items_for`.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"

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

POPUP geometry (qdshell.py lines 1770-1785):
- popup width = 200 px, item height = 28 px, separator = 6 px
- items ordered: `Close (0)`, `Minimise (1)`, `Toggle maximised (2)`,
  `[separator] (3)`, `Window info (4)`

Popup is anchored at the chrome-local right-click position. For a
maximised toplevel (work area starts at output (0, 0)), titlebar is
at y=0..28; right-clicking at (x_titlebar, y_titlebar) opens a popup
whose top-left lands at the same global coords. So:

```bash
# Right-click target — middle of titlebar, comfortably away from
# the right-edge buttons.
RC_X=200
RC_Y=14

# Popup item centres in screen coords (popup top-left = right-click pos).
ITEM_X=$((RC_X + 100))                # popup centre column
ITEM_CLOSE_Y=$((RC_Y + 14))           # row 0 centre
ITEM_MIN_Y=$((RC_Y + 28 + 14))        # row 1 centre
ITEM_TOGGLE_MAX_Y=$((RC_Y + 56 + 14)) # row 2 centre
```

## Steps

### Step 1 — capture handle, maximise for known geometry

```bash
qdwin_ctrl "list"   # capture handle as $H
qdwin_ctrl "max $H"
sleep 0.5
qdwin_screenshot /tmp/09-step1-baseline.png
```

**Assert (1.1):** `list` returns one tl line.
**Assert (1.2):** screenshot shows maximised foot with titlebar at
the top and panel at the bottom.

### Step 2 — right-click titlebar to open the context menu

```bash
qdwin_click "$RC_X" "$RC_Y" right
sleep 0.5
qdwin_screenshot /tmp/09-step2-popup-open.png
```

**Assert (2.1):** the screenshot shows a 200×118 px context-menu
popup anchored near `(RC_X, RC_Y)` — visible items "Close",
"Minimise", **"Restore"** (because the window was maximised in
step 1; the label is state-aware), a thin separator, "Window info".
**Assert (2.2):** qdshell.log shows
`context menu handle=$H at (RC_X, RC_Y) size=200x...`.

### Step 3 — left-click "Restore" inside the popup

```bash
qdwin_click "$ITEM_X" "$ITEM_TOGGLE_MAX_Y"
sleep 0.7
qdwin_ctrl "state $H"
qdwin_screenshot /tmp/09-step3-after-toggle-max.png
```

**Assert (3.1):** `state & 0x01 == 0` — the toplevel was un-
maximised by the popup action.
**Assert (3.2):** screenshot shows foot back at non-maximised
size; popup is gone.
**Assert (3.3):** qdshell.log contains
`popup_button → handle=$H action=toggle_max` (proves the v21 path,
not the legacy wl_pointer path).

### Step 4 — re-open menu, click "Minimise"

```bash
qdwin_ctrl "max $H"           # restore deterministic geometry
sleep 0.5
qdwin_click "$RC_X" "$RC_Y" right
sleep 0.5
qdwin_click "$ITEM_X" "$ITEM_MIN_Y"
sleep 0.7
qdwin_ctrl "state $H"
qdwin_screenshot /tmp/09-step4-after-minimise.png
```

**Assert (4.1):** `state & 0x04 == 0x04` (MINIMIZED set).
**Assert (4.2):** screenshot shows wallpaper + panel only.

### Step 5 — re-raise, re-open menu, click "Close"

```bash
qdwin_ctrl "raise $H" >/dev/null
sleep 0.3
qdwin_ctrl "max $H"
sleep 0.5
qdwin_click "$RC_X" "$RC_Y" right
sleep 0.5
qdwin_click "$ITEM_X" "$ITEM_CLOSE_Y"
sleep 1.5
qdwin_ctrl "list"
qdwin_screenshot /tmp/09-step5-after-close.png
```

**Assert (5.1):** `list` no longer contains `$H`.
**Assert (5.2):** screenshot shows wallpaper + panel only.

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; true' >/dev/null
```

## Pass criteria

All asserts 1.1 → 5.2 pass. Assert 2.1's "Restore" label and
3.3's `popup_button → ... action=toggle_max` log line are the v21
fingerprints — pre-v21 builds would show "Toggle maximised" and
no popup_button line.

## Known-broken-if

- Step 2 popup never paints: check qdwin.log for the
  `qdwin_shell_v1_send_chrome_button` path firing on btn=0x111 —
  its absence means right-click isn't reaching qdwin's grab
  handler. (Most likely the `right` arg to qdwin_click isn't
  translating to `BTN_RIGHT`; double-check helper.)
- Popup paints but is at wrong screen position: anchoring math is
  off in `qdwin_handle_show_popup` or `show_context_menu`. Less
  critical than action dispatch.
- Popup opens, click on item lands on the popup, but qdshell.log
  shows no `popup_button →` line: the v21 binding wasn't
  negotiated (compositor advertising < 21 OR shell capping at < 21
  in `on_global`). Verify with
  `grep "bound qdwin_shell_v1" ~/.local/share/qdshell.log`.
- Label says "Toggle maximised" instead of "Restore"/"Maximise":
  qdshell.py is on the pre-v21 POPUP_ITEMS list. Re-deploy the
  installed `/usr/share/qdshell/qdshell.py`.
