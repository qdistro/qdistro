# 32 — `forever_exe` scope grants only the approved exe

**What**: approve `test.action` for `work` with scope `forever_exe`
when the caller exe is `/usr/bin/python3.13`. Then (a) repeat the
python call → cache hit, ALLOWED, no prompt; (b) issue the same
action from `/usr/bin/perl` → prompt appears (cache row's
`forever_exe` does not match the new exe).

**Why**: `forever_exe` is the per-exe scope — admin's way of saying
"this command is fine forever, but I'm not signing a blank cheque
for the uid". `permissions.md` describes scopes only conceptually;
the test that the broker actually enforces exe-discrimination on
cache lookup is what protects admin from a compromised /usr/bin
binary scope-bleeding into other binaries.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f "perl /tmp/32-" 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/32-release-wait'
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

### S1 — launch admin app, trigger python request, approve forever_exe

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3

B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
  >/tmp/32-py1.log 2>&1 & echo $! >/tmp/32-py1.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2

$VMGUI "$VM" screenshot /tmp/32-s1a-pending.png

B64=$(base64 -w0 <<'EOF'
runuser -u admin -- python3 - <<'PYEOF'
import dbus

bus = dbus.SystemBus()
obj = bus.get_object("org.qdistro.AdminBroker1",
                     "/org/qdistro/AdminBroker1")
iface = dbus.Interface(obj, "org.qdistro.AdminBroker1")
for row in iface.GetPending():
    if str(row.get("action", "")) == "test.action":
        iface.DecideRequest(int(row["id"]), "allow", "forever_exe")
        break
else:
    raise SystemExit("no pending test.action request")
PYEOF
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1

$VMEXEC "$VM" 'wait $(cat /tmp/32-py1.pid) 2>/dev/null; cat /tmp/32-py1.log'
```

**Assert**:
- `/tmp/32-py1.log` contains `ALLOWED`.

### S2 — cache row carries `forever_exe` + python's exe

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, action, match_kind, match_value, scope
  FROM approvals WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
```

**Assert**: output is exactly
`2000|test.action|exe_only|/usr/bin/python3.13|forever_exe` (or
`/usr/bin/python3.11` etc. depending on Tumbleweed's python
package — the load-bearing string is the `/usr/bin/python*` exe
captured at decide time).

### S3 — second python call: cache hit, no prompt

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
  >/tmp/32-py2.log 2>&1 & echo $! >/tmp/32-py2.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/32-s3-stillempty.png
$VMEXEC "$VM" 'wait $(cat /tmp/32-py2.pid) 2>/dev/null; cat /tmp/32-py2.log'
```

**Assert**:
- `/tmp/32-s3-stillempty.png`: admin app's pending list empty.
- `/tmp/32-py2.log`: `ALLOWED`. (Cache hit — no admin interaction.)

### S4 — perl call: prompt re-appears (different exe)

```bash
B64=$(base64 -w0 <<'EOF'
cat >/tmp/32-perl-caller.pl <<'PERL'
use strict; use warnings;
use Net::DBus;
my $bus = Net::DBus->system;
my $svc = $bus->get_service("org.qdistro.AdminBroker1");
my $obj = $svc->get_object("/org/qdistro/AdminBroker1",
                           "org.qdistro.AdminBroker1");
my $rid = $obj->RequestPermission("test.action", { caller => "perl-32" });
print "rid=$rid\n";
# A visual runner can spend longer than Net::DBus's default method timeout
# inspecting the pending screenshot. Keep this SAME caller alive, but do not
# enter the blocking D-Bus method until S5 has delivered the decision.
while (!-e "/tmp/32-release-wait") {
    select undef, undef, undef, 0.1;
}
my $ok = $obj->WaitForDecision(int $rid);
print($ok ? "ALLOWED\n" : "DENIED\n");
PERL
chmod 0644 /tmp/32-perl-caller.pl
sudo -u work bash -c 'perl /tmp/32-perl-caller.pl \
  >/tmp/32-perl.log 2>&1 & echo $! >/tmp/32-perl.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2

$VMGUI "$VM" screenshot /tmp/32-s4-perl-pending.png
$VMEXEC "$VM" 'dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.GetPending'
```

**Assert**:
- `/tmp/32-s4-perl-pending.png`: one new pending row visible.
- `GetPending` output: one entry with `uid=2000`,
  `action=test.action`, `exe` ending in `perl`, `details` showing
  `caller=perl-32`. The python cache row did NOT short-circuit
  this request.

### S5 — deny perl, sender sees DENIED

```bash
# OCR-click Deny (or virsh Ctrl+N if window is focused).
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 1
$VMEXEC "$VM" 'touch /tmp/32-release-wait'
$VMEXEC "$VM" 'wait $(cat /tmp/32-perl.pid) 2>/dev/null; cat /tmp/32-perl.log'
```

**Assert**: `/tmp/32-perl.log` contains `DENIED`.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f "perl /tmp/32-perl-caller.pl" 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/32-perl-caller.pl /tmp/32-*.log /tmp/32-*.pid'
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

- This scenario is the operational complement to scenario 28: 28
  proves rules glob-match across exes; 32 proves cache rows
  exact-match a single exe. The two together pin the rule/cache
  separation of concerns.
- If S2 shows `match_kind=always` instead of `exe_only`, the
  admin app didn't actually pick `forever_exe` — re-examine S1b
  screenshot. (`forever` selects `always`; `forever_exe` selects
  `exe_only`.)
