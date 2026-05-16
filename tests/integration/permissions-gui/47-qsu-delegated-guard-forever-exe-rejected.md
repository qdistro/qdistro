# 47 — Delegated `forever_exe` on qsu is rejected; sender sees DENIED

**What**: as `work` (uid 2000) invoke `/usr/local/bin/qsu /bin/true`.
The pending request reaches the admin app via the delegated path
(`RequestPermissionAs`). Admin selects the legacy `Forever, only this
exact program` radio (scope=`forever_exe`) and clicks Approve. The
broker rejects the decision with `ScopeNotPermitted` because
`forever_exe` is in `_DELEGATED_FORBIDDEN_SCOPES`. The admin app's
selected scope is unwound (the pending row reappears or remains)
and the qsu sender — still blocking on `WaitForDecision` — does NOT
unblock with allow; on the runner's deny follow-up the sender sees
`request denied` and rc=1.

**Why**: `doc/sudo.md` §"Argv pinning and the delegated path"
documents why qsu loses the broad `forever_exe` scope under
delegation — an unauthenticated future caller at the same uid would
inherit a blanket "run any argv at this exe" grant. The broker's
`_DELEGATED_FORBIDDEN_SCOPES = frozenset(("1h", "24h", "forever",
"forever_exe"))` is the load-bearing guard. Unit tests pin
`DecideRequest` returning the exception; this scenario pins the
end-to-end UX — admin sees the rejection rather than the broker
silently downgrading to `once`.

## Setup

```bash
VM=${VMNAME:-qd-sudo}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'systemctl restart qdistro-root-exec.socket'
sleep 1

SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';
DELETE FROM audit WHERE action LIKE 'qsu.exec:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Steps

### S1 — launch admin app

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
$VMGUI "$VM" screenshot /tmp/47-s1-empty.png
```

**Assert** (`/tmp/47-s1-empty.png`):
- Window titled `admin approvals` visible.
- Pending list empty.

### S2 — invoke qsu as work; pending row carries argv

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /bin/true \
  >/tmp/47-qsu.log 2>&1 & echo $! >/tmp/47-qsu.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/47-s2-pending.png
```

**Assert** (`/tmp/47-s2-pending.png`):
- One pending row, action prefix `qsu.exec:root`.
- Detail pane shows `argv=/bin/true` (or `argv[00]=/bin/true`).
- Scope radios visible include `Forever, only this exact program`
  and `Forever, only this exact argv tuple`.

### S3 — admin picks `forever_exe`, presses Approve

```bash
# OCR /tmp/47-s2-pending.png to find the "Forever, only this exact
# program" radio label; click ~15px left of label-x at label-y-mid.
# (Runner: forever_exe is the 5th radio; forever_argv is the 6th.)
$VMGUI "$VM" screenshot /tmp/47-s3a-forever-exe-selected.png

# Activate window then virsh-send Ctrl+Y for Approve.
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 2
$VMGUI "$VM" screenshot /tmp/47-s3b-after-approve-click.png
```

**Assert**:
- `/tmp/47-s3a-forever-exe-selected.png` shows the `forever_exe`
  radio filled (NOT the default `once`).
- `/tmp/47-s3b-after-approve-click.png` shows either:
  - a QMessageBox / inline error mentioning `ScopeNotPermitted` or
    `not permitted for delegated`, OR
  - the pending row still visible (admin app caught the exception;
    decide didn't persist).
- The qsu sender did NOT unblock as allowed. Check:
  ```bash
  $VMEXEC "$VM" 'cat /tmp/47-qsu.log; kill -0 $(cat /tmp/47-qsu.pid) 2>/dev/null && echo STILL_RUNNING || echo EXITED'
  ```
  Expected: `STILL_RUNNING` (the qsu client is still waiting because
  the decide failed), or the log contains `request denied` if the
  broker recorded the rejection as a deny. Either is acceptable —
  the assertion is "the sender did NOT get a forever_exe allow."

### S4 — admin re-picks `forever_argv` and approves cleanly

The argv-pinned scope IS permitted on the delegated path. This step
proves the broker isn't broken — only the over-broad scope is
rejected.

```bash
$VMGUI "$VM" screenshot /tmp/47-s4a-forever-argv-selected.png
# Runner: click "Forever, only this exact argv tuple" radio (6th).
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 2

$VMEXEC "$VM" 'wait $(cat /tmp/47-qsu.pid) 2>/dev/null; cat /tmp/47-qsu.log; echo "rc=$?"'
```

**Assert**:
- `/tmp/47-s4a-forever-argv-selected.png` shows `forever_argv` radio
  filled.
- `/tmp/47-qsu.log` is empty or has no error; the qsu process has
  exited with rc=0 (`/bin/true` succeeded after admin approval).
- Cache table:
  ```bash
  SQL_B64=$(base64 -w0 <<'SQL_EOF'
  SELECT caller_uid, action, match_kind, match_value, scope
    FROM approvals WHERE action LIKE 'qsu.exec:%';
  SQL_EOF
  )
  $VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
  ```
  Output: one row `2000|qsu.exec:root|argv_exact|...|forever_argv`.
  The match_kind is `argv_exact` (forever_argv → argv_exact in
  `_VALID_SCOPES`), match_value carries the argv JSON.

### S5 — broker-side audit shows the rejected scope was not committed

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT decision, scope, substr(source, 1, 20) FROM audit
  WHERE action='qsu.exec:root'
  ORDER BY id DESC LIMIT 3;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**: the newest audit row records `decision=1`,
`scope=forever_argv`, NOT `scope=forever_exe`. If a row with
`scope=forever_exe` appears at all, it must have `decision=0`
(rejected) — the broker must not silently downgrade an admin's
forever_exe pick to a successful allow under any other scope.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/47-*.log /tmp/47-*.pid'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';
DELETE FROM audit WHERE action LIKE 'qsu.exec:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- The Qt admin app currently surfaces broker exceptions via
  `QMessageBox.critical` on `DecideRequest` failure. If you see a
  plain modal with `ScopeNotPermitted` in the body, that is the
  PASS shape for S3. If the admin app silently swallows the
  exception (no dialog, no log, the row vanishes anyway), report
  FAIL with a screenshot — silent broker-error swallowing is a UX
  regression worth fixing.
- The qsu socket-activation unit (`qdistro-root-exec.socket`) must
  be running for `/usr/local/bin/qsu` to reach the privileged exec
  service. The Setup restart covers this; if S2 yields no pending
  row within 2s, check `/run/qdistro-root-exec/sock` exists.
- `forever_exe` is the LEGACY scope name — `_VALID_SCOPES` maps it
  to `match_kind='exe_only'` (any argv at the same caller_exe). The
  scenario name keeps the UI label `Forever, only this exact
  program` aligned with the radio text in `admin_app/qdistro_admin_app.py`.
