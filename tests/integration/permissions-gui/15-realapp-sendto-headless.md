# 15 — cross-user send-to between two real qnotebook instances, headless

**What**: two real qnotebook processes, one running as `work`
(uid 2000) on `/home/work/testnb`, one as `work2` (uid 3000) on
`/home/work2/testnb`. Each hosts the `qdistro_sendto` plugin and
claims `org.qdistro.Qnotebook.uid<N>` on its session bus.
Broker's `ListReceivers` returns both entries. `work` asks the
broker to relay text to `work2`'s instance via `RelayMessage`;
admin approves via `dbus-send`; we assert:

- both receivers are discoverable,
- the target's SDK `GetLastReceived` reflects the delivered tuple,
- audit rows are correct for both directions,
- the approvals cache never records a send-to entry.

**Why**: 11 proved the wire path with stubs. 15 proves the same
wire path carries real apps via the plugin pattern. Sister
scenarios 16 (visual approve) and 17 (deny) exercise the admin
GUI surface.

** scope note**: the qterminator side is deferred pending
. When that clears,
scenarios should be extended to exercise qterminator ↔ qnotebook.
's thesis — general protocol, plugin-based participation —
is proven here with qnotebook running under two uids.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1957}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

# Fresh broker state.
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

# Ensure both user-relays are up with linger.
$VMEXEC "$VM" 'loginctl enable-linger work work2'
$VMEXEC "$VM" 'systemctl start user@2000.service user@3000.service'
for _ in 1 2 3 4 5; do
 $VMEXEC "$VM" 'test -S /run/user/2000/bus && test -S /run/user/3000/bus' && break
 sleep 1
done
$VMEXEC "$VM" 'systemctl --machine=work@.host --user restart qdistro-user-relay.service'
$VMEXEC "$VM" 'systemctl --machine=work2@.host --user restart qdistro-user-relay.service'

# Drop the stub notepads so ListReceivers assertions stay
# uncluttered. The bootstrap enables them by default; stopping just
# for this scenario is fine.
$VMEXEC "$VM" 'systemctl --machine=work@.host --user stop qstub-notepad.service 2>/dev/null || true'
$VMEXEC "$VM" 'systemctl --machine=work2@.host --user stop qstub-notepad.service 2>/dev/null || true'

# Launch the two qnotebook instances offscreen (no live display
# needed for the headless path). Uses the bootstrap-seeded notebook
# dirs and QSettings where plugins_enabled=[qdistro_sendto].
B64=$(base64 -w0 <<'EOF'
set -e
pkill -u work -f "python3 -m zim_qt" 2>/dev/null || true
pkill -u work2 -f "python3 -m zim_qt" 2>/dev/null || true
sleep 1
rm -f /home/work/testnb/.zim-qt/lock /home/work2/testnb/.zim-qt/lock
setsid runuser -u work -- env \
 XDG_RUNTIME_DIR=/run/user/2000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/2000/bus \
 QT_QPA_PLATFORM=offscreen \
 PYTHONUNBUFFERED=1 \
 /usr/local/bin/qnotebook /home/work/testnb \
 </dev/null >/tmp/15-qnb-work.log 2>&1 &
setsid runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 QT_QPA_PLATFORM=offscreen \
 PYTHONUNBUFFERED=1 \
 /usr/local/bin/qnotebook /home/work2/testnb \
 </dev/null >/tmp/15-qnb-work2.log 2>&1 &
sleep 5
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Steps

### S1 — broker discovers both real qnotebook instances

```bash
$VMEXEC "$VM" 'dbus-send --system --print-reply \
 --dest=org.qdistro.AdminBroker1 \
 /org/qdistro/AdminBroker1 \
 org.qdistro.AdminBroker1.ListReceivers'
```

**Assert**:
- `int32 2000` paired with `"org.qdistro.Qnotebook.uid2000"` +
 friendly `"Qnotebook"`.
- `int32 3000` paired with `"org.qdistro.Qnotebook.uid3000"` +
 friendly `"Qnotebook"`.

### S2 — work sends to work2, admin approves

```bash
B64=$(base64 -w0 <<'EOF'
set -e
runuser -u work -- dbus-send --system --print-reply \
 --dest=org.qdistro.AdminBroker1 \
 /org/qdistro/AdminBroker1 \
 org.qdistro.AdminBroker1.RelayMessage \
 int32:3000 \
 string:org.qdistro.Qnotebook.uid3000 \
 string:text/plain \
 string:phase4_real_to_real \
 > /tmp/15-s2-relay.out 2>&1 &
echo $! > /tmp/15-s2-relay.pid
sleep 1

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
 int32:$RID string:allow string:once >/tmp/15-s2-decide.out 2>&1
wait $(cat /tmp/15-s2-relay.pid) || true
echo "=== relay.out ==="
cat /tmp/15-s2-relay.out
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**:
- `request_id=<small integer>` printed.
- `relay.out` ends with `method return` (no `Error` prefix).

### S3 — work2's qnotebook receiver saw the payload

```bash
$VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply \
 --dest=org.qdistro.Qnotebook.uid3000 \
 /org/qdistro/App1 \
 org.qdistro.App1.GetLastReceived'
```

**Assert**: output includes `string "[text/plain] phase4_real_to_real"`.

### S4 — reverse direction: work2 → work

```bash
B64=$(base64 -w0 <<'EOF'
set -e
runuser -u work2 -- dbus-send --system --print-reply \
 --dest=org.qdistro.AdminBroker1 \
 /org/qdistro/AdminBroker1 \
 org.qdistro.AdminBroker1.RelayMessage \
 int32:2000 \
 string:org.qdistro.Qnotebook.uid2000 \
 string:text/plain \
 string:echo_reverse \
 > /tmp/15-s4-relay.out 2>&1 &
echo $! > /tmp/15-s4-relay.pid
sleep 1

RID=$(dbus-send --system --print-reply \
 --dest=org.qdistro.AdminBroker1 \
 /org/qdistro/AdminBroker1 \
 org.qdistro.AdminBroker1.GetPending 2>&1 \
 | grep -A1 '"id"' | grep int32 | head -1 | awk '{print $NF}')
runuser -u admin -- dbus-send --system --print-reply \
 --dest=org.qdistro.AdminBroker1 \
 /org/qdistro/AdminBroker1 \
 org.qdistro.AdminBroker1.DecideRequest \
 int32:$RID string:allow string:once >/tmp/15-s4-decide.out 2>&1
wait $(cat /tmp/15-s4-relay.pid) || true
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

$VMEXEC "$VM" 'runuser -u work -- env \
 XDG_RUNTIME_DIR=/run/user/2000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/2000/bus \
 dbus-send --session --print-reply \
 --dest=org.qdistro.Qnotebook.uid2000 \
 /org/qdistro/App1 \
 org.qdistro.App1.GetLastReceived'
```

**Assert**: output includes `string "[text/plain] echo_reverse"`.

### S5 — audit rows correct for both directions

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, action, decision, scope, source, approver_uid
 FROM audit
 WHERE action LIKE 'app.send-to:%Qnotebook%'
 ORDER BY id DESC LIMIT 2;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**:
- Two rows (most recent first):
 - `3000|app.send-to:2000:org.qdistro.Qnotebook.uid2000|1|once|prompt|1000`
 - `2000|app.send-to:3000:org.qdistro.Qnotebook.uid3000|1|once|prompt|1000`

### S6 — cache never persisted

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT count(*) FROM approvals WHERE action LIKE 'app.send-to:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
```

**Assert**: output is `0`.

## Teardown

```bash
$VMEXEC "$VM" '
 pkill -u work -f "python3 -m zim_qt" 2>/dev/null || true
 pkill -u work2 -f "python3 -m zim_qt" 2>/dev/null || true
 rm -f /tmp/15-*.out /tmp/15-*.pid /tmp/15-qnb-*.log
'
```

## Notes for the runner

- If S1 shows only one Qnotebook entry, the other instance died
 before claiming its bus name. Check
 `/tmp/15-qnb-{work,work2}.log` for stack traces. Usually:
 - `.zim-qt/lock` has stale root ownership → fix with `chown`,
 - `plugins_enabled` in the uid's QSettings is missing → bootstrap's
 QSettings seed didn't run for that uid,
 - `qdistro_app` isn't on the uid's PYTHONPATH → the system
 site-packages install from bootstrap didn't cover
 `/usr/lib/python3.13/site-packages/qdistro_app/`.
- Scope is forced to `once` by broker policy for `app.send-to:*`;
 passing anything else gets `.ScopeNotPermitted`.
- `GetLastReceived` returns the exact `[kind] payload` string even
 if the editor's visual representation of the received text
 round-tripped through qdoc→markdown in surprising ways — the
 wire state, not the editor state, is the acceptance
 signal.
