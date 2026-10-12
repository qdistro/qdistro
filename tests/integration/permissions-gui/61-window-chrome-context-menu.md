# 61 — server-side window frame: title bar, buttons, edges, menu

<!-- qci:visual: required -->

**What**: qdshell frames exactly the toplevels whose client negotiated
server-side decorations (qdwin v36 `toplevel_decoration`), and the
frame works end to end. On a Qt toplevel (server-side): the title bar +
E/S/W edges attach (`attach_decoration ... inset=N24 E4 S4 W4`); a
left-drag on the title bar moves the window (`request_raise` +
`begin_interactive_move` on a live serial); a right-click opens the
`show_popup` window menu, which an outside click dismisses; the
maximize button maximises (`set_maximized ... max=1`) and a double-click
on the title bar restores; a drag on the bottom-right corner resizes
(`begin_interactive_resize` → `end_interactive_resize`); the menu's
Minimize row minimises; the close button closes a fresh window
(`request_close` + `toplevel_removed`). On a self-decorated client
(weston-terminal): NO shell frame (`mode=client_side`, no
`attach_decoration`) — and its OWN title bar still drags it
(`xdg_toplevel.move`).

**Why**: Qt/KDE apps negotiate server-side decorations and therefore
paint no frame; before this lane they rendered borderless with no
move/close affordance (`issues/qdistro/app-window-decorations.md`,
option A). Self-decorated clients must not get a second title bar
stacked over their own, and their own move/resize requests used to be
compositor stubs. This is also the qdshell side of the D7/D9 GUI lane
(`open-followups.md`): `chrome_button(serial)` → `showPopup(serial)` →
`popup_button` → `popupItemTriggered`. The stale/forged-serial refusal
(`invalid_grab`) and the nested submenu grab remain pinned host-side
(`test_popup_grab_hardening.py`); this lane proves the live path.

**Frame layout** (for computing click points): the title bar is 24 px
tall across the full outer width (window + 4 px edge each side), with
three 24×24 buttons flush right: **minimize** (underscore glyph),
**maximize** (square; overlapping squares = restore), **close** (X).
The window title is drawn left-aligned. The top 4 px of the title bar
are a resize grip — aim title-bar clicks/drags at its vertical middle
(top + 12) and left of the buttons. The E/S/W edges are 4 px wide; the
last 16 px of the bottom edge at either end are the corners.

**Pointer note**: this scenario explicitly exercises pointer input, so
it is exempt from the AGENTS.md "avoid raw coordinate clicks" default.
For EVERY click/drag, first take a `qdwin_screenshot`, compute the
point from the frame, and verify the point with
`vm-gui click-preview x y` (it parks the pointer + rings the target,
no button event). Only then issue `vm-gui click` / `rightclick` /
`doubleclick` / `drag` at the SAME coordinates. Grade every step's
RESULT from the journal oracles named below plus a fresh
`qdwin_screenshot` — never from a click's post frame alone.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}/61
mkdir -p "$ART"
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "$VM"
```

Precondition: qdwin lane (product session owns `wayland-1`, qdshell up).

```bash
$VMEXEC "$VM" 'runuser -u admin -- test -S /run/user/1000/wayland-1' \
    || { echo "ERROR: no wayland-1 — not a qdwin-lane VM"; exit 2; }
$VMEXEC "$VM" 'runuser -u admin -- pgrep -af "[q]s -p" >/dev/null' \
    || { echo "ERROR: qdshell not running"; exit 2; }
```

Keep qdlocker from eating the session mid-scenario (same block as
scenario 18/60):

```bash
$VMEXEC "$VM" 'set -e
    faillock --user admin --reset 2>/dev/null || true
    install -d -m 0755 -o 0 -g 0 /etc/qdistro
    : > /etc/qdistro/locker-ctrl-introspection
    chmod 0644 /etc/qdistro/locker-ctrl-introspection
    install -d -m 0755 -o admin -g users /home/admin/.config/systemd/user/qdlocker.service.d
    cat >/home/admin/.config/systemd/user/qdlocker.service.d/90-ci-gui.conf <<EOF
[Service]
Environment=QDLOCKER_IDLE_MS=86400000
EOF
    chown admin:users /home/admin/.config/systemd/user/qdlocker.service.d/90-ci-gui.conf
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service
    sleep 3'
```

The guest driver waits on the compositor's own journal lines, so it
needs the compositor pid once. `qdwin_journal` greps everything weston
logged for it; `await_qdwin_line <ere>` polls it (~20 s bound).

```bash
$VMEXEC "$VM" 'cat > /tmp/61-driver.sh <<'"'"'EOF'"'"'
#!/bin/bash
set -u
QDPID=$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
    systemctl --user show qdwin-compositor.service -p MainPID --value)
case "$QDPID" in ""|0|*[!0-9]*) echo "ERROR: no compositor MainPID" >&2; exit 2;; esac
qdwin_journal() { journalctl _PID="$QDPID" --no-pager -o cat 2>/dev/null; }
await_qdwin_line() {
    local ere=$1
    for i in $(seq 1 40); do
        qdwin_journal | grep -qE "$ere" && return 0
        sleep 0.5
    done
    qdwin_journal | tail -30 >&2
    return 1
}
"$@"
EOF
    chmod +x /tmp/61-driver.sh'
```

## Steps

### S1 — a server-side client gets the shell frame

Launch the admin app (PyQt6 — Qt negotiates server-side decorations;
the shipped paint-waiting launcher), then wait for the compositor to
log `toplevel_decoration ... mode=server_side` and the frame's
`attach_decoration ... inset=N24 E4 S4 W4` for its handle.

```bash
$VMEXEC "$VM" 'bash /tmp/61-driver.sh true'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh
    APP_PID=$(qdwin_start_admin_app) || exit 2
    echo "$APP_PID" > /tmp/61-app.pid
    H=$(qdwin_window_handle "admin approvals.*" "$APP_PID") || exit 2
    echo "$H" > /tmp/61-app.handle
    echo "window pid=$APP_PID handle=$H"'
$VMEXEC "$VM" 'H=$(cat /tmp/61-app.handle)
    bash /tmp/61-driver.sh await_qdwin_line "toplevel_decoration handle=$H mode=server_side" &&
    bash /tmp/61-driver.sh await_qdwin_line "attach_decoration handle=$H inset=N24 E4 S4 W4"' \
    || { echo "FAIL(S1): no server-side frame for the window"; exit 1; }
qdwin_screenshot "$ART/s1-frame.png"
```

**Assert** (hard, agent-visual): `s1-frame.png` shows, along the TOP
edge of the admin-approvals window, a ~24 px title bar spanning its
full width, carrying the window title on the left and three button
glyphs (underscore, square, X) on the right; and thin (~4 px) edges of
the same colour down both sides and along the bottom. No title bar, a
title bar without text or buttons, or two stacked title bars is a FAIL.

### S2 — left-drag on the title bar raises and moves the window

From `s1-frame.png`, pick a title-bar point left of the buttons at the
bar's vertical middle (`window_left + width/3`, `bar_top + 12`). Verify
the point with `click-preview`, then drag ~120 px right and ~60 px
down.

```bash
qdwin_screenshot "$ART/s2-before-drag.png"
# Runner: compute (X,Y) on the title bar from the frame;
# confirm the red ring sits ON the bar, not on a button:
$VMGUI "$VM" click-preview X Y title-bar
$VMGUI "$VM" drag X Y $((X + 120)) $((Y + 60))
$VMEXEC "$VM" 'H=$(cat /tmp/61-app.handle)
    bash /tmp/61-driver.sh await_qdwin_line "request_raise handle=$H"
    bash /tmp/61-driver.sh await_qdwin_line "begin_interactive_move handle=$H"
    bash /tmp/61-driver.sh await_qdwin_line "end_interactive_move handle=$H"' \
    || { echo "FAIL(S2): drag produced no raise/move/end_move journal lines"; exit 1; }
qdwin_screenshot "$ART/s2-after-drag.png"
```

**Assert** (hard): the three journal lines exist for this handle.
**And** (agent-visual) `s2-after-drag.png` shows the window AND its
frame displaced by roughly the drag delta (~+120 x, ~+60 y) versus
`s2-before-drag.png`. Journal-only is not enough — a move that logged
but never moved pixels is a compositor defect, not a pass.

### S3 — right-click on the title bar opens the context menu

Recompute a title-bar point on `s2-after-drag.png` (the window moved),
verify with `click-preview`, then `rightclick`. The compositor must log
`show_popup parent=<H>` — proof that `chrome_button(serial)` reached
qdshell and `showPopup(serial)` passed the live-grab check.

```bash
# Runner: new title-bar point (X2,Y2) from s2-after-drag.png
$VMGUI "$VM" click-preview X2 Y2 title-bar
$VMGUI "$VM" rightclick X2 Y2
$VMEXEC "$VM" 'H=$(cat /tmp/61-app.handle)
    bash /tmp/61-driver.sh await_qdwin_line "show_popup parent=$H"' \
    || { echo "FAIL(S3): right-click on the title bar produced no show_popup"; exit 1; }
qdwin_screenshot "$ART/s3-menu-open.png"
```

**Assert** (hard, agent-visual): `s3-menu-open.png` shows a small dark
menu anchored at the click point with the rows **Minimize**,
**Maximize** (or **Restore**), **Close**. No menu despite a logged
`show_popup` is a compositing defect; a logged refusal
(`show_popup refused: stale/invalid`) is the serial-contract bug this
lane exists to catch — grade FAIL.

### S4 — an outside click dismisses the menu

Click on the desktop well away from both the menu and the window (e.g.
100 px in from a screen corner that has no window). The compositor's
popup grab must log `popup dismissed by outside click`.

```bash
# Runner: pick an empty-desktop point (X3,Y3) away from menu + window
$VMGUI "$VM" click X3 Y3
$VMEXEC "$VM" 'bash /tmp/61-driver.sh await_qdwin_line "popup dismissed by outside click"' \
    || { echo "FAIL(S4): outside click did not dismiss the menu"; exit 1; }
qdwin_screenshot "$ART/s4-menu-gone.png"
```

**Assert** (hard, agent-visual): `s4-menu-gone.png` shows no menu; the
window is unchanged.

### S5 — maximize button, then double-click on the title bar restores

Click the **maximize** button (the middle of the three, square glyph)
from `s4-menu-gone.png`. qdshell acts on the release over the same
button → `request_maximize` → `set_maximized ... max=1`. Then
double-click the (now full-width) title bar left of the buttons →
`set_maximized ... max=0`.

```bash
# Runner: maximize-button centre (BX,BY) from s4-menu-gone.png
$VMGUI "$VM" click-preview BX BY maximize-button
$VMGUI "$VM" click BX BY
$VMEXEC "$VM" 'H=$(cat /tmp/61-app.handle)
    bash /tmp/61-driver.sh await_qdwin_line "set_maximized handle=$H max=1"' \
    || { echo "FAIL(S5): maximize button did not maximise"; exit 1; }
qdwin_screenshot "$ART/s5-maximized.png"
# Runner: title-bar point (DX,DY) on s5-maximized.png, left of the buttons
$VMGUI "$VM" click-preview DX DY title-bar
$VMGUI "$VM" doubleclick DX DY
$VMEXEC "$VM" 'H=$(cat /tmp/61-app.handle)
    bash /tmp/61-driver.sh await_qdwin_line "set_maximized handle=$H max=0"' \
    || { echo "FAIL(S5): double-click on the title bar did not restore"; exit 1; }
qdwin_screenshot "$ART/s5-restored.png"
```

**Assert** (hard, agent-visual): `s5-maximized.png` shows the window
filling the work area with its title bar at the top, the middle button
now drawn as overlapping squares (restore), and no side/bottom edges.
`s5-restored.png` shows it back at roughly its `s4-menu-gone.png` size
and position, edges back.

### S6 — dragging the bottom-right corner resizes the window

On `s5-restored.png`, take a point on the bottom edge within its last
~8 px from the right (`frame_right - 6`, `bottom_edge_top + 2`) —
the edge is only 4 px tall, so `click-preview` it carefully. Drag it
~100 px right and ~80 px down.

```bash
qdwin_screenshot "$ART/s6-before-resize.png"
# Runner: corner point (RX,RY) from s6-before-resize.png
$VMGUI "$VM" click-preview RX RY bottom-right-corner
$VMGUI "$VM" drag RX RY $((RX + 100)) $((RY + 80))
$VMEXEC "$VM" 'H=$(cat /tmp/61-app.handle)
    bash /tmp/61-driver.sh await_qdwin_line "begin_interactive_resize handle=$H edges=0xa" &&
    bash /tmp/61-driver.sh await_qdwin_line "end_interactive_resize handle=$H"' \
    || { echo "FAIL(S6): corner drag produced no resize-drag journal lines"; exit 1; }
qdwin_screenshot "$ART/s6-after-resize.png"
```

**Assert** (hard): both journal lines exist for this handle
(`edges=0xa` = bottom|right). **And** (agent-visual)
`s6-after-resize.png` shows the window roughly 100 px wider and 80 px
taller than in `s6-before-resize.png`, its top-left corner unmoved, and
the frame (title bar + edges) hugging the new size. If the journal shows
`begin_interactive_resize ... ignored` the press missed the edge —
re-aim once before grading FAIL.

### S7 — the menu's Minimize row minimises the window

Open the menu on the title bar (point from `s6-after-resize.png`), then
click the first row's centre — the Minimize item — computed from the
menu's position in a `s7-menu.png` capture (row height ≈ 1/3 of menu
height; Minimize is the top row). `popup_button` forwards the press;
qdshell's hit-test maps it to row 0 → `requestMinimize`.

```bash
# Runner: title-bar point (X5,Y5) from s6-after-resize.png
$VMGUI "$VM" click-preview X5 Y5 title-bar
$VMGUI "$VM" rightclick X5 Y5
$VMEXEC "$VM" 'H=$(cat /tmp/61-app.handle)
    bash /tmp/61-driver.sh await_qdwin_line "show_popup parent=$H"' \
    || { echo "FAIL(S7): menu did not reopen"; exit 1; }
qdwin_screenshot "$ART/s7-menu.png"
# Runner: compute the Minimize row centre (MX,MY) from s7-menu.png;
# confirm with click-preview, then click.
$VMGUI "$VM" click-preview MX MY minimize-row
$VMGUI "$VM" click MX MY
$VMEXEC "$VM" 'H=$(cat /tmp/61-app.handle)
    bash /tmp/61-driver.sh await_qdwin_line "set_minimized handle=$H"' \
    || { echo "FAIL(S7): Minimize row did not minimise the window"; exit 1; }
qdwin_screenshot "$ART/s7-minimized.png"
```

**Assert** (hard, agent-visual): `s7-minimized.png` shows the admin
window AND its frame gone from the desktop (menu gone too — the item
action tears the popup down). The window must still exist as a
toplevel (minimised, not closed):

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh
    H=$(cat /tmp/61-app.handle)
    qdwin_windows | grep -q "^$H"' \
    || { echo "FAIL(S7): handle vanished instead of minimising"; exit 1; }
```

### S8 — a self-decorated client gets no shell frame and drags itself

Launch `weston-terminal` (native Wayland, draws its own light-grey
title bar, never binds xdg-decoration; the baked base ships it with
weston). The compositor must report `mode=client_side` for it and
qdshell must attach NO frame. Then drag weston-terminal's OWN title bar:
the client's `xdg_toplevel.move` must start a compositor move-drag.

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        WAYLAND_DISPLAY=wayland-1 setsid weston-terminal </dev/null \
        >/tmp/61-csd.log 2>&1 &
    H2=""
    for i in $(seq 1 40); do
        TPID=$(pgrep -u admin -nx weston-terminal) \
            && H2=$(qdwin_window_handle ".*" "$TPID") && break
        sleep 0.5
    done
    [ -n "$H2" ] || { echo "ERROR: weston-terminal window never appeared" >&2; exit 2; }
    echo "$H2" > /tmp/61-csd.handle
    echo "csd window pid=$TPID handle=$H2"'
$VMEXEC "$VM" 'H2=$(cat /tmp/61-csd.handle)
    bash /tmp/61-driver.sh await_qdwin_line "toplevel_decoration handle=$H2 mode=client_side" || exit 1
    sleep 3
    if bash /tmp/61-driver.sh qdwin_journal | grep -q "attach_decoration handle=$H2 "; then
        echo "shell frame attached to a self-decorated client" >&2; exit 1
    fi' \
    || { echo "FAIL(S8): self-decorated client was framed by the shell"; exit 1; }
qdwin_screenshot "$ART/s8-csd.png"
# Runner: a point (TX,TY) on weston-terminal's OWN grey title bar, away
# from its -/[]/x buttons; click-preview it, then drag ~100 px right.
$VMGUI "$VM" click-preview TX TY csd-title-bar
$VMGUI "$VM" drag TX TY $((TX + 100)) $((TY + 40))
$VMEXEC "$VM" 'H2=$(cat /tmp/61-csd.handle)
    bash /tmp/61-driver.sh await_qdwin_line "xdg_toplevel.move handle=$H2 anchor=" &&
    bash /tmp/61-driver.sh await_qdwin_line "end_interactive_move handle=$H2"' \
    || { echo "FAIL(S8): the client's own title bar did not move it"; exit 1; }
qdwin_screenshot "$ART/s8-csd-moved.png"
```

**Assert** (hard, agent-visual): `s8-csd.png` shows weston-terminal
with ONLY its own grey title bar — no dark shell title bar above it and
no shell edges around it. `s8-csd-moved.png` shows it displaced by
roughly the drag delta.

### S9 — the close button closes a fresh server-side window

Launch `qterminator` (Qt, server-side), wait for its frame, click its
**close** button (rightmost, X glyph). qdshell acts on the release →
`request_close` dispatched → the toplevel goes away.

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh
    cd / && setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        WAYLAND_DISPLAY=wayland-1 \
        DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
        qterminator >/tmp/61-qterm.log 2>&1 </dev/null
    H3=""
    for i in $(seq 1 60); do
        QPID=$(pgrep -u admin -nf "[q]terminator") \
            && H3=$(qdwin_window_handle ".*" "$QPID") && break
        sleep 0.5
    done
    [ -n "$H3" ] || { echo "ERROR: qterminator window never appeared" >&2; exit 2; }
    echo "$H3" > /tmp/61-qterm.handle
    echo "qterminator pid=$QPID handle=$H3"'
$VMEXEC "$VM" 'H3=$(cat /tmp/61-qterm.handle)
    bash /tmp/61-driver.sh await_qdwin_line "attach_decoration handle=$H3 inset=N24 E4 S4 W4"' \
    || { echo "FAIL(S9): no frame on qterminator"; exit 1; }
qdwin_screenshot "$ART/s9-qterm.png"
# Runner: close-button centre (CX,CY) — rightmost 24x24 cell of
# qterminator's shell title bar (NOT any tab/strip inside the window).
$VMGUI "$VM" click-preview CX CY close-button
$VMGUI "$VM" click CX CY
$VMEXEC "$VM" 'H3=$(cat /tmp/61-qterm.handle)
    bash /tmp/61-driver.sh await_qdwin_line "request_close handle=$H3 dispatched" &&
    bash /tmp/61-driver.sh await_qdwin_line "toplevel_removed handle=$H3"' \
    || { echo "FAIL(S9): close button did not close the window"; exit 1; }
qdwin_screenshot "$ART/s9-closed.png"
```

**Assert** (hard): `request_close handle=$H3 dispatched` then
`toplevel_removed handle=$H3` in the compositor journal, and
(agent-visual) `s9-closed.png` shows qterminator gone. If qterminator
asks for confirmation instead of closing, the journal's `request_close
... dispatched` alone proves the button; grade the step PASS with that
note and close the dialog.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app.py 2>/dev/null;
    pkill -u admin -x weston-terminal 2>/dev/null;
    pkill -u admin -f "[q]terminator" 2>/dev/null;
    rm -f /tmp/61-driver.sh /tmp/61-app.pid /tmp/61-app.handle /tmp/61-csd.handle /tmp/61-csd.log \
          /tmp/61-qterm.handle /tmp/61-qterm.log'
```

## Report format

Per step: PASS/FAIL with the journal lines cited (copy them verbatim
from `qdwin_journal`) and the artifact path of every frame asserted on.
`visual: required` — every visual verdict must name the opened PNG.
