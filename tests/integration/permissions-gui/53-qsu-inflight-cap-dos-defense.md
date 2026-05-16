# 53 — Per-uid in-flight cap rejects the 5th concurrent qsu

**What**: from `work` (uid 2000), open 5 concurrent qsu
invocations of `/bin/sleep 60` — none of which will be approved.
Exactly ONE of the 5 must be rejected by `qdistro-root-exec` with
an error frame containing `too many in-flight qsu requests for
uid=2000` and NEVER reach the broker. The other 4 reach the broker
as pending rows (subject to the broker-serialization caveat in
Notes — verifying "exactly 4 pending" is racy; verifying "≥1
pending + exactly 1 rejected" is the load-bearing assertion).

**Why**: `qsu/qdistro_root_exec.py` defines
`MAX_INFLIGHT_PER_UID = 4` with a per-uid counter (`_inflight_by_uid`)
guarded by a lock. The reason is in the source comment: "One
hostile uid can open multiple connections and each blocks on
admin approval; without a cap they DoS the whole qsu surface."
A regression that drops the cap would let one compromised user
account flood admin's pending queue and starve every legitimate
caller. The cap is enforced before the broker request is sent
— the rejection should be visible in stderr, NOT as a denied
admin prompt.

This is a headless+broker scenario; the admin app is not used.

## Setup

```bash
VM=${VMNAME:-qd-sudo}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f sleep 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'systemctl restart qdistro-root-exec.socket'
sleep 1

B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Steps

### S1 — fire 5 concurrent qsu /bin/sleep invocations

Each call differs in its argv (`sleep 60 N` where N=1..5) so the
broker can't collapse them. `sleep 60` is long enough that no
invocation exits before the snapshot — the in-flight counter
stays at peak occupancy.

```bash
B64=$(base64 -w0 <<'EOF'
set +e
for i in 1 2 3 4 5; do
  sudo -u work bash -c "/usr/local/bin/qsu /bin/sleep 60 $i \
    >/tmp/53-q$i.out 2>/tmp/53-q$i.err < /dev/null & echo \$! >/tmp/53-q$i.pid"
done
sleep 5
ls -la /tmp/53-q*.pid
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

### S2 — count pending rows on the broker side (must be ≥1, ≤4)

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- python3 - <<'PYEOF'
import dbus, json
bus = dbus.SystemBus()
obj = bus.get_object("com.qdistro.AdminBroker1",
                     "/com/qdistro/AdminBroker1")
iface = dbus.Interface(obj, "com.qdistro.AdminBroker1")
rows = iface.GetPending()
qsu_rows = [r for r in rows if str(r.get("action", "")).startswith("qsu.exec:")]
argvs = sorted(str(r.get("details", {}).get("argv", "")) for r in qsu_rows)
print(json.dumps({"count": len(qsu_rows), "argvs": argvs}, indent=2))
PYEOF
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: `count` is between 1 and 4 inclusive. The 5th argv
(`/bin/sleep 60 5`, or whichever was the unlucky one) must NOT
appear in the argvs list. The exact count varies because the
broker serializes RequestPermissionAs and the snapshot may be
taken before all 4 have landed in `_pending`; see Notes.

### S3 — exactly one of the 5 qsu clients got the in-flight error

```bash
B64=$(base64 -w0 <<'EOF'
set +e
for i in 1 2 3 4 5; do
  if grep -q "too many in-flight" /tmp/53-q$i.out /tmp/53-q$i.err 2>/dev/null; then
    echo "REJECTED:$i"
  fi
done
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: output contains exactly one `REJECTED:N` line. The
client (qsu.py)'s `_stream` consumes the `error` JSON frame and
writes it to stderr prefixed with `qsu:`; the message includes
`too many in-flight qsu requests for uid=2000`.

### S4 — the rejected qsu exited promptly (rc=1, no hang)

```bash
B64=$(base64 -w0 <<'EOF'
set +e
for i in 1 2 3 4 5; do
  pid=$(cat /tmp/53-q$i.pid 2>/dev/null)
  if [ -z "$pid" ]; then continue; fi
  if grep -q "too many in-flight" /tmp/53-q$i.out /tmp/53-q$i.err 2>/dev/null; then
    if kill -0 "$pid" 2>/dev/null; then
      echo "REJECTED_$i:still_running pid=$pid"
    else
      wait "$pid" 2>/dev/null
      echo "REJECTED_$i:exited rc=$?"
    fi
  fi
done
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: the line for the rejected invocation is
`REJECTED_N:exited rc=1`. NOT `still_running` — that would mean
the qsu client is hanging on the socket after the server already
sent an error+exit frame.

### S5 — kill the 4 surviving pending qsu clients to clean up

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- python3 - <<'PYEOF'
import dbus
bus = dbus.SystemBus()
obj = bus.get_object("com.qdistro.AdminBroker1",
                     "/com/qdistro/AdminBroker1")
iface = dbus.Interface(obj, "com.qdistro.AdminBroker1")
for r in iface.GetPending():
    if str(r.get("action", "")).startswith("qsu.exec:"):
        try:
            iface.DecideRequest(int(r["id"]), "deny", "once")
        except Exception as e:
            print(f"deny rid={r['id']} failed: {e}")
PYEOF
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; pkill -u work -f sleep 2>/dev/null; true'
```

**Assert**: post-cleanup `GetPending` returns no `qsu.exec:` rows.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f sleep 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/53-*.out /tmp/53-*.err /tmp/53-*.pid'
B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Notes for the runner

- The cap is per UID, so the 4 invocations counted under uid=2000
  matter. A 5th invocation from a DIFFERENT uid (e.g. work2 if
  you spun the gui VM with both work + work2) would land its own
  pending row — the limit is not global. Out of scope for this
  scenario.
- The release path on socket close decrements the counter
  (`_inflight_release` in the finally block). If the 4 pending
  invocations time out at the qsu client and disconnect, the
  count drops back below the cap; a 6th invocation would land
  successfully. We don't test that timing — the scenario asserts
  the cap at peak occupancy.
- If S3 reports zero `REJECTED:N` lines, either the cap is too
  high (MAX_INFLIGHT_PER_UID was bumped without updating the
  scenario), or the 5 concurrent calls are landing too slowly to
  collide. Tighten the launch loop or set the qsu argv to a
  longer-running command.

### Broker-serialization caveat for S2

The broker's single-threaded glib mainloop processes
`RequestPermissionAs` calls one at a time, and each call does a
~50ms `exe_sha256` computation in `_read_proc_layered`. With 4
concurrent calls, the snapshot at "sleep 5" may show only 1-2
pending rows even though all 4 will eventually arrive. Empirically
(validated 2026-05-16 on qd-sudo): `count=1` is the common
observation after a fresh broker restart, with the remaining rows
landing 8-15 seconds later. The load-bearing claim is the
**in-flight cap fires at #5** (S3), not the pending count at a
specific instant. Future broker work to parallelise
`_read_proc_layered` would tighten the observable window; until
then, treat 1-4 pending as PASS for S2.
