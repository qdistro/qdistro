# 31 — Fire-and-forget `RequestPermission` queues without a waiter

<!-- qci:visual: required -->

**What**: from `work`, call `RequestPermission(action, details)`
synchronously (no `WaitForDecision` follow-up). Verify (a) the
call returns a `request_id` immediately, (b) the request appears
in `GetPending`, (c) the Qt admin app shows the row, (d) when
admin approves, the broker emits `RequestDecided` to anyone
listening and writes the cache row — even though no caller is
waiting on the decision.

**Why**: `permissions.md` §S4 documents the fire-and-forget
pattern — "please change the policy so next time this works".
Today's `qdistro-test-permission` always waits, so existing
scenarios all exercise the waiter path. A regression where
`RequestPermission` requires a paired waiter (e.g. the queue
entry gets garbage-collected if no one is waiting after N ms)
would silently lose policy-improvement requests. This scenario
pins the queue-survives-without-waiter contract.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui
GUEST_TMP=/tmp/qci-qdistro_tests_integration_permissions-gui_31-fire-and-forget-request.md
ARTIFACT_DIR=${QCI_GUI_ARTIFACT_DIR:?QCI_GUI_ARTIFACT_DIR is required}

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" "mkdir -p $GUEST_TMP; rm -f $GUEST_TMP/*"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_system_unit_active qdistro-admin-broker.service 15 1'

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

### S1 — `work` fires `RequestPermission`, returns immediately

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u work -- dbus-send --system --print-reply --reply-timeout=2000 \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.RequestPermission \
  string:"test.action" \
  dict:string:string:"purpose","fire-and-forget-31" \
  2>&1 | tee /tmp/qci-qdistro_tests_integration_permissions-gui_31-fire-and-forget-request.md/31-s1.out
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" "cat $GUEST_TMP/31-s1.out" > "$ARTIFACT_DIR/31-s1.out"
```

**Assert**:
- `$GUEST_TMP/31-s1.out` body contains `int32 N` where N is a positive
  integer. Capture N as `${RID}` for later steps.
- No `Error` prefix; reply is `method return`.

### S2 — `GetPending` shows the row; admin app shows it too

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; await_x11_window "admin approvals" admin :0 20 1' || {
  $VMEXEC "$VM" 'cat /home/admin/.local/state/qdistro/admin-app.log 2>&1' > "$ARTIFACT_DIR/admin-app-launch-failure.log"
  echo "ERROR: admin approvals window did not map; see admin-app-launch-failure.log" >&2
  exit 1
}
$VMGUI "$VM" screenshot "$ARTIFACT_DIR/s2-app-pending.png"

$VMEXEC "$VM" 'dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.GetPending'
```

**Assert**:
- `GetPending` output contains a struct with `"action" = "test.action"`
  and `"uid" = 2000`. The `id` field equals `${RID}` from S1.
- `$ARTIFACT_DIR/s2-app-pending.png` shows one row in the Pending list
  reading `uid=2000  test.action`.

### S3 — wait 10 seconds, queue does NOT garbage-collect

```bash
# This delay is the behavior under test: a pending fire-and-forget request must
# remain queued without a waiter for at least this long.
sleep 10
$VMEXEC "$VM" 'dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.GetPending'
$VMGUI "$VM" screenshot "$ARTIFACT_DIR/s3-app-still-pending.png"
```

**Assert**:
- `GetPending` still returns the same struct (same `id=${RID}`).
- `$ARTIFACT_DIR/s3-app-still-pending.png` still shows the row.

### S4 — admin approves; cache row appears; `RequestDecided` fires

```bash
# Self-contained: this block may run in a fresh shell (a recovery rerun),
# so it defines its own GUEST_TMP default and scripts.
#
# The monitor records its OWN pid, then execs. Never capture it with
# `& echo \$!`: re-quoted by a driver, `$!` expands in the driver shell and
# names the harness's live claim watcher, which the stop would then kill.
# Readiness is a positive control, not a sleep: the monitor also matches a
# private QciProbe.Ready signal, and it counts as subscribed only once a
# probe carrying this run's token appears in ITS log.
#
# ORDER IS PART OF THE TEST: run this monitor start, and see
# MONITOR_READY + REQUEST_STILL_PENDING, BEFORE any approval action (the
# "1 hour" click, Ctrl+Y, or any host step that performs them). A driver
# that approves first and starts the monitor afterwards loses the signal.
GUEST_TMP=${GUEST_TMP:-/tmp/qci-qdistro_tests_integration_permissions-gui_31-fire-and-forget-request.md}
MON_START_B64=$(base64 -w0 <<'EOF'
T=$1
rm -f "$T/31-decided.log" "$T/31-monitor.pid"
setsid -f /bin/sh -c 'echo $$ >"$1"; exec dbus-monitor --system "$2" "$3" >"$4" 2>&1 </dev/null' \
  _ "$T/31-monitor.pid" \
  'type=signal,interface=org.qdistro.AdminBroker1,member=RequestDecided' \
  'type=signal,interface=org.qdistro.QciProbe,member=Ready' \
  "$T/31-decided.log"
tok=ready-$$-$RANDOM
for _ in $(seq 1 100); do
  dbus-send --system --type=signal /org/qdistro/QciProbe org.qdistro.QciProbe.Ready "string:$tok" 2>/dev/null
  grep -q "$tok" "$T/31-decided.log" 2>/dev/null && break
  sleep 0.1
done
p=$(cat "$T/31-monitor.pid" 2>/dev/null)
if grep -q "$tok" "$T/31-decided.log" 2>/dev/null && [ "$(cat /proc/$p/comm 2>/dev/null)" = dbus-monitor ]; then
  echo "MONITOR_READY pid=$p"
else
  echo "MONITOR_NOT_READY pid=[$p]"
fi
# Ordering proof: the monitor is only useful if it came up BEFORE the
# approval. The request must still be pending now; if it is not, the
# approval already happened and an empty signal log proves nothing.
if dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
     /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.GetPending 2>&1 \
     | grep -q 'test.action'; then
  echo "REQUEST_STILL_PENDING"
else
  echo "REQUEST_ALREADY_DECIDED"
fi
EOF
)
MON_STOP_B64=$(base64 -w0 <<'EOF'
T=$1
p=$(cat "$T/31-monitor.pid" 2>/dev/null)
case $p in ''|*[!0-9]*) echo "MONITOR_STOP: no pid recorded" >&2; exit 0 ;; esac
if [ "$(cat /proc/$p/comm 2>/dev/null)" = dbus-monitor ]; then
  kill "$p"
elif [ -d /proc/$p ]; then
  echo "MONITOR_STOP: pid $p is $(cat /proc/$p/comm), not dbus-monitor; NOT killing" >&2
fi
exit 0
EOF
)
$VMEXEC "$VM" "echo $MON_START_B64 | base64 -d | bash -s $GUEST_TMP"

# Approve with 1h scope via admin app. Window is already focused.
B64=$(base64 -w0 <<'EOF'
set -eu
source /tmp/qci-gui-waiters.sh
await_x11_window "admin approvals" admin :0 10 1
wid=$(runuser -u admin -- env DISPLAY=:0 \
  xdotool search --onlyvisible --name "admin approvals" | head -n1)
timeout 10s runuser -u admin -- env DISPLAY=:0 \
  xdotool windowactivate --sync "$wid"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# Default scope is "once"; press Ctrl+Y to approve. ("once" means
# no cache row will be written; for this scenario we want a cache
# row, so the runner first clicks "1 hour" via OCR — see
# AGENTS.md  for the click pattern.)
# (runner OCR-clicks the "1 hour" radio)
sleep 0.5
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 1

$VMEXEC "$VM" "echo $MON_STOP_B64 | base64 -d | bash -s $GUEST_TMP; sleep 0.3; cat $GUEST_TMP/31-decided.log" \
  | tee "$ARTIFACT_DIR/31-decided.log"

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT count(*) FROM approvals WHERE action='test.action' AND caller_uid=2000;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
```

**Assert**:
- The monitor start printed `MONITOR_READY pid=<N>` and
  `REQUEST_STILL_PENDING`, BEFORE the "1 hour" click and Ctrl+Y.
  `MONITOR_NOT_READY` or `REQUEST_ALREADY_DECIDED` is a harness ERROR
  (the monitor was not listening before the approval, so an empty signal
  log would prove nothing), not a product FAIL. (The log also holds the
  `QciProbe` readiness signal; ignore it.)
- `$ARTIFACT_DIR/31-decided.log` contains one `member=RequestDecided`
  signal block; its args are `int32 ${RID}` then `string "allow"`.
- Cache count is `1` — the broker wrote the row even though no
  one was waiting on `WaitForDecision`.
- Admin app pending list is now empty.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
# Self-contained (Teardown may run in a fresh shell after a failure).
GUEST_TMP=${GUEST_TMP:-/tmp/qci-qdistro_tests_integration_permissions-gui_31-fire-and-forget-request.md}
MON_STOP_B64=$(base64 -w0 <<'EOF'
T=$1
p=$(cat "$T/31-monitor.pid" 2>/dev/null)
case $p in ''|*[!0-9]*) echo "MONITOR_STOP: no pid recorded" >&2; exit 0 ;; esac
if [ "$(cat /proc/$p/comm 2>/dev/null)" = dbus-monitor ]; then
  kill "$p"
elif [ -d /proc/$p ]; then
  echo "MONITOR_STOP: pid $p is $(cat /proc/$p/comm), not dbus-monitor; NOT killing" >&2
fi
exit 0
EOF
)
$VMEXEC "$VM" "echo $MON_STOP_B64 | base64 -d | bash -s $GUEST_TMP"
$VMEXEC "$VM" "rm -rf $GUEST_TMP"
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

## Notes for the runner

- The S3 sleep is intentionally long (10s) to catch a hypothetical
  "drop queue entries with no waiter after T seconds" regression
  with margin. The broker today does NOT GC pending entries
  proactively — they live until admin decides or until the broker
  is restarted.
- This scenario does not exercise the SDK side of fire-and-forget
  (`qdistro_app.request_async(...)` once that lands). The wire
  contract (RequestPermission alone with no waiter) is sufficient
  to pin the broker behaviour the SDK depends on.
