# 40 — Same-silo clipboard *receive* short-circuits to allow

**What**: as `admin`, call `CheckClipboardReceive("user1", "user1",
"text/plain", ...)`. Expect `"allow"`, a
`source='clipboard_receive_same_silo'` audit row, and no rule
consultation.

**Why**: `clipboard.md` §Compositor-mediated gating documents a
**receive-time gate** (`wl_data_offer.receive`) layered on top of
the set-time `CheckClipboardTransfer` for defense-in-depth. Same-
silo receives must short-circuit just like same-silo transfers —
otherwise apps inside the same silo would block on each
`wl_data_offer.receive`. Scenario 36 covers Transfer's same-silo
path; this scenario pins Receive's equivalent.

This is a headless scenario.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action LIKE 'qdistro.clipboard.receive:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Steps

### S1 — same-silo receive returns `"allow"`

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.CheckClipboardReceive \
  string:"user1" \
  string:"user1" \
  string:"text/plain" \
  string:"" \
  string:"" \
  string:"" \
  boolean:true
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: reply body is `string "allow"`.

### S2 — audit row carries `source='clipboard_receive_same_silo'`

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT action, decision, source FROM audit
  WHERE action='qdistro.clipboard.receive:user1:user1'
  ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**: output is
`qdistro.clipboard.receive:user1:user1|1|clipboard_receive_same_silo mime=text/plain ...`.
The `source` cell starts with `clipboard_receive_same_silo`, contains
`mime=text/plain`, and the trailing `src_app=(unknown)
dst_app=(unknown) src_engine=(unknown)` placeholders are present.

### S3 — same-silo with a *cross-silo* deny rule does NOT block

```bash
B64=$(base64 -w0 <<'EOF'
YAML='- name: scenario-40-deny-user1-receive
  decision: deny
  match:
    action: qdistro.clipboard.receive:user1:user1
  rationale: should never fire — broker short-circuits same-silo receive
'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.SaveRule \
  string:"40-rule.yaml" \
  string:"$YAML"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1

B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.CheckClipboardReceive \
  string:"user1" \
  string:"user1" \
  string:"text/plain" \
  string:"" \
  string:"" \
  string:"" \
  boolean:true
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: reply body is still `string "allow"`. Same-silo
short-circuit fires before the rules engine is consulted.

## Teardown

```bash
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action LIKE 'qdistro.clipboard.receive:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- `CheckClipboardReceive` is admin-only via the dbus policy file;
  calling it as `work` raises `org.freedesktop.DBus.Error.AccessDenied`.
- The `mime_type` argument is required (no default) — pass an empty
  string explicitly if you want to test the "no mime carried"
  branch. Tier-3/4/5 callers in production always have a concrete
  mime (text/plain, image/png, etc.).
