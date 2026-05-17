# 11 — cross-user send-to, headless happy path

**What**: `work` (uid 2000) asks the broker to relay a payload to
`work2`'s qstub-notepad (uid 3000). Admin approves with scope=`once`.
Assert notepad's document contains the payload and the audit log
has a row with both uids.

**Why**: validates the thesis — admin mediates cross-user
IPC and the wire path works end-to-end — without depending on any
graphical subsystem. Sister scenario 12 does the same from the Qt
sender's GUI.

This scenario is a pure command-line walk; no screenshots, no
keyboard injection. A graphical runner can execute it but the
assertions are all `vm-exec` + sqlite output matching.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

# Fresh broker state so we can assert a clean audit tail.
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

# Make sure both user-relays and the work2 notepad are up. Without
# linger they die between sessions; bootstrap enables linger
# for work + work2 but a non-bootstrap VM may need this:
$VMEXEC "$VM" 'loginctl enable-linger work work2'
$VMEXEC "$VM" 'systemctl start user@2000.service user@3000.service'
# Wait for the sockets.
for _ in 1 2 3 4 5; do
 $VMEXEC "$VM" 'test -S /run/user/2000/bus && test -S /run/user/3000/bus' && break
 sleep 1
done
$VMEXEC "$VM" 'systemctl --machine=work@.host --user restart qdistro-user-relay.service qstub-notepad.service'
$VMEXEC "$VM" 'systemctl --machine=work2@.host --user restart qdistro-user-relay.service qstub-notepad.service'
sleep 1
```

## Steps

### S1 — broker sees both relays

```bash
$VMEXEC "$VM" 'dbus-send --system --print-reply \
 --dest=org.qdistro.AdminBroker1 \
 /org/qdistro/AdminBroker1 \
 org.qdistro.AdminBroker1.ListReceivers'
```

**Assert**:
- Output contains a struct with `int32 2000` and
 `string "org.qdistro.StubNotepad.uid2000"`.
- Output contains a struct with `int32 3000` and
 `string "org.qdistro.StubNotepad.uid3000"`.

### S2 — work sends to work2, admin approves once

```bash
B64=$(base64 -w0 <<'EOF'
set -e
# Kick off the blocking RelayMessage as work in the background;
# capture its stdout/stderr so we can inspect the return code.
runuser -u work -- dbus-send --system --print-reply \
 --dest=org.qdistro.AdminBroker1 \
 /org/qdistro/AdminBroker1 \
 org.qdistro.AdminBroker1.RelayMessage \
 int32:3000 \
 string:org.qdistro.StubNotepad.uid3000 \
 string:text/plain \
 string:hello_from_work_headless \
 > /tmp/sendto-relay.out 2>&1 &
echo $! > /tmp/sendto-relay.pid
sleep 1

# Pick off the pending request id and approve as admin.
RID=$(dbus-send --system --print-reply \
 --dest=org.qdistro.AdminBroker1 \
 /org/qdistro/AdminBroker1 \
 org.qdistro.AdminBroker1.GetPending 2>&1 \
 | grep -A1 '"id"' | grep int32 | head -1 | awk '{print $NF}')
echo "request_id=$RID"
runuser -u admin -- dbus-send --system --print-reply \
 --dest=org.qdistro.AdminBroker1 \
 /org/qdistro/AdminBroker1 \
 org.qdistro.AdminBroker1.DecideRequest \
 int32:$RID string:allow string:once >/tmp/sendto-decide.out 2>&1
wait $(cat /tmp/sendto-relay.pid) || true
echo "=== relay.out ==="
cat /tmp/sendto-relay.out
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**:
- `request_id=` is a small positive integer.
- `/tmp/sendto-relay.out` ends with a `method return` line (no
 `Error` prefix).

### S3 — notepad document contains payload

```bash
$VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply \
 --dest=org.qdistro.StubNotepad.uid3000 \
 /org/qdistro/App1 \
 org.qdistro.App1.GetDocument'
```

**Assert**: output contains `"[text/plain] hello_from_work_headless"`.

### S4 — audit row correct

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, action, decision, scope, source, approver_uid
 FROM audit ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**: output is exactly
`2000|app.send-to:3000:org.qdistro.StubNotepad.uid3000|1|once|prompt|1000`.

### S5 — cache NOT extended

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT count(*) FROM approvals WHERE action LIKE 'app.send-to:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
```

**Assert**: output is `0`. One-shot actions must not persist.

## Teardown

```bash
$VMEXEC "$VM" 'rm -f /tmp/sendto-relay.out /tmp/sendto-relay.pid /tmp/sendto-decide.out'
# Clear the notepad doc for clean re-runs. No D-Bus clear method
# today; restart the service.
$VMEXEC "$VM" 'systemctl --machine=work2@.host --user restart qstub-notepad.service'
```

## Notes for the runner

- If ListReceivers in S1 is empty, the relay daemon(s) aren't running
 — linger likely missing, or dbus policy file absent. Check
 `journalctl _UID=2000 --since '1 minute ago'` for relay boot
 messages and `/etc/dbus-1/system.d/org.qdistro.UserRelay.conf`.
- S2's `wait $(cat /tmp/sendto-relay.pid)` is where a failure would
 hang the scenario. If RelayMessage returns an error DBusException
 (e.g. `.ScopeNotPermitted` or `.Denied`), it appears in
 `/tmp/sendto-relay.out`; the wait exits immediately. A true hang
 means the broker never saw a decide. Check broker journal.
