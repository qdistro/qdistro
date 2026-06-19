# 22 — `ApprovalRevoked` signal payload, GUI revoke path

**What**: with one cached approval seeded for `work` (uid 2000),
attach `dbus-monitor` to the system bus, then revoke the row from
the Qt admin app's Cache tab. Verify (a) one `ApprovalRevoked`
signal is emitted with the correct `(caller_uid, action, exe)`
payload, (b) the cache row is gone, (c) an audit row with
`source='revoke'` was written.

**Why**: permissions.md  promises that revocation is broadcast as
a D-Bus signal so subscribers (qdshell first, others later) tear down
resources granted by the cached row at the same instant the row
disappears. Scenario 10 covers the GUI mechanics of revoke; this
scenario is the wire-level contract — payload shape and one-signal-
per-row.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -f "[d]bus-monitor.*ApprovalRevoked" 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

APPROVALS_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals;
SQL_EOF
)
AUDIT_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE source='revoke';
SQL_EOF
)
$VMEXEC "$VM" "echo $APPROVALS_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $AUDIT_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"

# Seed one approval — forever_exe so match_value carries an exe path
# the signal payload can be checked against.
B64=$(base64 -w0 <<'EOF'
python3 - <<'PY'
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
c.store(2000, "test.action", "/usr/bin/python3.13", "forever_exe", True, 1000)
print("seeded 1 row")
PY
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Steps

### S1 — attach dbus-monitor, launch admin app

```bash
# dbus-monitor runs as root because the system bus policy file
# restricts the eavesdrop privilege to root. Capture only
# ApprovalRevoked traffic so the log is one signal long when the
# scenario succeeds.
$VMEXEC "$VM" 'rm -f /tmp/22-signals.log; \
  setsid dbus-monitor --system \
    "type=signal,interface=org.qdistro.AdminBroker1,member=ApprovalRevoked" \
    >/tmp/22-signals.log 2>&1 </dev/null &
  echo $! >/tmp/22-monitor.pid'
sleep 1

$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3

B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/22-s1-launched.png
```

**Assert**:
- Window `admin approvals` is visible.
- `/tmp/22-signals.log` exists; its content (post-`signal time=...`
  header lines from dbus-monitor's startup) does NOT yet contain
  the substring `member=ApprovalRevoked` — no signal has fired.

### S2 — keyboard: reach Cache tab, select row, press Revoke

Mouse clicks are platform-blocked on the XWayland GUI template
(AGENTS.md §3b), so drive the whole revoke path with the keyboard via
`virsh send-key` (the blessed input path, §3a). After launch the
window focuses the Pending list; one **Shift+Tab** moves focus back to
the tab bar, **Right** switches Pending→Cache, **Tab** enters the
Cache table, **Down** selects the `test.action` row (sets the table's
`currentIndex`, which `btn_revoke` acts on), a second **Tab** moves
focus from the table OUT to the `btn_revoke` button, and **Space**
activates it. The Tab-out-of-table step relies on the Cache table
having `tabKeyNavigation` disabled — Qt's default traps Tab inside the
view, which would make `btn_revoke` unreachable by keyboard with a row
selected.

```bash
# Re-focus the window so the evdev send-key events land on it.
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 0.5

# Shift+Tab: Pending list -> tab bar.
virsh send-key "$VM" --codeset linux --holdtime 100 KEY_LEFTSHIFT KEY_TAB
sleep 0.3
# Right: Pending -> Cache tab.
virsh send-key "$VM" --codeset linux KEY_RIGHT
sleep 0.3
$VMGUI "$VM" screenshot /tmp/22-s2a-cache-tab.png
# Tab: focus into the Cache table.
virsh send-key "$VM" --codeset linux KEY_TAB
sleep 0.3
# Down: select the test.action row (sets currentIndex).
virsh send-key "$VM" --codeset linux KEY_DOWN
sleep 0.3
$VMGUI "$VM" screenshot /tmp/22-s2b-row-selected.png
# Tab: focus OUT of the table to btn_revoke (needs tabKeyNavigation off).
virsh send-key "$VM" --codeset linux KEY_TAB
sleep 0.3
# Space: activate the focused Revoke button.
virsh send-key "$VM" --codeset linux KEY_SPACE
sleep 1
$VMGUI "$VM" screenshot /tmp/22-s2c-after-revoke.png
```

**Assert**:
- `/tmp/22-s2a-cache-tab.png`: the Cache tab is selected and shows the
  `test.action` row (table is non-empty before revoke).
- `/tmp/22-s2c-after-revoke.png`: Cache tab table is empty (no
  `test.action` row).
- No error dialog appeared.

(Screenshots are coarse on this template — AGENTS.md "Ground truth".
The authoritative checks are the dbus-monitor log in S3 and the audit
row in S4, which prove the revoke actually fired with the right
payload.)

### S3 — exactly one signal, correct payload

```bash
$VMEXEC "$VM" 'kill $(cat /tmp/22-monitor.pid) 2>/dev/null; sleep 0.3; cat /tmp/22-signals.log'
```

**Assert** (textual analysis of `/tmp/22-signals.log`):
- Exactly **one** `signal ... interface=org.qdistro.AdminBroker1;
  member=ApprovalRevoked` block.
- The signal body contains three arguments in this order:
  - `int32 2000`  (caller_uid)
  - `string "test.action"`  (action)
  - `string "/usr/bin/python3.13"`  (exe / match_value)

### S4 — audit row matches

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, action, source, approver_uid FROM audit
  WHERE source='revoke' ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**: output is exactly `2000|test.action|revoke|1000`.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'kill $(cat /tmp/22-monitor.pid) 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/22-signals.log /tmp/22-monitor.pid'
APPROVALS_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals;
SQL_EOF
)
AUDIT_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE source='revoke';
SQL_EOF
)
$VMEXEC "$VM" "echo $APPROVALS_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $AUDIT_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- The signal's `exe` argument carries the cache row's `match_value`
  (the exe captured at decide-time), NOT a re-derived live caller exe.
  For a `forever_exe` row that's the same string; for a `forever`
  row `match_value` is empty and the signal carries `string ""`.
  Future scenarios should cover both shapes — this one pins the
  `forever_exe` shape since it's the load-bearing case for qdshell's
  per-exe stream teardown.
- `dbus-monitor --system` needs root. If `kill` of the monitor PID
  doesn't reap it (orphaned via setsid), the next run's monitor will
  share the log file and produce two-signal noise; the teardown
  block above cleans up explicitly.
