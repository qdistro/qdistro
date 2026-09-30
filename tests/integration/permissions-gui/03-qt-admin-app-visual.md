# 03 — Qt admin approval app renders master/detail correctly

<!-- qci:visual: required -->

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

Open the S1 frame before grading it. If the window is only partly drawn
(a black, transparent, or desktop-patterned rectangle cuts through its
contents), keep that frame and capture up to four more frames, 2 s apart,
under distinct `-r2.png` ... `-r5.png` names. Open each new frame. Use the
first fully drawn frame as the S1 evidence; if none is fully drawn, report
the rendering failure with all captures. This does not relax any visual
assertion below.

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
```

Then, as a separate command, wait for the app to SHOW the request. The
title is computed from the Pending model's row count (see
`_update_window_title`), so `(1 pending)` means the row is in the list:

```bash
title_rc=0
$VMEXEC "$VM" 'for _ in $(seq 1 60); do
  t=$(runuser -u admin -- env DISPLAY=:0 xdotool search --name "^admin approvals" getwindowname 2>/dev/null | head -1)
  [ "$t" = "admin approvals (1 pending)" ] && exit 0
  sleep 0.5
done
echo "title never showed the request: $t" >&2; exit 1' || title_rc=$?
echo "s2 title-wait rc=$title_rc"
$VMGUI "$VM" screenshot /tmp/03-qt-admin-app-visual-s2-populated.png
```

Apply the same bounded recapture procedure to S2 if the frame is partly
drawn. The title wait proves the model row exists; it does not prove that
the guest framebuffer has finished drawing the detail pane.

A non-zero `s2 title-wait rc` FAILS S2 regardless of the frame.

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
 timeout 10 xdotool search --sync --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
```

Then, as a separate command, the title wait. Never screenshot straight
after the keystroke: the deny is a D-Bus round trip plus a 250 ms
debounced refresh, and a capture taken 158 ms after `send-key` under a
16-way run graded the still-populated pane as "Ctrl+N did not deny"
(2026-09-28). A bare `admin approvals` title (no `(N pending)`) means the
model is empty:

```bash
title_rc=0
$VMEXEC "$VM" 'for _ in $(seq 1 60); do
  t=$(runuser -u admin -- env DISPLAY=:0 xdotool search --name "^admin approvals" getwindowname 2>/dev/null | head -1)
  [ "$t" = "admin approvals" ] && exit 0
  sleep 0.5
done
echo "title never settled: $t" >&2; exit 1' || title_rc=$?
echo "s3 title-wait rc=$title_rc"
```

A non-zero `s3 title-wait rc` (about 30 s) means the deny never emptied
the model: S3 FAILS on that ground regardless of the frames.

The client surface can lag the title by a frame, so capture at most 5
frames, 2 s apart, each as a separate runner action (not a shell loop),
starting with N=1:

1. `$VMGUI "$VM" screenshot /tmp/03-s3-afterdeny-N.png` (substitute N).
2. Open it and grade by vision: the empty state is `(no selection)` in
   the detail pane and no row in the list.
3. If it shows the empty state, make it canonical and stop:
   `$VMGUI "$VM" view-copy /tmp/03-s3-afterdeny-N.png --out /tmp/03-qt-admin-app-visual-s3-afterdeny.png`
4. Otherwise, if N < 5: `sleep 2`, increment N, go back to 1.
5. If frame 5 is still stale, S3 FAILS: `view-copy` frame 5 to the
   canonical path and grade that below.

Keep every numbered frame. Use `view-copy`, not `cp`: a same-size twin
of a frame you have already seen reads as black where it repeats.

**Assert (after deny):**
- Left list view is empty again.
- Detail pane user label returns to `(no selection)`.
- `Action:`, exe, and `Details:` labels are empty / blank.
- No error dialog, modal, or red banner appears.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/test-pid /tmp/test-output.txt /home/admin/.local/state/qdistro/admin-app.log'
```

## Notes for the runner

- The admin app is a Qt widget app running under XWayland
 (`QT_QPA_PLATFORM=xcb`). `xdotool search --name` works on it,
 unlike native Wayland surfaces.
- If the launch fails, inspect `/home/admin/.local/state/qdistro/admin-app.log` inside the VM.
 Typical cause: stale Wayland env vars or missing
 `DBUS_SESSION_BUS_ADDRESS`.
- S3 uses `Ctrl+N` (the app's wired shortcut) rather than a pixel
 click — font/DPI/placement changes won't break it.
- Deny vs. Approve: both have shortcuts (`Ctrl+Y` / `Ctrl+N`); the
 scenario picks Deny specifically so the `qdistro-test-permission`
 subprocess doesn't get an unintended allow, which matters if
 future scenarios rely on clean broker state.
