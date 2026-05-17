# 37 — Cross-silo clipboard transfer default-denies, rule allows

**What**: as `admin`, call `CheckClipboardTransfer("user1", "admin",
["text/plain"], ...)` with no rule installed. Expect `"deny"` and a
`source='clipboard_default_deny'` audit row. Then install an allow
rule for that exact action; repeat the call; expect `"allow"` and
`source='clipboard_rule'`.

**Why**: cross-silo data movement is opt-in. The two assertions
together prove (a) the default-deny is enforced and (b) admin can
opt in by writing a rule. Without (a), every clipboard read across
silos would leak by default; without (b), there'd be no way to
opt in at all.

This is a headless scenario.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action LIKE 'qdistro.clipboard.transfer:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Steps

### S1 — no rule: cross-silo defaults to deny

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.CheckClipboardTransfer \
  string:"user1" \
  string:"admin" \
  array:string:"text/plain" \
  string:"" \
  string:"" \
  string:""
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT action, decision, source FROM audit
  WHERE action='qdistro.clipboard.transfer:user1:admin'
  ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**:
- dbus-send reply: `string "deny"`.
- Audit row's `source` cell starts with `clipboard_default_deny`
  and contains `mime=text/plain`. Decision cell is `0`.

### S2 — install an allow rule for that specific transfer

```bash
B64=$(base64 -w0 <<'EOF'
YAML='- name: allow-user1-to-admin-clipboard
  decision: allow
  match:
    action: qdistro.clipboard.transfer:user1:admin
  rationale: scenario 37 — opt-in cross-silo clipboard
'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.SaveRule \
  string:"37-allow-user1-admin.yaml" \
  string:"$YAML"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1
```

### S3 — same call now returns `"allow"`, audit `source='clipboard_rule'`

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.CheckClipboardTransfer \
  string:"user1" \
  string:"admin" \
  array:string:"text/plain" \
  string:"" \
  string:"" \
  string:""
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT action, decision, source, rule_path FROM audit
  WHERE action='qdistro.clipboard.transfer:user1:admin'
  ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**:
- dbus-send reply: `string "allow"`.
- Audit row: `decision=1`, `source` starts with `clipboard_rule`,
  `rule_path = /etc/qdistro/rules.d/37-allow-user1-admin.yaml`.

### S4 — reverse direction is still default-deny

```bash
# admin → user1 has no rule; cross-silo default still applies.
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.CheckClipboardTransfer \
  string:"admin" \
  string:"user1" \
  array:string:"text/plain" \
  string:"" \
  string:"" \
  string:""
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: reply is `string "deny"`. (Allow rules are directional;
allowing user1→admin doesn't allow admin→user1.)

## Teardown

```bash
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action LIKE 'qdistro.clipboard.transfer:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- The `source` cell in audit rows carries the mime list, source-app,
  dest-app, and sandbox engine after the source label. The assertion
  intentionally only matches the leading prefix
  (`clipboard_rule` vs `clipboard_default_deny`) to avoid coupling
  to mime/app ordering.
- S4 is the directional check. A regression that interpreted rules
  bidirectionally (admin→user1 because user1→admin is allowed)
  would be subtle and dangerous — admin data leaking back into a
  less-trusted silo.
