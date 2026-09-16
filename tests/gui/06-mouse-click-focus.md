# 06 — mouse click-to-focus + raise

<!-- qci:visual: required -->

**Acceptance criterion:** clicking on a background window's chrome
or content area focuses AND raises that window. Validates qdwin's
pointer button handler now does click-to-focus (was a passthrough
no-op). Pointer enter delivery (cursor-shape requests, hover) was
already working — this scenario covers the ACT of clicking.

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

# Two cascaded foots — second is auto-focused, first is behind.
for i in 1 2; do
    qdwin_ctrl "launcher-toggle" >/dev/null
    qdwin_ctrl "launcher-type foot" >/dev/null
    qdwin_ctrl "launcher-activate" >/dev/null
    sleep 1.5
done
```

## Steps

### Step 1 — verify foot2 is on top + focused

```bash
qdwin_ctrl "list"
qdwin_ctrl "focus"
qdwin_screenshot /tmp/06-step1-baseline.png
```

**Assert (1.1):** `list` reports two `tl` lines.
**Assert (1.2):** `focus` reports the second-spawned handle (e.g.
`handle=2`).
**Assert (1.3):** screenshot shows foot2 in front (cascaded ~40px
right + down from foot1).

### Step 2 — type a label into focused foot2

```bash
qdwin_type_lower "i am foot two"
qdwin_send_key KEY_ENTER
sleep 1
qdwin_screenshot /tmp/06-step2-foot2-labelled.png
```

**Assert (2.1):** screenshot shows `i am foot two` echoed in the
foreground foot.

### Step 3 — click on foot1's titlebar (background window)

```bash
# foot1 spawns first → cascaded at offset 0 → titlebar around (290, 145).
qdwin_click 295 145
sleep 0.5
qdwin_ctrl "focus"
qdwin_screenshot /tmp/06-step3-after-click.png
```

**Assert (3.1):** `focus` reports the FIRST-spawned handle (one less
than baseline). Click moved keyboard focus to foot1.
**Assert (3.2):** screenshot shows foot1 raised to the foreground
(was behind in step 1; now visibly on top).

### Step 4 — type into foot1; text should land there

```bash
qdwin_type_lower "i am foot one"
qdwin_send_key KEY_ENTER
sleep 1
qdwin_screenshot /tmp/06-step4-foot1-labelled.png
```

**Assert (4.1):** screenshot shows `i am foot one` typed at foot1's
prompt + the bash error `i: command not found` (or similar — `i` is
not a command). The point is the text routes to foot1, not foot2.

### Step 5 — click back on foot2 (now partially visible behind foot1)

```bash
# foot2 chrome at offset 40 → titlebar around (335, 185).
qdwin_click 335 185
sleep 0.5
qdwin_ctrl "focus"
qdwin_screenshot /tmp/06-step5-back-to-foot2.png
```

**Assert (5.1):** `focus` reports the second-spawned handle again.
**Assert (5.2):** screenshot shows foot2 in front, with the original
`i am foot two` history visible.

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; true' >/dev/null
```

## Pass criteria

All asserts 1.1 → 5.2 pass. Confirms qdwin's pointer button handler
now (a) finds the toplevel under the cursor (content view OR any
chrome side), (b) raises it on the normal layer, (c) sets keyboard
focus, (d) emits seat_focus_changed for the shell.

## Known-broken-if

- Step 3 focus stays on the original handle → click-to-focus
  regression. Check `qdwin_proxy_default_grab_button` in
  `qdwin/qdwin.c` for the `state == PRESSED && button ==
  BTN_LEFT` branch with `qdwin_toplevel_for_view`.
- Step 3 focus moves but visual doesn't (foot1 stays behind) →
  request_raise re-stack regression. Same handler must call
  `qdwin_toplevel_move_to_layer(tl, &normal_layer)`.
- No mouse cursor visible in virt-viewer at all → SPICE viewer-side
  issue (B6); doesn't affect this test since QMP `mouse_move`
  injects events at the QEMU input layer below SPICE. Real cursor
  visibility for human users needs SPICE channel debugging — out of
  scope here.

## Coordinate notes

The cascade offset is 40px between successive toplevels (mod 200),
so on a fresh session with 2 foots:
- foot1 chrome roughly: (290, 130) → (1070, 700)
- foot2 chrome roughly: (330, 170) → (1110, 740)

Titlebars are 28px tall by default; chrome border is 8px. Clicking
the centre of a titlebar (e.g. 600, 145 for foot1, 600, 185 for
foot2) is robust regardless of theme padding.
