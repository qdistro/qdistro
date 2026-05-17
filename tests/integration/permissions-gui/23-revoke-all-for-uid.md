# 23 — `RevokeAllForUid` emits one signal per row, scoped to uid

**What**: seed five cached approvals across two uids (3 for uid 2000,
2 for uid 3000), attach a dbus-monitor on `ApprovalRevoked`, call
`RevokeAllForUid(2000)` as admin from a shell, verify (a) exactly
three signals fired, all carrying `caller_uid=2000`, (b) uid 3000's
rows survive, (c) three matching `source='revoke'` audit rows landed.

**Why**: scenarios 10 / 22 cover single-row revoke. `RevokeAllForUid`
is the admin's panic button — "rip every cached grant for that user
out right now" — and the contract is one signal per row so qdshell
(and other subscribers) tear down per-row resources. A bug that
silently merges revocations into one signal would leak access on
all but one of the rows.

This is a headless scenario; no GUI interaction.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

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

B64=$(base64 -w0 <<'EOF'
python3 - <<'PY'
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
# uid 2000: three distinct actions
c.store(2000, "act.a", "/usr/bin/python3.13", "forever_exe", True, 1000)
c.store(2000, "act.b", "/usr/bin/curl",       "24h",         True, 1000)
c.store(2000, "act.c", "",                    "forever",     True, 1000)
# uid 3000: two distinct actions (must survive)
c.store(3000, "act.x", "/usr/bin/vim",        "forever_exe", True, 1000)
c.store(3000, "act.y", "",                    "forever",     True, 1000)
print("seeded 5 rows")
PY
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Steps

### S1 — attach dbus-monitor, sanity-check seed

```bash
$VMEXEC "$VM" 'rm -f /tmp/23-signals.log; \
  setsid dbus-monitor --system \
    "type=signal,interface=org.qdistro.AdminBroker1,member=ApprovalRevoked" \
    >/tmp/23-signals.log 2>&1 </dev/null &
  echo $! >/tmp/23-monitor.pid'
sleep 1

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, action FROM approvals ORDER BY caller_uid, action;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
```

**Assert**:
- sqlite output lists five rows: `2000|act.a`, `2000|act.b`,
  `2000|act.c`, `3000|act.x`, `3000|act.y`.

### S2 — call `RevokeAllForUid(2000)` as admin

```bash
$VMEXEC "$VM" 'runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.RevokeAllForUid \
  int32:2000'
sleep 1
```

**Assert**: `dbus-send` output ends with `int32 3` (three rows deleted).

### S3 — exactly three signals, all for uid 2000

```bash
$VMEXEC "$VM" 'kill $(cat /tmp/23-monitor.pid) 2>/dev/null; sleep 0.3; cat /tmp/23-signals.log'
```

**Assert** (textual analysis of `/tmp/23-signals.log`):
- Exactly three `member=ApprovalRevoked` signal blocks.
- Every block's first body argument is `int32 2000`. (No
  `int32 3000` should appear anywhere in the signal-body section
  of the log.)
- The set of action strings across the three signals is
  `{"act.a", "act.b", "act.c"}` (any order — the broker drains by
  sqlite row id, not action).

### S4 — uid 3000's rows survive, audit reflects three revokes

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, action FROM approvals ORDER BY caller_uid, action;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, action, source, approver_uid FROM audit
  WHERE source='revoke' ORDER BY id;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**:
- Surviving approvals rows: exactly `3000|act.x` and `3000|act.y`.
  No row with caller_uid=2000.
- Audit table contains exactly three `source='revoke'` rows. Every
  row has `caller_uid=2000` and `approver_uid=1000`. The three
  actions are `act.a`, `act.b`, `act.c` in some order.

## Teardown

```bash
$VMEXEC "$VM" 'kill $(cat /tmp/23-monitor.pid) 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/23-signals.log /tmp/23-monitor.pid'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals;
DELETE FROM audit WHERE source='revoke';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- `runuser -u admin -- dbus-send --system` works because the system
  bus policy file routes `RevokeAllForUid` on the admin uid (1000)
  the same way it routes admin's other calls. Root would also work
  (`approver_uid` would be `0`), but admin is the documented entry
  point.
- The three audit rows landing all carry `approver_uid=1000`
  because the broker records the calling D-Bus peer's uid, not
  the *target* uid being revoked.
