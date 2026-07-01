# 01 — TUI approver renders correctly in a real terminal

**What**: launch `qdistro-admin-tui` inside `qterminal` in the VM's
admin compositor session, inject one pending request, and verify the
TUI's empty and populated states both render with correct colors,
layout, and footer keybindings.

**Why**: tmux text dumps caught some regressions but miss color-only
cues (scope highlight, urgency tint) and cursor placement. This
scenario is the first pixel-level TUI assertion.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-0052}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

# Precondition: broker running, no pending requests, admin compositor session up.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'
$VMEXEC "$VM" 'test -S /run/user/1000/wayland-0 || test -S /run/user/1000/wayland-1'
$VMEXEC "$VM" 'pkill -u admin qterminal 2>/dev/null; true'
```

## Steps

### S1 — launch TUI in qterminal, empty state

Use the repo-supplied launcher; it sets `WAYLAND_DISPLAY`,
`XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS` before exec'ing
qterminal so the child doesn't crash on "Cannot connect to D-Bus
session bus" the way a naive `runuser -u admin -- env DISPLAY=:0`
does. Ships as `/usr/local/bin/qdistro-start-admin-tui` via
`deploy/start-admin-tui.sh`.

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-tui'

sleep 3
$VMGUI "$VM" screenshot /tmp/01-tui-approver-visual-s1-empty.png
```

**Assert (empty):**
- A qterminal window is visible on the admin compositor desktop (chrome with
 "Shell No. 1" titlebar is acceptable).
- Inside the terminal content area, the TUI header reads
 `qdistro admin approvals (TUI)` followed by a pending-count
 indicator (e.g. `(no pen…)`) and a clock `HH:MM:SS`.
- The left pane shows a table header with columns `uid`, `action`,
 `exe` and no data rows.
- The right pane shows the literal text `(no request selected)`.
- The footer shows keybinding chips in order:
 `a Approve d Deny ^r Create Rule shift+^a Approve All shift+^d Deny All
 1 1:once 2 2:1h 3 3:24h 4 ^p palette`.
 OCR may render the modifier glyph as a curly quote or omit the caret;
 assert the labels and ordering rather than exact glyph shape.
 `a`, `d`, the modifier chips, and the digits are highlighted
 (brighter than the labels).

### S2 — inject one pending request, populated state

```bash
B64=$(base64 -w0 <<'EOF'
#!/bin/bash
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
 >/tmp/test-output.txt 2>&1 & echo $! >/tmp/test-pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

sleep 2
$VMGUI "$VM" screenshot /tmp/01-tui-approver-visual-s2-populated.png
```

**Assert (populated):**
- Header pending-count now reads `1 pending` (or similar non-zero
 count) instead of `no pen…`.
- Left pane table has exactly one data row whose `uid` cell reads
 `work` (or `2000`), `action` cell reads `test.action`, and the
 row is highlighted as the current selection (inverse video or
 colored background distinguishing it from the header).
- Right pane shows request detail — at minimum the action name
 `test.action` and the requesting uid/user appear; the literal
 `(no request selected)` is gone.
- Footer keybinding chips are unchanged from S1.

### S3 — deny and confirm empty state returns

```bash
# `vm-gui key d` may silently no-op through XWayland: xdotool
# injects via X but qterminal's focus/input path
# doesn't receive plain-letter keys reliably. Drive via virsh
# send-key (evdev, below X/Wayland). See AGENTS.md .
virsh send-key "$VM" --codeset linux KEY_D
sleep 1
$VMGUI "$VM" screenshot /tmp/01-tui-approver-visual-s3-afterdeny.png
```

**Assert (after deny):**
- Left pane is empty again; right pane returns to
 `(no request selected)`.
- Header pending-count returns to `no pen…` / `0 pending`.
- No error dialog or red banner is visible.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin qterminal 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_tui 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/test-pid /tmp/test-output.txt /tmp/qterminal-tui.log'
```

## Notes for the runner

- If qterminal fails to appear, check `/tmp/qterminal-tui.log` inside
 the VM (`vm-exec $VM 'cat /tmp/qterminal-tui.log'`). The launcher
 sets `DISPLAY`, `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`,
 `DBUS_SESSION_BUS_ADDRESS`; missing any of those in the session
 itself (e.g. admin not logged in via greetd) is the usual culprit.
- The TUI uses Textual's default dark theme; exact RGB values are not
 stable across Textual upgrades. Assertions above intentionally
 talk about *relative* contrast (brighter / inverse / highlighted),
 not specific hex codes.
