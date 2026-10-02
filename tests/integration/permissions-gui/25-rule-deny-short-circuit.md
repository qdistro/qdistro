# 25 — Declarative `deny` rule short-circuits without prompting

<!-- qci:visual: required -->

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
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'

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
source /tmp/qci-gui-waiters.sh
bg_start 25-work work 'python3 /usr/local/bin/qdistro-test-permission'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# bg_wait, never `wait $(cat X.pid)` — that does not wait in a separate guest
# shell (AGENTS.md, "A backgrounded job"). A TIMEOUT here IS this step's failure.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 25-work 60'
$VMGUI "$VM" screenshot /tmp/25-s3-stillempty.png

$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log 25-work; echo "rc=$(bg_rc 25-work)"'
# Exact pending count (one line, `0` when empty) -- see the Assert below.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; echo "pending=$(broker_pending_count)"'
```

**Assert**:
- `/tmp/25-s3-stillempty.png` shows the same empty admin app.
- `DENIED` is on its own line in the `bg_log 25-work` output printed
  by this step, and the same command's `rc=$(bg_rc 25-work)` is `1`.
  Read both from that bg record before Teardown. Do not re-read
  `/tmp/25-work.log` (or a later copy of that path): Teardown
  deletes it, so the path is empty even when `bg_log` already
  printed `DENIED`.
- No pending request: `broker_pending_count` prints exactly `0`
  (`pending=0` in the command above). In a single guest driver write
  it as a check on that helper's own stdout, e.g.
  `n=$(broker_pending_count) || fail pending-call; [ "$n" = 0 ] || fail "pending=$n"`.
  Do NOT parse `dbus-send --print-reply` text for this: it prints an
  empty array as an indented `array [` / `]` pair under a
  `method return` header, and a hand-written matcher of that layout
  is what turned the 2026-10-01 full run ERROR on a correct broker
  (`grep '^]$'` missed the indented `]`). Do not match the merged
  vm-exec capture either; compare only the helper's stdout.

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

Claim with EXACTLY the lock path the prompt gives,
`/tmp/qci/<slug>/driver.lock`, and use that same `/tmp/qci/<slug>`
directory for `waiting`, the go markers and any guest scratch. Copy it,
do not retype it: in the 2026-10-01 full run a retry driver claimed
`/tmp/qci/qdistro/<slug>/driver.lock`, so it advertised its step in a
stray directory while every go marker went to the real one, and it sat
in `qci_host_step` until the agent killed it (the claim library now
refuses that path with exit 2). A retry needs no new path: once the
previous driver has exited, the same lock is free again.

When driving this as the required single guest shell, S4's audit/cache
queries and Teardown are guest-only work. After the host has captured and
opened S3, release that host step and let the guest shell complete S4,
Teardown, and its final verdict without another `qci_host_step` wait.
The previous full run inserted an unneeded `s4_cleanup` host gate; the
driver then waited for a release the model session never sent and was
terminated before writing its verdict. Keep the S1–S4 checks and cleanup
status authoritative: a missing completion marker remains ERROR.

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
