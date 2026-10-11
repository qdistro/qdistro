# 61 — window chrome strip: drag, right-click context menu, item actions

<!-- qci:visual: required -->

**What**: on a plain admin toplevel, prove the shell-attached north
chrome strip exists (visual + `attach_decoration` journal), that a
left-drag on it drives `request_raise` + `begin_interactive_move` on a
live input serial and actually moves the window, that a right-click on
it opens the `show_popup` context menu (`show_popup parent=` journal +
visible Minimize/Maximize/Close rows), that an outside click dismisses
the menu (`popup dismissed by outside click`), that the Minimize row
minimises the window (`set_minimized`), and that the Close row closes a
window (`request_close` + `toplevel_removed`).

**Why**: this is the qdshell side of the D7/D9 GUI lane
(`open-followups.md`). The `chrome_button`/`popup_button` wire events
and the `show_popup` grab-serial contract had zero live callers before
the shell-chrome milestone — every step here is a code path that did
not exist before: `chrome_button(serial)` → `request_raise` +
`begin_interactive_move(serial)` for the drag, and
`chrome_button(serial)` → `showPopup(serial)` → `popup_button` →
`popupItemTriggered` for the menu. The stale/forged-serial refusal
(`invalid_grab`) and the nested submenu grab remain pinned host-side
(`test_popup_grab_hardening.py`); this lane proves the live path.

**Pointer note**: this scenario explicitly exercises pointer input, so
it is exempt from the AGENTS.md "avoid raw coordinate clicks" default.
For EVERY click/drag, first take a `qdwin_screenshot`, compute the
point from the frame, and verify the point with
`vm-gui click-preview x y` (it parks the pointer + rings the target,
no button event). Only then issue `vm-gui click` / `rightclick` /
`drag` at the SAME coordinates. Grade every step's RESULT from the
journal oracles named below plus a fresh `qdwin_screenshot` — never
from a click's post frame alone.

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

### S1 — the north chrome strip attaches to a plain toplevel

Launch the admin app (the shipped paint-waiting launcher), then wait
for the compositor to log `attach_decoration ... inset=N20` for its
handle — the 20 px north strip Qdwin.qml attaches once it learns the
window width.

```bash
$VMEXEC "$VM" 'bash /tmp/61-driver.sh true'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh
    APP_PID=$(qdwin_start_admin_app) || exit 2
    echo "$APP_PID" > /tmp/61-app.pid
    H=$(qdwin_window_handle "admin approvals.*" "$APP_PID") || exit 2
    echo "$H" > /tmp/61-app.handle
    echo "window pid=$APP_PID handle=$H"'
$VMEXEC "$VM" 'H=$(cat /tmp/61-app.handle)
    bash /tmp/61-driver.sh await_qdwin_line "attach_decoration handle=$H inset=N20"' \
    || { echo "FAIL(S1): no attach_decoration inset=N20 for the window"; exit 1; }
qdwin_screenshot "$ART/s1-chrome-strip.png"
```

**Assert** (hard, agent-visual): `s1-chrome-strip.png` shows a solid
~20 px horizontal strip spanning the full width of the admin-approvals
window along its TOP edge, visibly distinct from the window content
below it. No strip / a strip that is part of the client's own painting
(a normal titlebar INSIDE the window) is a FAIL — the strip is a
shell-attached surface above the client content.

### S2 — left-drag on the strip raises and moves the window

From `s1-chrome-strip.png`, compute the strip's midpoint
(`window_left + width/2`, `strip_top + 10`). Verify the point with
`click-preview`, then drag ~120 px right and ~60 px down.

```bash
qdwin_screenshot "$ART/s2-before-drag.png"
# Runner: compute (X,Y) on the strip midpoint from the frame;
# confirm the red ring sits ON the strip:
$VMGUI "$VM" click-preview X Y strip-midpoint
$VMGUI "$VM" drag X Y $((X + 120)) $((Y + 60))
$VMEXEC "$VM" 'H=$(cat /tmp/61-app.handle)
    bash /tmp/61-driver.sh await_qdwin_line "request_raise handle=$H"
    bash /tmp/61-driver.sh await_qdwin_line "begin_interactive_move handle=$H"
    bash /tmp/61-driver.sh await_qdwin_line "end_interactive_move handle=$H"' \
    || { echo "FAIL(S2): drag produced no raise/move/end_move journal lines"; exit 1; }
qdwin_screenshot "$ART/s2-after-drag.png"
```

**Assert** (hard): the three journal lines exist for this handle
(`chrome_button` forwarded the live serial → qdshell issued
`request_raise` + `begin_interactive_move`; the compositor ran the
move-drag to its release). **And** (agent-visual)
`s2-after-drag.png` shows the window displaced by roughly the drag
delta (~+120 x, ~+60 y) versus `s2-before-drag.png`. Journal-only is
not enough — a move that logged but never moved pixels is a compositor
defect, not a pass.

### S3 — right-click on the strip opens the context menu

Recompute the strip midpoint on `s2-after-drag.png` (the window moved),
verify with `click-preview`, then `rightclick`. The compositor must log
`show_popup parent=<H>` — proof that `chrome_button(serial)` reached
qdshell and `showPopup(serial)` passed the live-grab check.

```bash
# Runner: new strip midpoint (X2,Y2) from s2-after-drag.png
$VMGUI "$VM" click-preview X2 Y2 strip-midpoint
$VMGUI "$VM" rightclick X2 Y2
$VMEXEC "$VM" 'H=$(cat /tmp/61-app.handle)
    bash /tmp/61-driver.sh await_qdwin_line "show_popup parent=$H"' \
    || { echo "FAIL(S3): right-click on the strip produced no show_popup"; exit 1; }
qdwin_screenshot "$ART/s3-menu-open.png"
```

**Assert** (hard, agent-visual): `s3-menu-open.png` shows a small dark
menu anchored at the click point with at least the rows **Minimize**,
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

### S5 — the Minimize row minimises the window

Reopen the menu (strip midpoint on `s4-menu-gone.png`), then click the
first row's centre — the Minimize item — computed from the menu's
position in a `s5-menu.png` capture (row height ≈ 1/3 of menu height;
Minimize is the top row). `popup_button` forwards the press; qdshell's
hit-test maps it to row 0 → `requestMinimize`.

```bash
$VMGUI "$VM" click-preview X2 Y2 strip-midpoint
$VMGUI "$VM" rightclick X2 Y2
$VMEXEC "$VM" 'H=$(cat /tmp/61-app.handle)
    bash /tmp/61-driver.sh await_qdwin_line "show_popup parent=$H"' \
    || { echo "FAIL(S5): menu did not reopen"; exit 1; }
qdwin_screenshot "$ART/s5-menu.png"
# Runner: compute the Minimize row centre (MX,MY) from s5-menu.png;
# confirm with click-preview, then click.
$VMGUI "$VM" click-preview MX MY minimize-row
$VMGUI "$VM" click MX MY
$VMEXEC "$VM" 'H=$(cat /tmp/61-app.handle)
    bash /tmp/61-driver.sh await_qdwin_line "set_minimized handle=$H"' \
    || { echo "FAIL(S5): Minimize row did not minimise the window"; exit 1; }
qdwin_screenshot "$ART/s5-minimized.png"
```

**Assert** (hard, agent-visual): `s5-minimized.png` shows the admin
window AND its chrome strip gone from the desktop (menu gone too —
the item action tears the popup down). The window must still exist as
a toplevel (minimised, not closed):

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh
    H=$(cat /tmp/61-app.handle)
    qdwin_windows | grep -q "^$H"' \
    || { echo "FAIL(S5): handle vanished instead of minimising"; exit 1; }
```

### S6 — the Close row closes a fresh window

Launch `weston-terminal` (a second, independent native-Wayland
toplevel; the baked base ships it with weston — the XWayland-backed
admin TUI launcher is an opt-in lane), wait for its chrome strip, reopen its menu, and
click the **Close** row (bottom row). `request_close` must be
dispatched and the toplevel removed — the whole chrome/popup state for
the handle dies with it.

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        WAYLAND_DISPLAY=wayland-1 setsid weston-terminal </dev/null \
        >/tmp/61-tui.log 2>&1 &
    H2=""
    for i in $(seq 1 40); do
        TPID=$(pgrep -u admin -nx weston-terminal) \
            && H2=$(qdwin_window_handle ".*" "$TPID") && break
        sleep 0.5
    done
    [ -n "$H2" ] || { echo "ERROR: weston-terminal window never appeared" >&2; exit 2; }
    echo "$H2" > /tmp/61-tui.handle
    echo "second window pid=$TPID handle=$H2"'
$VMEXEC "$VM" 'H2=$(cat /tmp/61-tui.handle)
    bash /tmp/61-driver.sh await_qdwin_line "attach_decoration handle=$H2 inset=N20"' \
    || { echo "FAIL(S6): no chrome strip on the second window"; exit 1; }
qdwin_screenshot "$ART/s6-tui.png"
# Runner: second-window strip midpoint (X4,Y4) from s6-tui.png
$VMGUI "$VM" click-preview X4 Y4 tui-strip
$VMGUI "$VM" rightclick X4 Y4
$VMEXEC "$VM" 'H2=$(cat /tmp/61-tui.handle)
    bash /tmp/61-driver.sh await_qdwin_line "show_popup parent=$H2"' \
    || { echo "FAIL(S6): no show_popup for the second window"; exit 1; }
qdwin_screenshot "$ART/s6-menu.png"
# Runner: Close row centre (CX,CY) — bottom row of the menu.
$VMGUI "$VM" click-preview CX CY close-row
$VMGUI "$VM" click CX CY
$VMEXEC "$VM" 'H2=$(cat /tmp/61-tui.handle)
    bash /tmp/61-driver.sh await_qdwin_line "request_close handle=$H2"
    bash /tmp/61-driver.sh await_qdwin_line "toplevel_removed handle=$H2"' \
    || { echo "FAIL(S6): Close row did not close the window"; exit 1; }
qdwin_screenshot "$ART/s6-closed.png"
```

**Assert** (hard): `request_close handle=$H2` then
`toplevel_removed handle=$H2` in the compositor journal, and
(agent-visual) `s6-closed.png` shows the second window gone.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app.py 2>/dev/null;
    pkill -u admin -x weston-terminal 2>/dev/null;
    rm -f /tmp/61-driver.sh /tmp/61-app.pid /tmp/61-app.handle /tmp/61-tui.handle /tmp/61-tui.log'
```

## Report format

Per step: PASS/FAIL with the journal lines cited (copy them verbatim
from `qdwin_journal`) and the artifact path of every frame asserted on.
`visual: required` — every visual verdict must name the opened PNG.
