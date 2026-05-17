# 35 — TUI and Qt admin app stay in sync via broker signals

**What**: launch the Qt admin app AND the Textual TUI side by side
(both subscribe to the same broker's `RequestPending` /
`RequestDecided` signals). Inject one pending request as `work`.
Verify both UIs show the row. Approve in the Qt app (Ctrl+Y);
verify the TUI's pending list empties immediately (driven by the
`RequestDecided` signal, not by a polling refresh).

**Why**: `permissions.md` doesn't pin a "one approver per session"
constraint, and the broker's signal-based design supports multiple
concurrent subscribers. The future model (admin's desktop +
mobile admin in the future) depends on subscribers staying in sync.
A regression where the TUI cache its own view and didn't react to
`RequestDecided` from another subscriber would let two admins
double-decide a request — the second decide would error out with
"request id N gone" but admin's mental model would be muddy.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_tui 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin qterminal 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1
```

## Steps

### S1 — launch TUI and Qt admin app

```bash
# Launch TUI in qterminal first, then Qt admin app. Both subscribe
# to RequestPending immediately.
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-tui'
sleep 3
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
$VMGUI "$VM" screenshot /tmp/35-s1-both-empty.png
```

**Assert**: `/tmp/35-s1-both-empty.png` shows both windows on
screen (Qt admin app's `admin approvals` window AND a qterminal
running the TUI). Both list panes are empty.

### S2 — inject one pending request

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
  >/tmp/35-work.log 2>&1 & echo $! >/tmp/35-work.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2

$VMGUI "$VM" screenshot /tmp/35-s2-both-show-row.png
```

**Assert** (`/tmp/35-s2-both-show-row.png`):
- Qt admin app pending list shows one row: `uid=2000  test.action`.
- TUI's inner pending list shows the same request (action
  `test.action`, uid 2000). Both UIs updated without polling —
  the broker emitted `RequestPending` once and both subscribers
  reacted.

### S3 — focus the Qt admin app, approve with Ctrl+Y

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 1.5

$VMGUI "$VM" screenshot /tmp/35-s3-both-emptied.png
$VMEXEC "$VM" 'wait $(cat /tmp/35-work.pid) 2>/dev/null; cat /tmp/35-work.log'
```

**Assert** (`/tmp/35-s3-both-emptied.png`):
- Qt admin app's pending list is empty; detail pane reads
  `(no selection)`.
- TUI's pending list is also empty — the TUI received the
  broker's `RequestDecided` signal and removed the entry on its
  own. (NOT just visually empty because the window is occluded —
  the runner should confirm by reading the TUI's pane content,
  e.g. "no pending requests" placeholder or empty bullet list.)
- `/tmp/35-work.log`: `ALLOWED`.

### S4 — invariant: TUI didn't try to double-decide

```bash
$VMEXEC "$VM" 'journalctl -u qdistro-admin-broker.service --since "1 minute ago" \
  | grep -E "(invalid|gone|already|stale).*request" || true'
```

**Assert**: zero lines matching that pattern. A double-decide
attempt from the TUI would show as `"DecideRequest: request_id N
gone"` or similar in the broker log.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_tui 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin qterminal 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/35-work.log /tmp/35-work.pid'
```

## Notes for the runner

- The TUI and Qt admin app both run as the `admin` uid. They share
  the same system bus connection family but separate proxy objects;
  the broker's signal subscribers are connection-scoped, so two
  subscribers receive two signal copies.
- If S3 shows the Qt app emptied but the TUI still has the row,
  the TUI's signal subscription is broken (likely the same
  bus_name-filter bug fixed in commit `d72a430` for the Qt app
  — see scenario 08 for the cousin failure). File a separate
  todo card for the TUI version of that fix.
- TUI placement on screen depends on labwc's window-management;
  the runner may need to read the TUI screenshot region from a
  different location than the Qt app. OCR the visible text first
  to identify which window is which.
