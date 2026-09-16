# 10 — context menu maximise/restore label flips with state

<!-- qci:visual: required -->

**Acceptance criterion:** the qdshell context-menu row that
toggles the MAXIMIZED bit shows "Maximise" when the window is at
its non-maximised cascade size and "Restore" when it's filling the
work area. The label flips on every state change — the menu is
not painted once and cached.

This validates `popup_items_for(tl)` in `qdshell.py` and ensures
a future refactor doesn't accidentally hard-code the label or
compute it once at popup-create time before the state is known.

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

# Right-click anchor (well clear of the right-edge buttons) and
# popup-row centres relative to it.
RC_X=200; RC_Y=14
ITEM_X=$((RC_X + 100))
ITEM_TOGGLE_Y=$((RC_Y + 56 + 14))   # row 2 centre
```

## Steps

### Step 1 — open menu in non-maximised state, expect "Maximise"

```bash
qdwin_ctrl "list"   # capture handle as $H — non-maximised at first
qdwin_click "$RC_X" "$RC_Y" right
sleep 0.5
qdwin_screenshot /tmp/10-step1-popup-restored.png
qdwin_send_key KEY_ESC   # dismiss the menu without acting
sleep 0.3
```

**Assert (1.1):** screenshot shows a popup with row 2 labelled
**"Maximise"** (not "Restore", not "Toggle maximised").

### Step 2 — maximise via ctrl-socket, open menu again, expect "Restore"

```bash
qdwin_ctrl "max $H"
sleep 0.5
qdwin_click "$RC_X" "$RC_Y" right
sleep 0.5
qdwin_screenshot /tmp/10-step2-popup-maximised.png
qdwin_send_key KEY_ESC
sleep 0.3
```

**Assert (2.1):** screenshot shows a popup with row 2 labelled
**"Restore"** (the label flipped to reflect the new state).

### Step 3 — click "Restore" via popup, then open menu again, expect "Maximise" again

```bash
qdwin_click "$RC_X" "$RC_Y" right
sleep 0.5
qdwin_click "$ITEM_X" "$ITEM_TOGGLE_Y"     # click Restore
sleep 0.7
qdwin_ctrl "state $H"
qdwin_click "$RC_X" "$RC_Y" right          # re-open menu
sleep 0.5
qdwin_screenshot /tmp/10-step3-popup-after-restore.png
qdwin_send_key KEY_ESC
sleep 0.3
```

**Assert (3.1):** `state & 0x01 == 0` — Restore via popup
un-maximised the window.
**Assert (3.2):** screenshot shows a popup with row 2 labelled
**"Maximise"** again (label flipped back).

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; true' >/dev/null
```

## Pass criteria

All three label-flip asserts pass: "Maximise" → "Restore" →
"Maximise". This proves the label is recomputed from the current
`tl.state` on every menu open, not cached.

## Known-broken-if

- Label is always "Toggle maximised": qdshell on a pre-v21
  POPUP_ITEMS list. Re-deploy.
- Label is always "Maximise" or always "Restore": the toggle
  branch in `popup_items_for` is reading the wrong bit, or the
  `tl.state` update from `toplevel_state` events lags the popup
  open. Inspect qdshell.log for `toplevel_state handle=$H bits=0x1`
  before each popup open.
