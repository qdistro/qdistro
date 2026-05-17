# 33 — Expired cache rows clear via `RunCacheGc`

**What**: seed a cache row whose `expires_at` is in the past (a
"1h" row that already aged out). Verify `CheckPermission` does NOT
return `"allow"` from the stale row, the periodic GC tick (or an
explicit `RunCacheGc`) deletes the row, and the admin app's Cache
tab reflects the deletion after refresh.

**Why**: scope = "1h" / "24h" only makes sense if the broker actually
enforces the expiry. Without GC, a 1h grant becomes a forever grant.
`permissions.md` §Admin PyQt polkit agent mentions scope picker
options including 1h/24h; the broker's `_gc_tick` is what makes
them honest. This scenario closes the loop on time-bounded scopes
without waiting an hour.

This is mostly headless; one screenshot to verify the Cache tab.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
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

### S1 — seed one already-expired row

```bash
# Bypass the broker and write directly to the sqlite table with an
# expires_at one hour in the past. ApprovalCache.store() uses
# now+ttl; we need now-ttl, so use raw INSERT.
B64=$(base64 -w0 <<'EOF'
python3 - <<'PY'
import sqlite3, time
conn = sqlite3.connect("/var/lib/qdistro/approvals/approvals.sqlite")
conn.execute("""
  INSERT INTO approvals
    (caller_uid, action, match_kind, match_value, decision,
     scope, expires_at, created_at, approver_uid)
  VALUES (2000, 'test.action', 'exe_only', '/usr/bin/python3.13',
          1, '1h', ?, ?, 1000)
""", (int(time.time()) - 3600, int(time.time()) - 7200))
conn.commit()
print("seeded one expired row")
PY
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT id, action, scope, expires_at FROM approvals
  WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
```

**Assert**: one row exists; `expires_at` is a positive integer
less than the current unix time (you can compare against `date +%s`).

### S2 — `CheckPermission` does NOT short-circuit on the expired row

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u work -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.CheckPermission \
  string:"test.action" \
  dict:string:string:"purpose","33-expired"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: reply body is `string "unknown"`. The broker's cache
lookup filtered the expired row out. (If reply is `"allow"`,
expiry is not honoured — a regression.)

### S3 — `RunCacheGc` deletes the row, returns count=1

```bash
$VMEXEC "$VM" 'runuser -u admin -- dbus-send --system --print-reply \
  --dest=com.qdistro.AdminBroker1 \
  /com/qdistro/AdminBroker1 \
  com.qdistro.AdminBroker1.RunCacheGc'

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT count(*) FROM approvals WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
```

**Assert**:
- `RunCacheGc` reply contains `int32 1`.
- Row count after is `0`.

### S4 — admin app Cache tab shows the row is gone

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3

# Runner: OCR click on "Cache" tab.
$VMGUI "$VM" screenshot /tmp/33-s4-cache-tab.png
```

**Assert**: `/tmp/33-s4-cache-tab.png` shows the Cache tab table
header (`id uid action scope exe expires`) and zero data rows for
`test.action`. The table may have unrelated rows from prior
scenarios — only assert the absence of any `test.action` row.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
DELETE FROM audit WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- The broker also runs `_gc_tick` once a minute; this scenario
  uses the explicit `RunCacheGc` D-Bus method to avoid a wall-clock
  wait. A separate, slower scenario could be authored to exercise
  the periodic tick if regression coverage warrants it.
- The S2 assertion is the load-bearing one: the lookup must filter
  expired rows *even before* GC runs. If S2 passes but S3 doesn't,
  the lookup-filter works but GC doesn't physically delete — the
  sqlite file would slowly bloat over months but security would be
  intact. If S2 fails, security is broken.
