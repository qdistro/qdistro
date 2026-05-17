# 07 — qdistro-approvals CLI roundtrip

**What**: exercise the CLI surface end-to-end — `list`, `audit`,
`revoke`, `audit-gc` — against a freshly-seeded cache + audit log,
capture text output, and assert each subcommand's effect is visible
both in its own output and in the broker's sqlite state.

**Why**: the CLI is the primary admin interface when the GUI and
TUI aren't suitable (ssh sessions, scripted rotation, post-incident
forensics). It routes writes through the broker's new
`RevokeApproval`/`RunAuditGc` methods (see commits `e31bb2b` and
`d912d84`). A regression there would silently let sqlite drift
from broker-computed state or bypass the audit trail.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

# Wipe approvals + audit so the test starts from zero. SQL goes
# through a base64 hop because vm-exec's JSON encoder doesn't
# escape embedded `"` (AGENTS.md ).
SQL_APPR_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals;
SQL_EOF
)
SQL_AUDIT_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_APPR_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_AUDIT_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"

# Seed three approvals at different scopes and two audit rows.
B64=$(base64 -w0 <<'EOF'
python3 - <<'PY'
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
from qdistro_admin_audit import AuditLog
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
c.store(2000, "test.action", "/usr/bin/python3.13", "1h", True, 1000)
c.store(2000, "other.action", "/usr/bin/curl", "24h", True, 1000)
c.store(3000, "net.restart", "", "forever", True, 1000)
a = AuditLog("/var/lib/qdistro/audit/audit.sqlite")
a.log(caller_uid=2000, caller_pid=111, caller_exe="/usr/bin/python3.13",
 action="test.action", decision=True, scope="1h",
 source="prompt", approver_uid=1000)
a.log(caller_uid=3000, caller_pid=222, caller_exe="/usr/bin/sudo",
 action="net.restart", decision=False, scope=None,
 source="prompt", approver_uid=1000)
print("seeded")
PY
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Steps

All CLI output is captured to files and read back, so this scenario
has no pixel assertions — just text-shape + sqlite-state checks.

### S1 — `qdistro-approvals list` shows three rows

```bash
$VMEXEC "$VM" 'qdistro-approvals list > /tmp/07-list.txt 2>&1; echo exit=$?'
$VMEXEC "$VM" 'cat /tmp/07-list.txt'
```

**Assert:**
- Exit code `0`.
- Output contains `test.action`, `other.action`, and `net.restart`
 as action names (one per row).
- Output contains the scope labels `argv_exact` (for the 1h/24h
 rows) and `always` (for the forever row), plus a human-readable
 expiry or "never" column — exact format may vary with the CLI's
 `_fmt_expiry` choices, but at minimum the three action names
 must all be present.

### S2 — `qdistro-approvals audit --limit 10` tails audit rows

```bash
$VMEXEC "$VM" 'qdistro-approvals audit --limit 10 > /tmp/07-audit.txt 2>&1; echo exit=$?'
$VMEXEC "$VM" 'cat /tmp/07-audit.txt'
```

**Assert:**
- Exit code `0`.
- Output contains both `test.action` and `net.restart` action names.
- Each row includes a decision token (`allow` / `deny` or `1` / `0`
 depending on the CLI's formatting) — at least one allow and one
 deny should be visible.

### S3 — `qdistro-approvals revoke <id>` routes through the broker

```bash
# Look up the id of the test.action row, revoke it, confirm it's gone.
B64=$(base64 -w0 <<'EOF'
TID=$(sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "SELECT id FROM approvals WHERE action='test.action'")
echo "target id=$TID"
qdistro-approvals revoke "$TID" > /tmp/07-revoke.txt 2>&1
echo "exit=$?"
cat /tmp/07-revoke.txt
echo "---remaining---"
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "SELECT action FROM approvals ORDER BY id"
echo "---audit tail---"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "SELECT action, source, approver_uid FROM audit ORDER BY id DESC LIMIT 3"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert:**
- CLI exit `0`, stdout says `revoked approval id=<TID>`.
- Remaining approvals are exactly `other.action` and `net.restart`;
 `test.action` is gone.
- The latest audit row has `action=test.action`, `source=revoke`,
 `approver_uid=0` (broker accepts root as admin-equivalent; see
 commit `e31bb2b`).

### S4 — `qdistro-approvals revoke 99999` on a missing id exits 1

```bash
$VMEXEC "$VM" 'qdistro-approvals revoke 99999 > /tmp/07-revoke-miss.txt 2>&1; echo exit=$?'
$VMEXEC "$VM" 'cat /tmp/07-revoke-miss.txt'
```

**Assert:**
- Exit code `1`.
- stderr (or combined output) contains `no cached approval with id=99999`.

### S5 — `qdistro-approvals audit-gc --retention-days 0` clears audit

```bash
# Set a retention-days of 0 so every row is older than the cutoff
# and gets deleted.
$VMEXEC "$VM" 'qdistro-approvals audit-gc --retention-days 0 > /tmp/07-audit-gc.txt 2>&1; echo exit=$?'
$VMEXEC "$VM" 'cat /tmp/07-audit-gc.txt'
SQL_COUNT_B64=$(base64 -w0 <<'SQL_EOF'
SELECT COUNT(*) FROM audit;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_COUNT_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert:**
- Exit code `0`.
- stdout reports `deleted N row(s) older than 0d` with N ≥ 3
 (seeded 2 + 1 revoke row from S3).
- Audit row count afterwards is `0`.

## Teardown

```bash
$VMEXEC "$VM" 'rm -f /tmp/07-list.txt /tmp/07-audit.txt /tmp/07-revoke.txt /tmp/07-revoke-miss.txt /tmp/07-audit-gc.txt'
# Leave the broker running; reset its sqlite (same base64 hop as Setup).
SQL_APPR_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals;
SQL_EOF
)
SQL_AUDIT_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_APPR_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_AUDIT_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- This scenario has **no screenshots** — all assertions are
 text-and-sqlite. Still produce the standard report format from
 AGENTS.md, with the "Screenshots" section either omitted or
 populated with `(text-only scenario — no pixel assertions)`.
- If the CLI binary isn't present at `/usr/local/sbin/qdistro-approvals`,
 report ERROR rather than FAIL — it means bootstrap didn't land
 the CLI.
- Exact column layouts for `list` and `audit` are not pinned —
 assertions target the content (action names, decisions) rather
 than formatting, so cosmetic changes to the CLI don't break this.
