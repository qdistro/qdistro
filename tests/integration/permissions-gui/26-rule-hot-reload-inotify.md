# 26 — Rules reload automatically via inotify on file drop

**What**: with no rule installed, write a new allow-rule YAML
*directly* into `/etc/qdistro/rules.d/` (bypassing `SaveRule`), wait
for the inotify watcher's debounce, verify the `RulesReloaded`
signal fired and the rule is live without any explicit reload call.

**Why**: `SaveRule` calls `reload_rules_from_disk` itself, so its
hot-reload path is trivially exercised by scenarios 24 / 25. The
*inotify* watcher is the failsafe for admins (or future tooling)
that drop files into the directory the old-fashioned way — e.g. a
config-management run, a git pull on `/etc/qdistro/rules.d/`, an
admin editing in vim. Without inotify the broker would silently
serve stale rules until restart. The 200ms debounce coalesces
batch drops; we test by watching for the `RulesReloaded` signal
within a 5-second window.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

$VMEXEC "$VM" 'pkill -f "[d]bus-monitor.*Rules" 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

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

### S1 — attach dbus-monitor on `RulesReloaded`

```bash
$VMEXEC "$VM" 'rm -f /tmp/26-signals.log; \
  setsid dbus-monitor --system \
    "type=signal,interface=org.qdistro.AdminBroker1,member=RulesReloaded" \
    >/tmp/26-signals.log 2>&1 </dev/null &
  echo $! >/tmp/26-monitor.pid'
sleep 1
```

**Assert**: `/tmp/26-signals.log` exists (no signals yet — the
broker emits a startup `RulesReloaded` *before* the monitor attaches
in this ordering, so the log should be header-only at this point).
Quote the log content as your justification.

### S2 — drop the rule file directly (NO `SaveRule`)

```bash
B64=$(base64 -w0 <<'EOF'
cat >/etc/qdistro/rules.d/26-inotify-allow.yaml <<'YAML'
- name: allow-work-test-action-via-inotify
  decision: allow
  match:
    uid: 2000
    action: test.action
  rationale: scenario 26 — direct file drop, inotify must catch it
YAML
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# inotify-debounce is 200ms; broker also has its own coalescing.
# Allow 3s for the watcher to fire, reload, and emit the signal.
sleep 3
```

### S3 — verify `RulesReloaded` fired

```bash
$VMEXEC "$VM" 'kill $(cat /tmp/26-monitor.pid) 2>/dev/null; sleep 0.3; cat /tmp/26-signals.log'
```

**Assert** (textual analysis of `/tmp/26-signals.log`):
- At least one `member=RulesReloaded` signal block appears
  AFTER the dbus-monitor's startup header.
- The signal body's `int32 N` argument is `≥1` (the broker counted
  at least the newly-dropped rule).

### S4 — `ListRules` reports the new rule

```bash
$VMEXEC "$VM" 'runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.ListRules'
```

**Assert**: output contains a dict entry with
`name = "allow-work-test-action-via-inotify"`, `decision = "allow"`,
`action = "test.action"`, `uid = 2000`,
`source_path = "/etc/qdistro/rules.d/26-inotify-allow.yaml"`.

### S5 — rule is live: `work` action gets ALLOWED, no prompt

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
  >/tmp/26-work.log 2>&1 & echo $! >/tmp/26-work.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMEXEC "$VM" 'wait $(cat /tmp/26-work.pid) 2>/dev/null; cat /tmp/26-work.log'
$VMEXEC "$VM" 'dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.GetPending'
```

**Assert**:
- `/tmp/26-work.log` contains `ALLOWED`.
- `GetPending` output is `array []`.

## Teardown

```bash
$VMEXEC "$VM" 'kill $(cat /tmp/26-monitor.pid) 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/26-signals.log /tmp/26-monitor.pid'
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
$VMEXEC "$VM" 'rm -f /tmp/26-work.log /tmp/26-work.pid'
```

## Notes for the runner

- If S3 shows no `RulesReloaded` signal, that's a real broker
  regression — file the FAIL with the log attached. The most common
  break in practice: inotify watcher tearing down on broker reload
  loop and never re-arming.
- The startup `RulesReloaded` emitted from `_emit_startup_rules_reloaded`
  may or may not be captured depending on monitor-attach timing.
  Don't count it. The S2 drop has to trigger a *fresh* signal AFTER
  the monitor was attached.
- The 3-second wait in S2 is generous. The 200ms debounce + tiny
  reload work means a passing scenario typically reports the signal
  within a second; we wait longer to keep the test robust under
  load.
