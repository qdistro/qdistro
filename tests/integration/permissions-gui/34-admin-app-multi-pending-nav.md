# 34 — Admin app navigation across multiple pending requests

**What**: inject three pending requests at once (from three
caller PIDs as `work`), launch the Qt admin app. Verify all three
rows appear in the Pending list, arrow-Down moves selection from
row 0 → row 1 → row 2, the detail pane updates each time with the
matching caller info, and approving row 1 leaves rows 0 and 2 with
their original selection-targets preserved.

**Why**: scenario 03 covers single-row rendering; 04 covers a
single approve. Real admin sessions have queues. The selection
state machine — keep the previously-selected request highlighted
across refreshes when possible, fall back to row 0 otherwise — is
non-trivial and easy to regress (see `MainWindow.refresh` lines
365–402). A regression that reset selection to row 0 on every
signal would feel broken to admin in a busy queue.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action LIKE 'multi.%';
DELETE FROM audit WHERE action LIKE 'multi.%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Steps

### S1 — launch admin app on empty queue

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
$VMGUI "$VM" screenshot /tmp/34-s1-empty.png
```

**Assert**: pending list empty.

### S2 — inject three pending requests

```bash
B64=$(base64 -w0 <<'EOF'
# Three callers with distinct actions so we can OCR-identify them.
# qdistro-test-permission accepts --action and --detail since
# todo/qdistro-test-permission-multi-action.md landed.
for i in 1 2 3; do
  sudo -u work bash -c "python3 /usr/local/bin/qdistro-test-permission \
      --action multi.action.$i --detail slot=$i \
      >/tmp/34-w$i.log 2>&1 & echo \$! >/tmp/34-w$i.pid"
done
sleep 3
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

$VMGUI "$VM" screenshot /tmp/34-s2-three-rows.png
$VMEXEC "$VM" 'dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.GetPending'
```

**Assert**:
- `/tmp/34-s2-three-rows.png` shows exactly three rows in the
  Pending list, in some order: `multi.action.1`, `multi.action.2`,
  `multi.action.3` (all under `uid=2000`).
- Detail pane shows row 0's action — whichever of the three is
  topmost.
- `GetPending` reports three structs.

### S3 — arrow-Down twice, detail pane tracks the selection

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

virsh send-key "$VM" --codeset linux KEY_DOWN
sleep 0.3
$VMGUI "$VM" screenshot /tmp/34-s3a-second-selected.png

virsh send-key "$VM" --codeset linux KEY_DOWN
sleep 0.3
$VMGUI "$VM" screenshot /tmp/34-s3b-third-selected.png
```

**Assert** (OCR both screenshots):
- `s3a`: detail pane's Action line reads the action of the SECOND
  visible row. (The visible-row order is whatever GetPending
  returned; verify the detail pane matches whichever row is
  highlighted with the selection-color background.)
- `s3b`: detail pane's Action line reads the action of the THIRD
  visible row. The previously-selected row is no longer
  highlighted.

### S4 — approve the currently-selected (third) row; survivors stay

```bash
# Default scope is "once" → no cache row, just a decide.
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 1
$VMGUI "$VM" screenshot /tmp/34-s4-after-approve.png

$VMEXEC "$VM" 'dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.GetPending'
```

**Assert**:
- `/tmp/34-s4-after-approve.png` shows exactly two rows in the
  Pending list. The action approved in S3b is gone; the other two
  remain.
- `GetPending` returns two structs whose actions are the un-
  approved subset of `{multi.action.1, multi.action.2,
  multi.action.3}`.
- Detail pane is NOT `(no selection)` — the broker app's
  refresh logic should preserve a sensible target row (row 0
  after the deleted row is gone) and the detail pane should
  display its action.

### S5 — clean up the surviving two requests by denying both

```bash
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 0.5
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 1
$VMGUI "$VM" screenshot /tmp/34-s5-drained.png

$VMEXEC "$VM" 'for i in 1 2 3; do
  wait $(cat /tmp/34-w$i.pid) 2>/dev/null
done; true'
```

**Assert**: `/tmp/34-s5-drained.png` shows empty pending list.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/34-w*.pid'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action LIKE 'multi.%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- The `multi.action.{1,2,3}` action namespace is chosen so the
  rows are visually distinguishable in the Pending list AND in
  the detail pane — every row has a different `Action:` string.
- Pending-list row order is NOT guaranteed by the broker — it's
  insertion order, but the python sub-shells in S2 race. Don't
  assert "row 0 is action.1" — assert the *set* of three actions
  is the right set, and that arrow-Down moves selection by one
  row in the rendered order.
- If S4's "two rows remain" check fails with three rows, the
  Ctrl+Y in S3 didn't fire — re-check that the admin app window
  has X focus (the windowactivate call is supposed to ensure
  that; if it didn't, the chord went to whoever else has focus).
