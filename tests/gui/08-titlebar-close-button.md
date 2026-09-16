# 08 — titlebar close button via mouse

<!-- qci:visual: required -->

**Acceptance criterion:** clicking the red × close button on the
titlebar of a foot toplevel destroys the toplevel — the wl_surface
goes away, qdwin emits `toplevel_removed`, and qdshell drops the tl
from its `list`.

Companion to scenario 07 (max/min/restore). Same chrome_button
dispatch path (qdwin_shell_v1@v20), different action.

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

# Same deterministic-geometry trick as scenario 07: maximise via
# ctrl-socket so button positions are a known function of screen
# size.
MAX_CLOSE_X=$((QDWIN_SCREEN_W - 17))
MAX_BTN_Y=14
```

## Steps

### Step 1 — capture handle, maximise, baseline screenshot

```bash
qdwin_ctrl "list"   # capture handle as $H
qdwin_ctrl "max $H"
sleep 0.5
qdwin_screenshot /tmp/08-step1-baseline.png
```

**Assert (1.1):** `list` returns exactly one `tl` line.
**Assert (1.2):** the screenshot shows a maximised foot with three
visible glyphs on the titlebar; the rightmost is a red × on a
distinctly red square (the close button background).

### Step 2 — click close button

```bash
qdwin_click "$MAX_CLOSE_X" "$MAX_BTN_Y"
# Closing is asynchronous: qdshell calls request_close, qdwin sends
# xdg_toplevel.close, foot exits, qdwin emits toplevel_removed.
# 1 second is enough on baseweed clones; bump on slower VMs.
sleep 1.5
qdwin_ctrl "list"
qdwin_screenshot /tmp/08-step2-after-close.png
```

**Assert (2.1):** `list` no longer contains a `tl` line for `$H`
(usually `list` returns just `ok list` with no preceding `tl`).
**Assert (2.2):** screenshot shows wallpaper + panel only — no foot
chrome, no foot content.

If 2.1 fails (foot still in list with the same handle), inspect
qdshell.log for `click → request_close handle=$H` — its absence
means the chrome_button event didn't reach hit_test_button (most
likely the click landed off-button by ±1 px because the integer
math truncated incorrectly; bump `MAX_CLOSE_X` by ±2 to verify).

### Step 3 — close again from restored state

To make sure the close path also works without the maximise dance:

```bash
qdwin_ctrl "launcher-toggle" >/dev/null
qdwin_ctrl "launcher-type foot" >/dev/null
qdwin_ctrl "launcher-activate" >/dev/null
sleep 2
qdwin_ctrl "list"   # capture new $H2
qdwin_ctrl "max $H2"     # maximise again to fix coords
sleep 0.5
qdwin_click "$MAX_CLOSE_X" "$MAX_BTN_Y"
sleep 1.5
qdwin_ctrl "list"
qdwin_screenshot /tmp/08-step3-second-close.png
```

**Assert (3.1):** the second `list` again has no `tl` line for
`$H2`.
**Assert (3.2):** the chrome_button dispatch is repeatable across
fresh toplevel handles, not a one-shot.

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; true' >/dev/null
```

## Pass criteria

All asserts 1.1 → 3.2 pass.

## Known-broken-if

- foot stays alive after the click but `state` shows the URGENT bit
  set: foot is asking for confirmation (some foot builds prompt
  before exit). Not a bug in chrome_button — re-test with `bash` or
  another simpler client.
- Click registers in qdshell.log as `click → request_close`, but
  foot doesn't go away: bug is in the qdwin compositor side
  `qdwin_handle_request_close` or in the
  `weston_desktop_surface_close_request` path. Out of scope for
  this scenario — the chrome_button path is innocent.
