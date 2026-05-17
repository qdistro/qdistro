# 41 — Cross-silo receive: default-deny, rule, and MIME glob

**What**: as `admin`, exercise `CheckClipboardReceive` across silos
in four configurations:
1. No rule → `"deny"` + `source='clipboard_receive_default_deny'`.
2. Allow rule (no mime selector) → `"allow"` for any mime.
3. Allow rule with `mime_type: text/*` → `"allow"` for `text/plain`,
   `"deny"` for `image/png` (mime glob doesn't match).
4. Rate-limit: pound the call → `.RateLimited` error.

**Why**: `clipboard.md` §Compositor-mediated gating documents the
**receive-time gate** with per-MIME rules. The MIME glob (`text/*`,
`image/*`, `application/*`) is the load-bearing knob admins use to
permit safe content shapes between silos while blocking risky ones.
A regression that ignored `mime_type:` would allow ALL mimes once a
silo pair is opted in, breaking the granular policy promise. A
regression that didn't apply rate-limiting to receive would let a
compromised qdshell pin the broker on `wl_data_offer.receive`
denials.

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

### S1 — no rule: cross-silo defaults to deny

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckClipboardReceive \
  string:"user1" \
  string:"admin" \
  string:"text/plain" \
  string:"" \
  string:"" \
  string:""
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT action, decision, source FROM audit
  WHERE action='qdistro.clipboard.receive:user1:admin'
  ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**:
- dbus-send reply: `string "deny"`.
- Audit row source starts with `clipboard_receive_default_deny`,
  contains `mime=text/plain`. Decision is `0`.

### S2 — wildcard allow rule: any mime allowed

```bash
B64=$(base64 -w0 <<'EOF'
YAML='- name: allow-user1-to-admin-receive
  decision: allow
  match:
    action: qdistro.clipboard.receive:user1:admin
'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.SaveRule \
  string:"41a-allow.yaml" \
  string:"$YAML"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1

# text/plain: allow
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckClipboardReceive \
  string:"user1" string:"admin" string:"text/plain" \
  string:"" string:"" string:""
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# image/png: also allow (no mime selector on the rule)
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckClipboardReceive \
  string:"user1" string:"admin" string:"image/png" \
  string:"" string:"" string:""
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: both replies are `string "allow"`. Audit rows both
carry `source='clipboard_receive_rule mime=...'`.

### S3 — replace with mime-glob rule (`text/*` only)

```bash
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/41a-allow.yaml'
B64=$(base64 -w0 <<'EOF'
YAML='- name: allow-user1-to-admin-text-only
  decision: allow
  match:
    action: qdistro.clipboard.receive:user1:admin
    mime_type: text/*
'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.SaveRule \
  string:"41b-text-only.yaml" \
  string:"$YAML"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1

# text/plain: match → allow
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckClipboardReceive \
  string:"user1" string:"admin" string:"text/plain" \
  string:"" string:"" string:""
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# text/html: also matches `text/*` → allow
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckClipboardReceive \
  string:"user1" string:"admin" string:"text/html" \
  string:"" string:"" string:""
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# image/png: does NOT match `text/*` → falls through to default deny
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckClipboardReceive \
  string:"user1" string:"admin" string:"image/png" \
  string:"" string:"" string:""
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# application/pdf: also does NOT match `text/*` → deny
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckClipboardReceive \
  string:"user1" string:"admin" string:"application/pdf" \
  string:"" string:"" string:""
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert** (in order):
- `text/plain` → `string "allow"`.
- `text/html` → `string "allow"`.
- `image/png` → `string "deny"`.
- `application/pdf` → `string "deny"`.

Audit cross-check:

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT decision, substr(source, 1, 30) FROM audit
  WHERE action='qdistro.clipboard.receive:user1:admin'
  ORDER BY id DESC LIMIT 4;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**: four most-recent rows are (reverse chronological)
`0|clipboard_receive_default_de`, `0|clipboard_receive_default_de`,
`1|clipboard_receive_rule mime=`, `1|clipboard_receive_rule mime=`.

### S4 — rate-limit: 51st call on same `(uid, action)` raises

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- python3 - <<'PY'
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object("com.qdistro.AdminBroker1",
                       "/com/qdistro/AdminBroker1")
ok = 0; err_name = None
for i in range(60):
    try:
        proxy.CheckClipboardReceive(
            "user1", "admin", "text/plain", "", "", "",
            dbus_interface="com.qdistro.AdminBroker1")
        ok += 1
    except dbus.DBusException as e:
        err_name = e.get_dbus_name()
        print(f"raised at i={i}: {err_name}")
        break
print(f"ok_before_raise={ok}")
PY
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: output contains `ok_before_raise=49` (or `50` depending
on whether prior S3 calls consumed bucket slots) and
`com.qdistro.AdminBroker1.RateLimited`.

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

- The mime-glob test is the load-bearing part. Watch the verdicts
  in S3 carefully: `text/*` is the rule pattern, only `text/...`
  mimes match, anything else falls through to the default-deny.
- S4's `ok_before_raise` count depends on how many calls the
  scenario already drained out of the rate-limit bucket via S2 +
  S3. The exact threshold (50 by default) is fixed; the COUNT
  before raise will differ. A FAIL is "no raise at all" or a
  different `err_name`.
- Audit rows for the *rule-matched* allows carry the full source
  label `clipboard_receive_rule mime=text/plain src_app=(unknown)
  dst_app=(unknown) src_engine=(unknown)` — substr(...,1,30) in
  the SELECT trims to the prefix for stable assertions.
