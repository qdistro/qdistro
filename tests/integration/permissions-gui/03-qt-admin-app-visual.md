# 03 — Qt admin approval app renders master/detail correctly

**What**: launch the PyQt admin app in admin's compositor session,
inject one pending request, verify the master (list) / detail (form)
layout renders with correct empty and populated states, and confirm
Deny returns the pane to empty.

**Why**: the Qt app is the primary approver (TUI is the
terminal companion). Its pixel layout — list on the left, detail on
the right with scope radio buttons + Approve/Deny — is the authority
for what "done" looks like on tty3. Tests pass the model but can't
see that the radio group rendered or the Approve button is focused.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-0052}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'
$VMEXEC "$VM" 'test -S /run/user/1000/wayland-0 || test -S /run/user/1000/wayland-1'
# Clean slate: no stray admin app, no stray qterminal+TUI, no stray test.
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin qterminal 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_tui 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'

# Drain broker state — a prior scenario may have left a stale pending
# request, which would falsify the S1 empty-state assertion.
# Restarting the service empties the in-memory queue.
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1
```

## Steps

### S1 — launch admin app, empty state

`qdistro-start-admin-app` (installed to `/usr/local/bin` by
bootstrap) is the admin-runnable launcher; it sets Wayland/X env vars
and detaches via `setsid`.

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
$VMGUI "$VM" screenshot /tmp/03-qt-admin-app-visual-s1-empty.png
```

**Assert (empty):**
- A window with titlebar text `admin approvals` is visible on the
 admin compositor desktop.
- The window content is split horizontally: a left-side list view
 (empty — no items) and a right-side detail pane.
- The detail pane's user label reads literally `(no selection)`.
- The detail pane shows the scope group: a "Scope:" header followed
 by eight radio buttons with labels, in order:
 `Just this once`, `1 hour`, `24 hours`, `Forever, any command`,
 `Forever, only this exact program`, `Forever, only this exact argv
 tuple`, `Forever, this argv basename anywhere`, `Forever, this argv
 prefix + any trailing args`. The first radio (`Just this once`) is
 selected.
- Two buttons labeled `Approve` and `Deny` are visible below the
 scope group.

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
$VMGUI "$VM" screenshot /tmp/03-qt-admin-app-visual-s2-populated.png
```

**Assert (populated):**
- Left list view now has one row referencing `work` (or uid `2000`)
 and `test.action`. The row is selected (highlighted background).
- Detail pane user label updated to `uid=2000 pid=<N>` (the
 literal `(no selection)` text is gone).
- Detail pane shows an `Action: test.action` line.
- Detail pane shows an executable path line containing
 `/usr/bin/python3` (version suffix like `.13` may vary).
- Detail pane shows `Details: purpose=smoke test`.
- The scope radio group and Approve/Deny buttons are still visible
 and enabled.

### S3 — Deny via Ctrl+N, confirm empty state returns

The Qt admin app wires `Ctrl+Y`/`Ctrl+N` as `WindowShortcut`-scoped
QShortcuts for Approve/Deny (see `_mk_shortcut` in
`admin_app/qdistro_admin_app.py`). They fire only while the admin
window is active, and the decision keys are guarded by the Pending
tab — but this test launches straight into the Pending tab with the
window focused, so a single Ctrl+N decides as expected (no tab
switch is needed). We inject Ctrl+N at the KVM keyboard level via
`virsh send-key` because xdotool modifier combos don't reach
XWayland Qt apps under the GUI test compositor (see AGENTS.md caveat).

```bash
# Focus the admin-approvals window before the KVM-level keystroke,
# so the focused X client that receives the event is the Qt app.
B64=$(base64 -w0 <<'EOF'
#!/bin/bash
runuser -u admin -- env DISPLAY=:0 \
 xdotool search --sync --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 1
$VMGUI "$VM" screenshot /tmp/03-qt-admin-app-visual-s3-afterdeny.png
```

**Assert (after deny):**
- Left list view is empty again.
- Detail pane user label returns to `(no selection)`.
- `Action:`, exe, and `Details:` labels are empty / blank.
- No error dialog, modal, or red banner appears.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/test-pid /tmp/test-output.txt /tmp/admin-app.log'
```

## Notes for the runner

- The admin app is a Qt widget app running under XWayland
 (`QT_QPA_PLATFORM=xcb`). `xdotool search --name` works on it,
 unlike native Wayland surfaces.
- If the launch fails, inspect `/tmp/admin-app.log` inside the VM.
 Typical cause: stale Wayland env vars or missing
 `DBUS_SESSION_BUS_ADDRESS`.
- S3 uses `Ctrl+N` (the app's wired shortcut) rather than a pixel
 click — font/DPI/placement changes won't break it.
- Deny vs. Approve: both have shortcuts (`Ctrl+Y` / `Ctrl+N`); the
 scenario picks Deny specifically so the `qdistro-test-permission`
 subprocess doesn't get an unintended allow, which matters if
 future scenarios rely on clean broker state.
