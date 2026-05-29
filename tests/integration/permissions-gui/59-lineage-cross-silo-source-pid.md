# 59 — permission lineage: cross-silo source attested by launch record (P1-1)

**What**: the cross-silo clipboard gate (`CheckClipboardTransfer`) keys its
rule lookup on the **source** silo. The source is named by qdshell, not the
D-Bus caller, so the broker now takes the source app's
kernel-authenticated `(source_pid, source_starttime)` and resolves *that*
pid against the launch-record store instead of trusting the claimed string
(finding **P1-1**). Prove the postures end-to-end against a live broker:

- **Shadow** (`lineage_enforce=false`, default): the claimed source silo
  still drives the decision — a `work:admin` rule matches a claimed
  `source_silo=work` → `"allow"` (legacy behaviour preserved).
- **Enforce, no attested source**: a cross-silo transfer with no relayed
  source pid (or an unregistered pid) → `"deny"`. A cross-silo decision
  requires an attested source.
- **Enforce, registered source**: after a root launcher `RegisterLaunch`es
  the live source pid as silo `work`, the transfer resolves to the
  launcher-attested `work` and the `work:admin` rule matches → `"allow"`.
- **Enforce, forged source claim**: the caller claims `source_silo=work`
  (to hit the work rule) but the source pid is attested as `scratch`; the
  broker overrides the claim with the attested silo → action
  `scratch:admin`, no rule → `"deny"`. The claim cannot forge the source.

**Why**: `issues/qdistro/permission-lineage-findings.md` finding P1-1 — the
cross-silo gates matched on a qdshell-relayed `source_silo` string that was
never re-bound to the live source process, so a forged source silo could
satisfy a cross-silo `allow` rule. This is the end-to-end proof that the
relayed source pid is resolved against the launch record and that a forged
or unattested source fails closed.

Headless scenario. Requires the broker build that ships
`qdistro_proc_identity.py`, `qdistro_launch_record.py`,
`qdistro_resolver.py`, `RegisterLaunch`, and the `_cross_silo_source`
routing on `CheckClipboardTransfer` (the `ssassssbut` signature with the
trailing `source_pid`/`source_starttime`).

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

# Clean slate: drop scenario rules + any prior enforce flag, restart broker
# in the default (shadow) posture.
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'pkill -f cross-silo-src-helper 2>/dev/null; true'
$VMEXEC "$VM" 'test -f /etc/qdistro/broker.conf && sed -i "/lineage_enforce/d" /etc/qdistro/broker.conf || true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1

# Admin authors a single cross-silo allow rule: work may paste into admin.
B64=$(base64 -w0 <<'EOF'
YAML='- name: scenario-59-work-to-admin
  decision: allow
  match:
    action: qdistro.clipboard.transfer:work:admin
'
runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.SaveRule \
  string:"59-work-to-admin.yaml" string:"$YAML"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1

# A long-lived `work` process supplies the source pid. sleep keeps a stable
# (pid, starttime, exe) for the duration of the scenario.
$VMEXEC "$VM" 'runuser -u work -- bash -c "setsid sleep 600 >/dev/null 2>&1 & echo \$! >/tmp/cross-silo-src-helper.pid"; true'
sleep 1
```

## Steps

A small helper builds the gate call. The gate is admin-pinned, so the
`dbus-send` runs as `admin`; `source_pid`/`source_starttime` name the
`work` source process.

### S1 — shadow: claimed source silo drives the decision (legacy)

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply=literal \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.CheckClipboardTransfer \
  string:"work" string:"admin" array:string:"text/plain" \
  string:"" string:"" string:"" boolean:false \
  uint32:0 uint64:0
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: reply is `allow`. Default broker is `lineage_enforce=false`; the
claimed `work` source matches the `work:admin` rule with no source pid.

### S2 — switch to enforce mode

```bash
$VMEXEC "$VM" 'install -d -m 0755 /etc/qdistro'
$VMEXEC "$VM" 'grep -q "^lineage_enforce" /etc/qdistro/broker.conf 2>/dev/null \
  || echo "lineage_enforce = true" >> /etc/qdistro/broker.conf'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1
$VMEXEC "$VM" 'journalctl -u qdistro-admin-broker.service --since "-10s" | grep -m1 lineage_enforce'
```

**Assert**: the journal line reads `lineage_enforce=True`.

### S3 — enforce: cross-silo with no attested source is denied

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- dbus-send --system --print-reply=literal \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.CheckClipboardTransfer \
  string:"work" string:"admin" array:string:"text/plain" \
  string:"" string:"" string:"" boolean:false \
  uint32:0 uint64:0
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: reply is `deny`. No source pid was relayed, so under enforce the
cross-silo decision fails closed (the claimed `work` is no longer trusted).

### S4 — enforce: source pid present but unregistered is denied

```bash
B64=$(base64 -w0 <<'EOF'
PID=$(cat /tmp/cross-silo-src-helper.pid)
ST=$(python3 -c "d=open('/proc/$PID/stat','rb').read(); print(int(d[d.rfind(b')')+2:].split()[19]))")
runuser -u admin -- dbus-send --system --print-reply=literal \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.CheckClipboardTransfer \
  string:"work" string:"admin" array:string:"text/plain" \
  string:"" string:"" string:"" boolean:false \
  uint32:$PID uint64:$ST
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: reply is `deny`. The relayed pid resolves to the `unknown`
subject (no launch record) → cross-silo fails closed.

### S5 — enforce: root registers the source → allow

```bash
B64=$(base64 -w0 <<'EOF'
python3 - <<'PY'
import dbus, os
pid = int(open("/tmp/cross-silo-src-helper.pid").read().strip())
d = open(f"/proc/{pid}/stat","rb").read()
st = int(d[d.rfind(b")")+2:].split()[19])
exe = os.readlink(f"/proc/{pid}/exe")
bus = dbus.SystemBus()
ifc = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
        "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
rid = ifc.RegisterLaunch("work","qdistro.tier3","qdistro.tier3.work",
        "i1", exe, dbus.UInt64(pid), "", dbus.UInt64(st))
print("registered", rid, "pid", pid, "starttime", st)
PY
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

B64=$(base64 -w0 <<'EOF'
PID=$(cat /tmp/cross-silo-src-helper.pid)
ST=$(python3 -c "d=open('/proc/$PID/stat','rb').read(); print(int(d[d.rfind(b')')+2:].split()[19]))")
runuser -u admin -- dbus-send --system --print-reply=literal \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.CheckClipboardTransfer \
  string:"work" string:"admin" array:string:"text/plain" \
  string:"" string:"" string:"" boolean:false \
  uint32:$PID uint64:$ST
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**:
- `RegisterLaunch` prints `registered <hex> pid <n> starttime <n>`.
- the transfer reply is `allow`. The source pid resolved to the
  launcher-attested silo `work`, so the `work:admin` rule matched. This is
  the verified-lineage cross-silo path.

### S6 — enforce: a forged source claim is overridden by attestation

Re-register the **same** source pid as silo `scratch` (re-registration is
idempotent on the live process), then have the caller forge
`source_silo=work` to try to reuse the `work:admin` rule.

```bash
B64=$(base64 -w0 <<'EOF'
python3 - <<'PY'
import dbus, os
pid = int(open("/tmp/cross-silo-src-helper.pid").read().strip())
d = open(f"/proc/{pid}/stat","rb").read()
st = int(d[d.rfind(b")")+2:].split()[19])
exe = os.readlink(f"/proc/{pid}/exe")
bus = dbus.SystemBus()
ifc = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
        "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
ifc.RegisterLaunch("scratch","qdistro.tier3","qdistro.tier3.scratch",
        "i1", exe, dbus.UInt64(pid), "", dbus.UInt64(st))
print("re-registered as scratch")
PY
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

B64=$(base64 -w0 <<'EOF'
PID=$(cat /tmp/cross-silo-src-helper.pid)
ST=$(python3 -c "d=open('/proc/$PID/stat','rb').read(); print(int(d[d.rfind(b')')+2:].split()[19]))")
runuser -u admin -- dbus-send --system --print-reply=literal \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.CheckClipboardTransfer \
  string:"work" string:"admin" array:string:"text/plain" \
  string:"" string:"" string:"" boolean:false \
  uint32:$PID uint64:$ST
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: reply is `deny`. The caller claimed `work`, but the source pid
is attested as `scratch`; the broker used `scratch` → action
`qdistro.clipboard.transfer:scratch:admin`, which has no rule → default
deny. **The source silo cannot be forged.**

### S7 — enforce: the deny left a forensic row

```bash
AUDIT_SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT count(*) FROM audit WHERE action LIKE 'qdistro.lineage.source_deny:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $AUDIT_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**: count is `>= 1` — the unattested-source denials (S3/S4) wrote
`qdistro.lineage.source_deny:clipboard.transfer:*` audit rows (Q#7).

## Teardown

```bash
$VMEXEC "$VM" 'pkill -f "sleep 600" 2>/dev/null; rm -f /tmp/cross-silo-src-helper.pid; true'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
$VMEXEC "$VM" 'sed -i "/lineage_enforce/d" /etc/qdistro/broker.conf 2>/dev/null || true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
AUDIT_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action LIKE 'qdistro.lineage.%'
  OR action LIKE 'qdistro.clipboard.transfer:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $AUDIT_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- S1 must run **before** S2 writes the enforce flag, or it will see
  enforce semantics (deny, since S1 relays no source pid).
- The `work` source is a plain `sleep 600`; its pid only needs to stay
  alive and keep a stable starttime across S4–S6. It does **not** hold a
  D-Bus connection (unlike scenario 58) — here the admin `dbus-send` is the
  gate caller and the source pid is merely named.
- `=literal` keeps the reply as a bare `allow`/`deny` token (no `string `
  prefix); assert on the trimmed line.
- If S5 reports `deny`, confirm the `sleep` pid in
  `/tmp/cross-silo-src-helper.pid` is still live (`ps -p <pid>`) and that
  its `/proc/<pid>/exe` (coreutils `sleep`) matches what `RegisterLaunch`
  re-read — a relabel or exec-swap would fail the resolver's exe axis.
- This scenario exercises the broker + its launch-record store directly.
  The full app→qdwin→qdshell→broker relay of the source pid is covered by
  the qdshell binding/QML change (deployed in lockstep); registration by a
  trusted launcher across tiers is the tracked next increment (see the
  findings doc, session 3).
