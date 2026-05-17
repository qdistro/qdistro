# 10 — Qt admin app Cache tab: mouse-driven revoke

**What**: seed the approval cache with four rows spanning different
scopes + uids, open the admin app's Cache tab, click a specific row
to select it, click the Revoke button, verify the row is gone from
the table AND the audit log gained a `source='revoke'` entry for
it.

**Why**: the Cache tab is the admin's only GUI-driven way to unwind
a prior approval (CLI works but requires a terminal). This scenario
exercises the full admin→broker→sqlite+audit path end-to-end
through the GUI, and demonstrates the mouse-intent pattern
(AGENTS.md ) against a table row instead of a simple radio
button.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1
SQL_APPR_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals;
SQL_EOF
)
SQL_AUDIT_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE source='revoke';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_APPR_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_AUDIT_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"

# Seed four approvals — distinct uids and scopes so the target row
# is unambiguous to pick visually.
B64=$(base64 -w0 <<'EOF'
python3 - <<'PY'
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
c.store(2000, "test.action", "/usr/bin/python3.13", "1h", True, 1000)
c.store(2000, "curl.net", "/usr/bin/curl", "24h", True, 1000)
c.store(3000, "net.restart", "", "forever", True, 1000)
c.store(3000, "edit.hosts", "/usr/bin/vim", "forever_exe", True, 1000)
print("seeded 4 rows")
PY
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Steps

### S1 — launch admin app, switch to Cache tab, verify table

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3

B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
 --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# >>> Runner: take over here. Read /tmp/10-qt-cache-revoke-s1a-before.png,
# locate the "Cache" tab header and CLICK IT. See AGENTS.md .
$VMGUI "$VM" screenshot /tmp/10-qt-cache-revoke-s1a-before.png

# (runner clicks "Cache" tab, then takes the after-switch screenshot)
$VMGUI "$VM" screenshot /tmp/10-qt-cache-revoke-s1b-cache-tab.png
```

**Assert (Cache tab active):**
- Post-click screenshot shows the `Cache` tab header active (raised
 or highlighted) and a table with a header row
 (`id uid action scope exe expires`) plus four data rows.
- The four actions visible, in any order: `test.action`,
 `curl.net`, `net.restart`, `edit.hosts`.
- `Revoke` and `Refresh` buttons visible below the table.

### S2 — click the `curl.net` row to select it; click Revoke

```bash
# >>> Runner: look at s1b, CLICK THE ROW whose action is `curl.net`
# (it's the uid=2000, scope=24h row). Any x inside the row bounds
# is fine; clicking the action cell is most unambiguous.
$VMGUI "$VM" screenshot /tmp/10-qt-cache-revoke-s2a-row-selected.png

# >>> Runner: look at s2a, CLICK THE "Revoke" BUTTON.
$VMGUI "$VM" screenshot /tmp/10-qt-cache-revoke-s2b-after-revoke.png
```

**Assert (after revoke):**
- `s2a` shows the `curl.net` row highlighted (row-selected color,
 blue or system selection color).
- `s2b` shows a table with exactly three rows remaining; no
 `curl.net` row. The remaining actions are `test.action`,
 `net.restart`, `edit.hosts` in some order.
- No error dialog appeared (Revoke succeeded silently).

### S3 — verify audit log recorded the revoke

```bash
B64=$(base64 -w0 <<'EOF'
echo "--- remaining cache ---"
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "SELECT action FROM approvals ORDER BY id"
echo "--- revoke audit rows ---"
sqlite3 /var/lib/qdistro/audit/audit.sqlite \
 "SELECT action, source, approver_uid FROM audit WHERE source='revoke' ORDER BY ts"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert:**
- Remaining cache rows: `test.action`, `net.restart`, `edit.hosts`.
 No `curl.net`.
- Audit table contains exactly one new row with action=`curl.net`,
 source=`revoke`, approver_uid=`1000` — the admin app runs as
 `admin` (uid 1000), and the broker records the **calling process's
 uid** as the approver. (The CLI's `qdistro-approvals revoke`
 would record `0` instead since CLI runs as root; different entry
 points, different approver_uid.)

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
SQL_APPR_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals;
SQL_EOF
)
SQL_AUDIT_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE source='revoke';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_APPR_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_AUDIT_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- The Cache tab triggers a refresh every time it's switched to —
 so any seeded rows land as soon as you click the tab; no explicit
 Refresh click is needed in S1.
- The table supports single-row selection; a second click on a
 different row replaces the selection. If you accidentally clicked
 the wrong row, just click the right one before hitting Revoke.
- `approver_uid=1000` in the audit row reflects the admin app
 running as `admin`. The broker records whichever uid made the
 D-Bus call, not whatever uid kicked off the launcher chain.
 Different GUI entry points can yield different approver_uid
 values — CLI `qdistro-approvals revoke` records `0` (root) for
 the same action.
