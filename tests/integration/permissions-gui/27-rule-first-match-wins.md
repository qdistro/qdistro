# 27 — Rule ordering: first match wins

**What**: install two rules for the same `(uid=2000, action=test.action)`
pair in this order: `(allow)` then `(deny)`. Run
`qdistro-test-permission` as `work`; expect ALLOWED (first match
wins). Swap the order — install `(deny)` first then `(allow)`;
expect DENIED. The two rules live in two filenames whose sorted
order pins the load order.

**Why**: `permissions.md` §Declarative rules: "Rules are matched
top-to-bottom; first match wins." The rule loader sorts filenames
in `/etc/qdistro/rules.d/` so the on-disk filename prefix is the
ordering knob admins rely on. A regression where later files
override earlier ones (or where all-rules-matching combine
non-deterministically) would silently invert admin intent.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
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

### S1 — install `(allow)` as `27a-...`, `(deny)` as `27b-...`

```bash
B64=$(base64 -w0 <<'EOF'
cat >/etc/qdistro/rules.d/27a-allow.yaml <<'YAML'
- name: allow-first
  decision: allow
  match:
    uid: 2000
    action: test.action
  rationale: scenario 27 S1 — first-match-wins (allow wins)
YAML
cat >/etc/qdistro/rules.d/27b-deny.yaml <<'YAML'
- name: deny-second
  decision: deny
  match:
    uid: 2000
    action: test.action
  rationale: scenario 27 S1 — should be ignored (later in sort order)
YAML
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.ReloadRules'
sleep 1
```

**Assert**: `ReloadRules` reply contains `int32 2` (two rules
loaded) and an empty `array []` of errors.

### S2 — `work` action is ALLOWED (first rule wins)

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
  >/tmp/27-work-a.log 2>&1 & echo $! >/tmp/27-work-a.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMEXEC "$VM" 'wait $(cat /tmp/27-work-a.pid) 2>/dev/null; cat /tmp/27-work-a.log'

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT decision, source, rule_path FROM audit
  WHERE action='test.action' ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**:
- `/tmp/27-work-a.log` contains `ALLOWED`.
- Audit row: `1|rule|/etc/qdistro/rules.d/27a-allow.yaml`. The
  `rule_path` column is what proves the *allow* file fired and the
  *deny* file was never consulted.

### S3 — swap names so `(deny)` sorts first

```bash
# Rename so 27a-* is now the deny rule and 27b-* is the allow rule.
$VMEXEC "$VM" 'mv /etc/qdistro/rules.d/27a-allow.yaml /etc/qdistro/rules.d/27c-allow.yaml'
$VMEXEC "$VM" 'mv /etc/qdistro/rules.d/27b-deny.yaml /etc/qdistro/rules.d/27a-deny.yaml'
$VMEXEC "$VM" 'runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.ReloadRules'
sleep 1
```

**Assert**: `ReloadRules` reply: `int32 2`, empty error array.

### S4 — `work` action is now DENIED

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
  >/tmp/27-work-b.log 2>&1 & echo $! >/tmp/27-work-b.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMEXEC "$VM" 'wait $(cat /tmp/27-work-b.pid) 2>/dev/null; cat /tmp/27-work-b.log'

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT decision, source, rule_path FROM audit
  WHERE action='test.action' ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**:
- `/tmp/27-work-b.log` contains `DENIED`.
- Audit row: `0|rule|/etc/qdistro/rules.d/27a-deny.yaml`.

## Teardown

```bash
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
$VMEXEC "$VM" 'rm -f /tmp/27-work-*.log /tmp/27-work-*.pid'
```

## Notes for the runner

- The renames in S3 only re-order on disk; the *content* of the
  rules is unchanged. If S4 reports ALLOWED, the broker either
  isn't re-walking the directory in filename order, or `ReloadRules`
  didn't pick the rename up (try `ls -1 /etc/qdistro/rules.d/27*`
  to confirm the new layout).
- This is the precondition for any "policy as code" workflow: admins
  rely on filename-prefix ordering being deterministic.
