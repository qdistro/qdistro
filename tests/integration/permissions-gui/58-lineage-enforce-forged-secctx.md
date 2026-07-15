# 58 — permission lineage: forged `sandbox_engine` denied under enforce

**What**: an admin rule pre-approves an action for
`sandbox_engine: qdistro.tier1`. A `work` caller with **no launch
record** forges `sandbox_engine=qdistro.tier1` in the `CheckPermission`
details dict. Prove the three lineage postures:

- **Shadow mode** (`lineage_enforce=false`, the default): the forged
  claim still matches the rule → `"allow"` (legacy, lineage-broken
  behaviour — preserved so rollout breaks nothing).
- **Enforce mode** (`lineage_enforce=true`): the same forged claim from
  an unregistered caller resolves to the `unknown` subject (empty
  sandbox_engine) → the rule no longer matches → `"unknown"`. Finding
  **P0-1** is closed.
- **Enforce + registered**: after a root launcher calls `RegisterLaunch`
  for the caller's live pid, the broker supplies the *launcher-attested*
  `sandbox_engine` and the rule matches → `"allow"` — even when the
  caller passes **no** secctx at all.

Plus the `RegisterLaunch` authorization boundary: a non-root caller is
refused with `AccessDenied`.

**Why**: `issues/qdistro/permission-lineage-findings.md` finding P0-1 —
`CheckPermission` / `RequestPermission` matched rules on the
client-supplied `app_id` / `sandbox_engine`, callable by any uid, with no
cross-check against the live process. This scenario is the end-to-end
proof that enforce mode replaces the forgeable claim with the
launcher-attested value and fails closed for unregistered/forged callers.

This is a headless scenario. It requires the broker build that ships
`qdistro_proc_identity.py`, `qdistro_launch_record.py`,
`qdistro_resolver.py`, and the `RegisterLaunch` method.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

# Clean slate: drop scenario rules + any prior broker.conf flag.
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'pkill -u work -f lineage-helper 2>/dev/null; true'
$VMEXEC "$VM" 'test -f /etc/qdistro/broker.conf && sed -i "/lineage_enforce/d" /etc/qdistro/broker.conf || true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

# Admin authors the tier-1 allow rule.
B64=$(base64 -w0 <<'EOF'
YAML='- name: scenario-58-tier1
  decision: allow
  match:
    action: org.qdistro.lineage.test
    sandbox_engine: qdistro.tier1
'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.SaveRule \
  string:"58-tier1.yaml" string:"$YAML"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1
```

## Steps

### S1 — shadow mode: forged `sandbox_engine` still matches (legacy)

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u work -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.CheckPermission \
  string:"org.qdistro.lineage.test" \
  dict:string:string:"sandbox_engine","qdistro.tier1"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: reply is `string "allow"`. (Default broker has
`lineage_enforce=false`; the claim is trusted as before.)

### S2 — switch to enforce mode

```bash
$VMEXEC "$VM" 'install -d -m 0755 /etc/qdistro'
$VMEXEC "$VM" 'grep -q "^lineage_enforce" /etc/qdistro/broker.conf 2>/dev/null \
  || echo "lineage_enforce = true" >> /etc/qdistro/broker.conf'
# Capture a journal cursor, restart, then wait for the startup posture line to
# appear AFTER the cursor — proves THIS restart logged it, not a stale line.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh
cur=$(journalctl -u qdistro-admin-broker.service -n0 --show-cursor 2>/dev/null | sed -n "s/^-- cursor: //p")
[ -n "$cur" ] || { echo "FAIL: could not capture journal cursor"; exit 1; }
systemctl restart qdistro-admin-broker.service
await_journal_line_after_cursor "$cur" "lineage_enforce" 30 1 -u qdistro-admin-broker.service'
```

**Assert**: the journal line reads `lineage_enforce=True`.

### S3 — enforce mode: forged claim from unregistered caller is denied

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u work -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.CheckPermission \
  string:"org.qdistro.lineage.test" \
  dict:string:string:"sandbox_engine","qdistro.tier1"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: reply is `string "unknown"`. The forged `sandbox_engine` was
dropped (no launch record → resolved engine `""`), so the tier-1 rule no
longer matches. **This is the P0-1 fix.**

### S4 — `RegisterLaunch` is root-only

```bash
# A non-root (work) caller must be refused at the bus / method level.
B64=$(base64 -w0 <<'EOF'
runuser -u work -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.RegisterLaunch \
  string:"work" string:"qdistro.tier1" string:"qdistro.tier1.work" \
  string:"i1" string:"/usr/bin/sleep" uint64:1 string:"" uint64:0 \
  2>&1 || true
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: the call fails — either an `org.freedesktop.DBus.Error.AccessDenied`
from the bus policy (RegisterLaunch denied to the default context) or, if
the bus lets it through to the method, `...AdminBroker1.AccessDenied`
("RegisterLaunch restricted to root launchers"). Either way the work uid
cannot register a launch record.

### S5 — enforce mode: a registered, verified caller matches

A launch record must bind the **live caller pid**, so we use a long-lived
helper that holds its D-Bus connection open across the registration.

```bash
# 1. Drop a helper that prints its pid, waits for a go-file, then calls
#    CheckPermission with NO secctx (proving the broker supplies the
#    launcher-attested engine, not the caller).
B64=$(base64 -w0 <<'EOF'
cat >/tmp/lineage-helper.py <<'PY'
import dbus, os, sys, time
bus = dbus.SystemBus()
obj = bus.get_object("org.qdistro.AdminBroker1", "/org/qdistro/AdminBroker1")
iface = dbus.Interface(obj, "org.qdistro.AdminBroker1")
with open("/tmp/lineage-helper.pid", "w") as f:
    f.write(str(os.getpid()))
while not os.path.exists("/tmp/lineage-go"):
    time.sleep(0.1)
verdict = iface.CheckPermission("org.qdistro.lineage.test", {})
with open("/tmp/lineage-helper.out", "w") as f:
    f.write(str(verdict))
PY
chmod 0644 /tmp/lineage-helper.py
rm -f /tmp/lineage-go /tmp/lineage-helper.out /tmp/lineage-helper.pid
runuser -u work -- bash -c 'setsid python3 /tmp/lineage-helper.py >/tmp/lineage-helper.log 2>&1 &'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1

# 2. Root registers the helper's live pid + starttime as a tier-1 work app.
B64=$(base64 -w0 <<'EOF'
PID=$(cat /tmp/lineage-helper.pid)
ST=$(python3 -c "d=open('/proc/$PID/stat','rb').read(); print(int(d[d.rfind(b')')+2:].split()[19]))")
EXE=$(readlink "/proc/$PID/exe")
RID=$(dbus-send --system --print-reply=literal \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.RegisterLaunch \
  string:"work" string:"qdistro.tier1" string:"qdistro.tier1.work" \
  string:"i1" string:"$EXE" uint64:"$PID" string:"" uint64:"$ST")
test -n "$RID"
printf 'registered %s pid %s starttime %s\n' "$RID" "$PID" "$ST"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# 3. Release the helper; it calls CheckPermission on its own connection.
$VMEXEC "$VM" 'touch /tmp/lineage-go'
sleep 1
$VMEXEC "$VM" 'cat /tmp/lineage-helper.out'
```

**Assert**:
- The trusted root `dbus-send` launcher prints
  `registered <hex> pid <n> starttime <n>`.
- `/tmp/lineage-helper.out` contains `allow`. The helper passed **no**
  `sandbox_engine`; the broker resolved its live pid to the launch
  record and supplied `qdistro.tier1`, so the rule matched. This is the
  verified-lineage path: claim == launcher record == live kernel
  identity == policy subject.

### S6 — enforce mode: launch record audit row exists

```bash
AUDIT_SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT count(*) FROM audit WHERE action LIKE 'qdistro.lineage.register:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $AUDIT_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**: count is `>= 1` — `RegisterLaunch` wrote a
`qdistro.lineage.register:work` audit row (forensic trail, findings Q#7).

## Teardown

```bash
$VMEXEC "$VM" 'touch /tmp/lineage-go; pkill -u work -f lineage-helper 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/lineage-helper.py /tmp/lineage-helper.out /tmp/lineage-helper.pid /tmp/lineage-go /tmp/lineage-helper.log'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'sed -i "/lineage_enforce/d" /etc/qdistro/broker.conf 2>/dev/null || true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
AUDIT_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action LIKE 'qdistro.lineage.%' OR action='org.qdistro.lineage.test';
SQL_EOF
)
$VMEXEC "$VM" "echo $AUDIT_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- The default broker posture is `lineage_enforce=false` (shadow). S1 must
  run **before** S2 writes the flag, or it will see enforce semantics.
- The helper in S5 must keep its D-Bus connection open between
  registration and the `CheckPermission` call: the broker resolves the
  **caller's live pid**, and a one-shot `dbus-send` would exit before the
  record could bind its pid. A recycled or exited pid resolves to
  `unknown` (fail-closed), which is the correct behaviour but not what S5
  is demonstrating.
- `setsid` detaches the helper so the `vm-exec` SSH session returning
  doesn't SIGHUP it.
- If S5 reports `unknown` instead of `allow`, check that the helper's pid
  in `/tmp/lineage-helper.pid` still names the live python process at
  registration time (`ps -p <pid>`), and that SELinux is not relabelling
  the helper into a domain that diverges from the registered label (the
  label axis fails closed on mismatch).
