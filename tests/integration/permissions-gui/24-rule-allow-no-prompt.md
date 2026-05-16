# 24 — Declarative `allow` rule short-circuits the admin prompt

**What**: write an allow-rule for `(uid=2000, action=test.action)`
via `SaveRule`, then run `qdistro-test-permission` as `work`. Assert
the SDK returns ALLOWED *without* the admin app ever showing a
pending row, the audit log records `source='rule'` (not
`source='prompt'`), and the cache stays empty (rules do not write
cache rows).

**Why**: `permissions.md` §Declarative rules is the load-bearing
path for admin-managed automation — rule-matched actions must
never spend admin attention. A regression where a rule decision
also enqueues a prompt would deluge admin with notifications for
auto-allowed traffic. A regression where a rule's allow doesn't
short-circuit at all would mean the rule engine is wired but inert.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'

# Wipe rules + cache + audit so the assertions read off a clean slate.
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
DELETE FROM audit WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Steps

### S1 — install the allow rule via `SaveRule`

```bash
# SaveRule's yaml_body must be a top-level list per the rules engine.
# Use base64-wrapping for the whole dbus-send because the YAML body
# has embedded newlines + quotes.
B64=$(base64 -w0 <<'EOF'
YAML='- name: allow-work-test-action
  decision: allow
  match:
    uid: 2000
    action: test.action
  rationale: scenario 24 — rule-allow short-circuit
'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.SaveRule \
  string:"24-allow-test-action.yaml" \
  string:"$YAML"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1
```

**Assert**:
- `dbus-send` reply is `string "/etc/qdistro/rules.d/24-allow-test-action.yaml"`.
- File exists on disk:
  ```bash
  $VMEXEC "$VM" 'ls -l /etc/qdistro/rules.d/24-allow-test-action.yaml'
  ```
  Output shows a file mode `-rw-r--r--`, non-zero size.

### S2 — launch admin app (must stay empty)

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
$VMGUI "$VM" screenshot /tmp/24-s2-empty.png
```

**Assert** (`/tmp/24-s2-empty.png`):
- Window `admin approvals` visible.
- Pending list empty; detail pane reads `(no selection)`.

### S3 — trigger the action as `work`; expect ALLOWED, no prompt

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
  >/tmp/24-work.log 2>&1 & echo $! >/tmp/24-work.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/24-s3-stillempty.png

$VMEXEC "$VM" 'wait $(cat /tmp/24-work.pid) 2>/dev/null; cat /tmp/24-work.log'
$VMEXEC "$VM" 'dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.GetPending'
```

**Assert**:
- `/tmp/24-s3-stillempty.png` shows the same empty admin app —
  no new row in the Pending list, detail pane still `(no selection)`.
- `/tmp/24-work.log` contains `ALLOWED` on its own line.
- `GetPending` dbus-send output is `array []` (empty array — no
  request was ever enqueued).

### S4 — audit row carries `source='rule'`, no cache row written

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, action, decision, scope, source FROM audit
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
- Audit row: `2000|test.action|1|<scope>|rule`. The `scope` cell
  may be empty or carry the rule's own scope; the load-bearing
  fields are `decision=1` and `source='rule'`. (NOT `source='prompt'`
  and NOT `source='cache'`.)
- Cache count is `0` — rule decisions do not populate the cache.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
DELETE FROM audit WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
$VMEXEC "$VM" 'rm -f /tmp/24-work.log /tmp/24-work.pid'
```

## Notes for the runner

- `SaveRule` itself calls `reload_rules_from_disk`, so the rule is
  live before its reply lands. No sleep beyond the standard 1s
  margin is needed before triggering the action in S3.
- If S3 fails (admin app shows a pending row), the most likely
  cause is the rule YAML failing to parse — `SaveRule` would have
  raised `RulesEngineRefused` in S1. Re-check the S1 dbus-send
  reply: it must be `string "/etc/qdistro/rules.d/..."`, not an
  `Error` reply.
- Scenario 25 is the deny twin; 28 covers ordering between two
  rules on the same action.
