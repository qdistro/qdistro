# 03 — Qt admin approval app renders master/detail correctly

<!-- qci:visual: required -->

**Lane: qdwin** (native Wayland, the shipped launcher). Read the "qdwin lane"
section of `AGENTS.md` first: no xdotool, no `DISPLAY=:0`; graded frames come
from `qdwin_screenshot`.

**What**: launch the PyQt admin app in admin's qdwin session the way the
image does, inject one pending request, verify the master (list) / detail
(form) layout renders with correct empty and populated states, and confirm
Deny returns the pane to empty.

**Why**: the Qt app is the shipped approver. Its pixel layout — list on the
left, detail on the right with scope radio buttons + Approve/Deny — is the
authority for what "done" looks like on the product desktop. Tests pass the
model but can't see that the radio group rendered or the Approve button is
focused.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh   # qdwin_screenshot (host side)
qdwin_set_vm "$VM"
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}

# Session up, work/work2 silo fixtures, idle locker held off and proven
# unlocked. A nonzero exit is a Setup ERROR.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_lane_setup --silos'
# Clean slate: no stray admin app, no stray test caller.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'

# Drain broker state — a prior scenario may have left a stale pending
# request, which would falsify the S1 empty-state assertion.
# Restarting the service empties the in-memory queue.
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'
```

## Steps

### S1 — launch admin app, empty state

`qdwin_start_admin_app` runs the shipped `/usr/local/bin/qdistro-start-admin-app`
in its first-paint mode: it returns (printing the app pid) only after the
window has painted its first frame and the compositor holds it. A nonzero exit
FAILS S1.

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_app > /tmp/03-app.pid'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30'
qdwin_screenshot "$ART/03-qt-admin-app-visual-s1-empty.png"
```

**Assert (empty):**
- The admin approvals window is visible on the qdwin desktop. qdwin draws
 no titlebar for ordinary apps, so look for the window body, not a
 titlebar; the title itself is proved by the title wait above.
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
- The window is fully drawn: no black, transparent or desktop-patterned
 region cuts through it. On this lane a partly drawn window is a FAIL.

### S2 — inject one pending request, populated state

```bash
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start 03-work work 'python3 /usr/local/bin/qdistro-test-permission'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

Then, as a separate command, wait for the app to SHOW the request. The
title is computed from the Pending model's row count (see
`_update_window_title`), so `(1 pending)` means the row is in the list, and
qdwin logs the title on the commit that carries it:

```bash
title_rc=0
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals \(1 pending\)" 30' || title_rc=$?
echo "s2 title-wait rc=$title_rc"
qdwin_screenshot "$ART/03-qt-admin-app-visual-s2-populated.png"
```

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
window has keyboard focus, and the decision keys are guarded by the Pending
tab — this test launches straight into the Pending tab, so a single Ctrl+N
decides. Focus the window through the compositor first (`qdwin_focus_window`
returns only once qdwin reports keyboard focus on it), then inject Ctrl+N at
the KVM keyboard from the HOST:

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
virsh -c qemu:///session send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
```

Then, as a separate command, the title wait. Never capture straight after
the keystroke: the deny is a D-Bus round trip plus a 250 ms debounced
refresh. A bare `admin approvals` title (no `(N pending)`) means the model is
empty:

```bash
title_rc=0
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "admin approvals" 30' || title_rc=$?
echo "s3 title-wait rc=$title_rc"
qdwin_screenshot "$ART/03-qt-admin-app-visual-s3-afterdeny.png"
# The caller got the deny (ground truth, independent of the frame).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 03-work 60; bg_log 03-work; echo "rc=$(bg_rc 03-work)"'
```

A non-zero `s3 title-wait rc` means the deny never emptied the model: S3
FAILS on that ground regardless of the frame.

**Assert (after deny):**
- Left list view is empty again.
- Detail pane user label returns to `(no selection)`.
- `Action:`, exe, and `Details:` labels are empty / blank.
- No error dialog, modal, or red banner appears.
- The caller's log shows `DENIED` (not `ALLOWED`).

## Teardown

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/03-app.pid'
```

## Notes for the runner

- The admin app is a native Wayland Qt client here (`QT_QPA_PLATFORM=wayland`,
 set by the shipped launcher). If the launch fails, the launcher names the
 app log it wrote under `/run/user/1000/qdistro-admin-app.*.log`; read it.
- S3 uses `Ctrl+N` (the app's wired shortcut) rather than a pixel
 click — font/DPI/placement changes won't break it.
- Deny vs. Approve: both have shortcuts (`Ctrl+Y` / `Ctrl+N`); the
 scenario picks Deny specifically so the `qdistro-test-permission`
 subprocess doesn't get an unintended allow, which matters if
 future scenarios rely on clean broker state.
