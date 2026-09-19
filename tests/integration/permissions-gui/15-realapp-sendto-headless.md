# 15 — cross-user send-to between two real qnotebook instances, headless

<!-- qci:visual: none -->

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

This is a headless scenario (`qci:visual: none`). Required
assertions are D-Bus / SDK / sqlite. A rejected, near-black, or
missing screenshot is not ERROR and not FAIL — do not take
frames as a substitute for those oracles
(full-20260918T143937Z-3516587).

** scope note**: the qterminator side is deferred pending
. When that clears,
scenarios should be extended to exercise qterminator ↔ qnotebook.
's thesis — general protocol, plugin-based participation —
is proven here with qnotebook running under two uids.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1957}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

# Fresh broker state. Do NOT await GetPending here: pending
# app.send-to:* only appears after a RelayMessage in S2/S4, once both
# qnotebook instances exist. A pre-launch waiter always times out on a
# valid empty GetPending (literal agents ERROR; looser agents skip it).
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
pkill -u work -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
pkill -u work2 -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
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
# Give both processes a moment to claim their session-bus names.
sleep 5
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# Post-launch readiness: both real receivers visible on the broker.
# This replaces the bogus pre-launch pending-action waits. If either
# qnotebook failed to claim its bus name, S1 will still FAIL with the
# ListReceivers evidence; the poll just avoids a pure startup race.
ready=0
for _ in $(seq 1 30); do
 # CAPTURE THROUGH A FILE, NEVER `$( ... 2>&1 )`: that hands vm-exec's fd 2 to
 # the substitution's pipe, and a virsh/jq descendant that outlives vm-exec
 # holds the pipe -- and this poll iteration -- open forever. The capture is
 # fresh per ITERATION and unlinked before the command runs, so a survivor of
 # iteration N cannot append into iteration N+1's answer.
 _lr=$(mktemp "${TMPDIR:-/tmp}/s15-listrecv.XXXXXXXX") || { echo "ERROR: mktemp failed"; exit 2; }
 exec {_lw}>"$_lr" || { rm -f "$_lr"; echo "ERROR: capture open failed"; exit 2; }
 exec {_lrd}<"$_lr" || { exec {_lw}>&-; rm -f "$_lr"; echo "ERROR: capture open failed"; exit 2; }
 if ! rm -f "$_lr" || [ -e "$_lr" ]; then exec {_lw}>&- {_lrd}<&-; echo "ERROR: capture still named"; exit 2; fi
 $VMEXEC "$VM" 'dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.ListReceivers' >&"$_lw" 2>&"$_lw" {_lw}>&- {_lrd}<&- || true
 exec {_lw}>&-
 # A failed replay is a capture failure, not an empty reply. `|| :` let a
 # nonzero head read as "no receivers yet", which this loop then spins on
 # until it times out and blames readiness (astra, A-astra finding 3).
 out=""
 if ! out=$(head -c 65536 <&"$_lrd"); then
  echo "ERROR: ListReceivers capture replay FAILED; the reply is UNAVAILABLE, not empty" >&2
  exec {_lrd}<&-; exit 2
 fi
 # Both bus-name markers must be INSIDE the prefix; a silent cap would
 # read as "the receivers are not there yet" and spin to the timeout.
 #
 # This detects only a stored suffix the replay did not return. It does NOT
 # establish that the guest's reply was complete: an earlier version compared
 # the size against this shell's `ulimit -f -H`, which is the hard limit where
 # writes obey the soft one, and which in any case is not the limit the WRITER
 # ran under. `>=` against it also failed healthy exact-fit replies outright
 # (astra, A6 findings 1 and 2). Equality at the cap is accepted.
 #
 # The `stat` must be CHECKED and must actually run: removing the inherited
 # -limit block here once took the assignment with it, leaving `$_s15_size`
 # referenced and never set -- which accepted a 70,000-byte reply with
 # `[: : integer expected`, and aborted a HEALTHY short reply under `set -u`
 # (astra, A7 finding 1). This scenario's error convention is exit 2.
 _s15_size=""
 if ! _s15_size=$(stat -Lc %s "/proc/self/fd/$_lrd" 2>/dev/null); then
  echo "ERROR: could not stat the ListReceivers capture; completeness is UNKNOWN, not verified" >&2
  exec {_lrd}<&-; exit 2
 fi
 if [ "$_s15_size" -gt 65536 ]; then
  echo "ERROR: ListReceivers reply (${_s15_size} bytes) exceeded the 65536-byte cap; INCOMPLETE" >&2
  exec {_lrd}<&-; exit 2
 fi
 exec {_lrd}<&-
 if printf '%s' "$out" | grep -q 'org.qdistro.Qnotebook.uid2000' \
  && printf '%s' "$out" | grep -q 'org.qdistro.Qnotebook.uid3000'; then
  ready=1
  break
 fi
 sleep 2
done
[ "$ready" = 1 ] || {
 echo "ERROR: setup — ListReceivers never showed both Qnotebook receivers within ~60s"
 echo "$out"
 exit 2
}
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
# This `wait` is CORRECT, unlike the `$VMEXEC "$VM" 'wait $(cat X.pid)'`
# shape elsewhere: the background job and this wait are in the SAME guest
# shell (one base64 payload piped to one bash), so the pid really is this
# shell's child. Do not "fix" it to bg_wait; do not copy it to a site where
# the producer ran in an earlier $VMEXEC call, where it does not wait at all.
wait $(cat /tmp/15-s2-relay.pid) || true  # qci-flake-allow: cross-shell-wait — same guest shell as the producer
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
# This `wait` is CORRECT, unlike the `$VMEXEC "$VM" 'wait $(cat X.pid)'`
# shape elsewhere: the background job and this wait are in the SAME guest
# shell (one base64 payload piped to one bash), so the pid really is this
# shell's child. Do not "fix" it to bg_wait; do not copy it to a site where
# the producer ran in an earlier $VMEXEC call, where it does not wait at all.
wait $(cat /tmp/15-s4-relay.pid) || true  # qci-flake-allow: cross-shell-wait — same guest shell as the producer
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
 pkill -u work -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
 pkill -u work2 -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
 rm -f /tmp/15-*.out /tmp/15-*.pid /tmp/15-qnb-*.log
'
```

## Notes for the runner

- Setup must **not** call `await_broker_pending_action` before the
  qnotebooks launch: `GetPending` is empty until S2/S4 issue
  `RelayMessage`. Readiness is "both receivers in ListReceivers".
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
