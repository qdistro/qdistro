# 29 — `CheckPermission` returns `"unknown"` without prompting

**What**: with no rule and an empty cache for the action,
synchronously call `CheckPermission("test.action", {})` as `work`.
Expect string reply `"unknown"`. Verify `GetPending` is still empty
afterwards (no prompt was enqueued) and the audit log gained NO
row for this action (CheckPermission is read-only and silent on
the unknown path).

**Why**: `permissions.md` §Two broker entry points describes
`CheckPermission` as the fast-path for never-block-the-user
callers. The `"unknown"` return is the load-bearing escape hatch:
the caller refuses the immediate attempt without spending admin
attention. Any regression that (a) enqueues a prompt on unknown,
(b) writes an audit row on unknown, or (c) returns a default
verdict instead of `"unknown"` would break the caller contract.

This is a headless scenario.

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

### S1 — `work` calls `CheckPermission` synchronously

```bash
# CheckPermission has signature sa{sv} -> s; for an empty details
# dict pass dict:string:variant:string:"" (dbus-send requires at
# least one entry for a{sv}, but the broker accepts an absent
# 'purpose' key — the smallest legal payload is below).
B64=$(base64 -w0 <<'EOF'
runuser -u work -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckPermission \
  string:"test.action" \
  dict:string:string:"purpose","scenario-29-fastpath"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: the dbus-send reply body is `string "unknown"`.

### S2 — no prompt was enqueued; no audit row was written

```bash
$VMEXEC "$VM" 'dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.GetPending'

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT count(*) FROM audit WHERE action='test.action';
SELECT count(*) FROM approvals WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
```

**Assert**:
- `GetPending` output is `array []`.
- Audit count is `0`.
- Approvals count is `0`.

### S3 — seed an allow rule; CheckPermission now returns `"allow"`

```bash
B64=$(base64 -w0 <<'EOF'
YAML='- name: scenario-29-allow
  decision: allow
  match:
    uid: 2000
    action: test.action
'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.SaveRule \
  string:"29-allow.yaml" \
  string:"$YAML"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1

B64=$(base64 -w0 <<'EOF'
runuser -u work -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckPermission \
  string:"test.action" \
  dict:string:string:"purpose","scenario-29-rule"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: dbus-send reply is `string "allow"`. (The shape change
across S1 → S3 — same call, different verdict because a rule was
installed — proves the rule engine drives the fast-path.)

### S4 — seed a cache row; CheckPermission returns the cached decision

```bash
# Drop the rule again so the only signal is the cache row.
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.ReloadRules'
sleep 1

B64=$(base64 -w0 <<'EOF'
python3 - <<'PY'
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
c.store(2000, "test.action", "/usr/bin/python3.13", "forever_exe", False, 1000)
print("seeded one DENY cache row")
PY
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

B64=$(base64 -w0 <<'EOF'
runuser -u work -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckPermission \
  string:"test.action" \
  dict:string:string:"purpose","scenario-29-cache"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: reply is `string "deny"`. The cache row's decision
(0 = deny) is reflected by the synchronous fast-path; rules took
precedence in S3, cache takes precedence in S4 — but both override
the default `"unknown"`.

## Teardown

```bash
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
DELETE FROM audit WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- `dbus-send` builds the a{sv} dict from `dict:string:string:K,V`
  pairs. The single key/value pair we pass is enough to satisfy
  dbus-send's parser; the broker tolerates any well-typed details
  dict.
- `CheckPermission` is silent on **every** path — unknown, rule-
  match, and cache-hit all return without writing an audit row
  (see `broker/qdistro_admin_broker.py` §`CheckPermission`). The
  `_VALID_SOURCES`-shaped audit rows for `source='rule'` /
  `source='cache'` come from `RequestPermission` / `_enqueue`, not
  from the fast-path. This scenario therefore asserts only the
  *no-prompt-no-cache* contract; readers should not infer that
  rule/cache fast-path hits leave audit rows.
