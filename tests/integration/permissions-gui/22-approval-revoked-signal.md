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
$VMEXEC "$VM" 'pkill -f "dbus-monitor.*ApprovalRevoked" 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals;
DELETE FROM audit WHERE source='revoke';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"

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

### S2 — switch to Cache tab, select row, click Revoke

```bash
# >>> Runner: OCR /tmp/22-s1-launched.png to find the `Cache` tab
# header; click it. Then OCR the new screenshot for the row containing
# `test.action`; click any cell in that row; then OCR for the `Revoke`
# button and click it. (Same pattern as scenario 10.)
$VMGUI "$VM" screenshot /tmp/22-s2a-cache-tab.png
# (runner clicks `Cache` tab)
$VMGUI "$VM" screenshot /tmp/22-s2b-row-selected.png
# (runner clicks the test.action row)
$VMGUI "$VM" screenshot /tmp/22-s2c-after-revoke.png
# (runner clicks `Revoke`)
sleep 1
```

**Assert** (`/tmp/22-s2c-after-revoke.png`):
- Cache tab table is empty (no `test.action` row).
- No error dialog appeared.

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
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals;
DELETE FROM audit WHERE source='revoke';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
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
