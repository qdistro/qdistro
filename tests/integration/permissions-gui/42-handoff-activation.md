# 42 — `CheckHandoffActivation` default-deny + per-app-id rule

**What**: as `admin`, exercise `CheckHandoffActivation` in three
configurations:
1. Same-silo activation → unconditional `"allow"` + audit source
   `handoff_same_silo`.
2. Cross-silo with no rule → `"deny"` + audit source
   `handoff_default_deny`.
3. Cross-silo with a rule that names `app_id` matching the source's
   wp_security_context_v1 tag → `"allow"`; same call with a
   non-matching `app_id` falls back to default deny.

**Why**: `qdistro/doc/window-handoff.md` and the broker source
document `CheckHandoffActivation` as the cross-silo
`xdg_activation_v1.activate` gate. Default-deny prevents one
silo's app from invisibly stealing focus on another silo's
compositor. Per-`app_id` rule selectors let admin opt in for
specific bridged apps (Firefox in dev-user, Chrome in
work-user, etc.). Without this scenario the policy gates rely on
unit tests that mock `app_id`; a wire-level test pins the actual
behaviour.

This is a headless scenario.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action LIKE 'qdistro.handoff.activate:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Steps

### S1 — same-silo handoff: unconditional allow

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckHandoffActivation \
  string:"user1" \
  string:"user1" \
  string:"org.mozilla.firefox" \
  string:"org.mozilla.firefox" \
  string:""
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT action, decision, substr(source, 1, 25) FROM audit
  WHERE action='qdistro.handoff.activate:user1:user1'
  ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**:
- Reply: `string "allow"`.
- Audit row: `qdistro.handoff.activate:user1:user1|1|handoff_same_silo src_app=or`.

### S2 — cross-silo, no rule: default deny

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckHandoffActivation \
  string:"user1" \
  string:"admin" \
  string:"org.mozilla.firefox" \
  string:"" \
  string:""
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT decision, substr(source, 1, 25) FROM audit
  WHERE action='qdistro.handoff.activate:user1:admin'
  ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**:
- Reply: `string "deny"`.
- Audit row: `0|handoff_default_deny src_a`.

### S3 — install a rule with `app_id: org.mozilla.firefox`, retest

```bash
B64=$(base64 -w0 <<'EOF'
YAML='- name: allow-firefox-handoff-user1-to-admin
  decision: allow
  match:
    action: qdistro.handoff.activate:user1:admin
    app_id: org.mozilla.firefox
  rationale: scenario 42 — per-app handoff opt-in
'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.SaveRule \
  string:"42-allow-firefox.yaml" \
  string:"$YAML"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1

# Same call as S2: now allows because app_id matches.
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckHandoffActivation \
  string:"user1" \
  string:"admin" \
  string:"org.mozilla.firefox" \
  string:"" \
  string:""
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# Different source app_id: rule does NOT match, default-deny applies.
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckHandoffActivation \
  string:"user1" \
  string:"admin" \
  string:"com.google.Chrome" \
  string:"" \
  string:""
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**:
- First reply (`firefox`): `string "allow"`.
- Second reply (`chrome`): `string "deny"`.

Audit cross-check:

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT decision, substr(source, 1, 25), rule_path FROM audit
  WHERE action='qdistro.handoff.activate:user1:admin'
  ORDER BY id DESC LIMIT 2;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**: two newest rows (reverse chrono):
- `0|handoff_default_deny src_a|` (chrome, no rule match).
- `1|handoff_rule src_app=org.m|/etc/qdistro/rules.d/42-allow-firefox.yaml` (firefox, rule match with rule_path).

### S4 — non-admin caller is denied at bus-policy level

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u work -- dbus-send --system --print-reply --reply-timeout=2000 \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckHandoffActivation \
  string:"user1" string:"admin" string:"x" string:"" string:"" 2>&1
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: output contains `org.freedesktop.DBus.Error.AccessDenied`
or `com.qdistro.AdminBroker1.AccessDenied` — handoff activation is
admin-only by both bus policy and broker code.

## Teardown

```bash
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action LIKE 'qdistro.handoff.activate:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- `CheckHandoffActivation` signature is `sssss` (5 strings):
  source_silo, dest_silo, source_app_id, dest_app_id,
  source_sandbox_engine. dbus-send order matters.
- The `dest_app_id` is recorded in the audit row but NOT used by
  the rule matcher today — only `source_app_id` (rule `app_id:`
  selector) is consulted. This is intentional per the broker
  source comment: rules express "who's allowed to send" not "who's
  allowed to receive" for handoff.
- A `sandbox_engine:` selector would work the same way once we
  wire test fixtures carrying that field. Skipping for this
  scenario; covered in unit tests under `test_broker_handoff_*`.
