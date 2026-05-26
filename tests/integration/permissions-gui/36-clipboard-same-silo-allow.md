# 36 — Same-silo clipboard transfer is unconditionally allowed

**What**: as `admin` (uid 1000 — the broker's clipboard caller),
call `CheckClipboardTransfer("user1", "user1", ["text/plain"], ...)`.
Expect `"allow"`, a `source='clipboard_same_silo'` audit row, and
no rule consultation.

**Why**: `permissions.md` (read along with the broker source) makes
same-silo transfers a hard `"allow"` short-circuit — apps inside a
silo share their compositor clipboard by Wayland semantics, so the
broker can't and shouldn't gate them. A regression that consulted
rules for same-silo would default-deny in the no-rule case (cross-
silo rule fall-through deny), silently breaking intra-silo
copy/paste.

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

### S1 — same-silo transfer returns `"allow"`

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.CheckClipboardTransfer \
  string:"user1" \
  string:"user1" \
  array:string:"text/plain" \
  string:"" \
  string:"" \
  string:"" \
  boolean:true
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: reply body is `string "allow"`.

### S2 — audit row carries `source='clipboard_same_silo'`

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT action, decision, source FROM audit
  WHERE action LIKE 'qdistro.clipboard.transfer:%'
  ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**: output is
`qdistro.clipboard.transfer:user1:user1|1|clipboard_same_silo mime=text/plain ...`.

The `source` cell starts with `clipboard_same_silo` and contains
`mime=text/plain`; trailing fields (`src_app=`, `dst_app=`,
`src_engine=`) carry the placeholders.

### S3 — same-silo with a *cross-silo* deny rule does NOT block

```bash
# Even with a deny rule for user1→user1 in place, the same-silo
# short-circuit must override (the rule is impossible to match
# anyway because the broker doesn't consult rules on same-silo).
B64=$(base64 -w0 <<'EOF'
YAML='- name: scenario-36-deny-user1
  decision: deny
  match:
    action: qdistro.clipboard.transfer:user1:user1
  rationale: should never fire — broker short-circuits same-silo
'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.SaveRule \
  string:"36-rule.yaml" \
  string:"$YAML"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1

B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.CheckClipboardTransfer \
  string:"user1" \
  string:"user1" \
  array:string:"text/plain" \
  string:"" \
  string:"" \
  string:"" \
  boolean:true
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: reply body is still `string "allow"`. The rule was
not consulted; the short-circuit fires first.

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

- `CheckClipboardTransfer` is dbus-policy-pinned to uid 1000
  (admin). Calling it as `work` raises `.AccessDenied`. The test
  must use `runuser -u admin`.
- Empty source/dest silo strings DON'T short-circuit to allow —
  they fall into the cross-silo deny path. Verify the assert by
  reading the actual source cell: it has to start with
  `clipboard_same_silo`, not `clipboard_default_deny`.
