# 25 — Declarative `deny` rule short-circuits without prompting

**What**: write a deny-rule for `(uid=2000, action=test.action)` via
`SaveRule`, then run `qdistro-test-permission` as `work`. Assert the
SDK returns DENIED, no admin prompt appears, the audit row carries
`source='rule'` with `decision=0`, and no cache row is written.

**Why**: the deny path's silent-failure mode is the opposite of
allow's: a regression where deny *also* enqueues a prompt would
ask admin to authorize something an explicit rule already
forbids; a regression where deny doesn't short-circuit at all
would mean a deny rule is purely advisory. permissions.md says
denies are silent and authoritative.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

APPROVALS_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
SQL_EOF
)
AUDIT_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $APPROVALS_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $AUDIT_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Steps

### S1 — install the deny rule via `SaveRule`

```bash
B64=$(base64 -w0 <<'EOF'
YAML='- name: deny-work-test-action
  decision: deny
  match:
    uid: 2000
    action: test.action
  rationale: scenario 25 — rule-deny short-circuit
'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.SaveRule \
  string:"25-deny-test-action.yaml" \
  string:"$YAML"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1
```

**Assert**: reply is
`string "/etc/qdistro/rules.d/25-deny-test-action.yaml"`.

### S2 — launch admin app

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
$VMGUI "$VM" screenshot /tmp/25-s2-empty.png
```

**Assert** (`/tmp/25-s2-empty.png`): empty pending list, detail
pane `(no selection)`.

### S3 — trigger as `work`; expect DENIED, no prompt

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
  >/tmp/25-work.log 2>&1 & echo $! >/tmp/25-work.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/25-s3-stillempty.png

$VMEXEC "$VM" 'wait $(cat /tmp/25-work.pid) 2>/dev/null; cat /tmp/25-work.log'
$VMEXEC "$VM" 'dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.GetPending'
```

**Assert**:
- `/tmp/25-s3-stillempty.png` shows the same empty admin app.
- `/tmp/25-work.log` contains `DENIED` on its own line.
- `GetPending` output is `array []`.

### S4 — audit row carries `source='rule'`, `decision=0`, cache empty

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, action, decision, source FROM audit
  WHERE action='test.action' ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT count(*) FROM approvals WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
```

**Assert**:
- Audit row: `2000|test.action|0|rule`.
- Cache count is `0`.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
APPROVALS_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
SQL_EOF
)
AUDIT_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $APPROVALS_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $AUDIT_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
$VMEXEC "$VM" 'rm -f /tmp/25-work.log /tmp/25-work.pid'
```
