# 52 — Invalid target_user is rejected before the broker is reached

**What**: as `work`, send a hand-crafted JSON request to
`/run/qdistro-root-exec/sock` whose `target_user` field carries an
embedded newline and other control characters. The
`qdistro-root-exec` service must reject it with an `error` frame
containing `invalid target_user`, return a non-zero `exit` frame,
and write NOTHING to the broker — `dbus-monitor` over the broker's
RequestPermissionAs signal sees zero traffic, and the broker's
audit DB has no new row for the malicious target. The admin app
never sees a pending row.

**Why**: `qsu/qdistro_root_exec.py` defines
`_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")` and rejects
any `target_user` that doesn't match BEFORE
`RequestPermissionAs` is called. The reason in the source comment
is explicit: "embedded newlines / control chars flow into the
broker action string and the audit syslog line." A regression
that drops the regex check would let an attacker write
near-arbitrary bytes into the audit DB's `action` column (which
admin browses for security incident review) — exactly the kind
of log injection that turns a sandbox bypass into a forensic
hide-the-evidence trick.

This is a headless scenario — no admin app interaction needed,
just the wire shape.

## Setup

```bash
VM=${VMNAME:-qd-sudo}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

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

### S1 — start dbus-monitor as admin to record any broker calls

```bash
$VMEXEC "$VM" 'runuser -u admin -- bash -c "dbus-monitor --system \
  \"interface='\''com.qdistro.AdminBroker1'\''\" \
  >/tmp/52-dbusmon.log 2>&1 & echo \$! >/tmp/52-dbusmon.pid"'
sleep 1
$VMEXEC "$VM" 'cat /tmp/52-dbusmon.pid'
```

### S2 — send a malicious JSON request directly to the socket

We bypass `/usr/local/bin/qsu` (the client) because qsu parses
target_user via argparse and would itself reject some forms. The
purpose here is to test the SERVER-side defence in
`qdistro-root-exec`. Use a python script that opens the socket
and sends a hand-crafted frame.

```bash
B64=$(base64 -w0 <<'EOF'
cat >/tmp/52-evil-client.py <<'PY'
import json, socket, sys
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/run/qdistro-root-exec/sock")
# Embedded newline + ANSI clear + fake-good username — exactly the
# kind of injection the _USERNAME_RE regex blocks.
target = "root\n[OK] audit row trailer\x1b[2J"
req = {"target_user": target, "argv": ["/bin/true"]}
sock.sendall((json.dumps(req) + "\n").encode())
# Read up to two frames (error + exit).
buf = b""
sock.settimeout(5.0)
try:
    while b"exit" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
except socket.timeout:
    pass
sys.stdout.write(buf.decode(errors="replace"))
PY
chmod 0644 /tmp/52-evil-client.py
sudo -u work python3 /tmp/52-evil-client.py >/tmp/52-evil.log 2>&1
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'cat /tmp/52-evil.log'
```

**Assert**: `/tmp/52-evil.log` contains:
- A JSON `error` frame with message containing `invalid target_user`.
- A JSON `exit` frame with non-zero code (typically `1`).
- NO `stdout`/`stderr` frames for `/bin/true` — the exec never
  happened.

### S3 — broker received NO RequestPermissionAs for the malicious target

Stop dbus-monitor and inspect the log:

```bash
$VMEXEC "$VM" 'kill $(cat /tmp/52-dbusmon.pid) 2>/dev/null; sleep 1
grep -c "RequestPermissionAs" /tmp/52-dbusmon.log || echo 0
grep -c "qsu.exec:" /tmp/52-dbusmon.log || echo 0'
```

**Assert**: both counts are `0` — the broker was never asked.
qdistro-root-exec failed closed at the input-validation stage.

### S4 — broker audit DB has no row for the malicious target

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT COUNT(*) FROM audit WHERE action LIKE 'qsu.exec:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**: count is `0`. The malicious target_user string never
reached the audit table — so an attacker cannot exploit log-line
injection to obscure their tracks.

### S5 — syslog records the validation rejection

The handler writes via `syslog.syslog(LOG_NOTICE, ...)` for valid
requests and `LOG_WARNING` for race detections; per the source,
an invalid target_user goes through the generic error path which
does NOT necessarily syslog (it just sends the error frame). The
load-bearing check is S2-S4 above; syslog is an OS-level
nice-to-have. To verify the handler at least surfaced an error,
also inspect journalctl:

```bash
$VMEXEC "$VM" 'journalctl -u qdistro-root-exec.service --since "1 minute ago" --no-pager | tail -20'
```

**Assert** (soft): if any line appears for this minute window
mentioning `invalid` or `error`, that's a bonus. If nothing
appears, treat as PASS — the contract is the wire-level error
frame, not the syslog line.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f dbus-monitor 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/52-*.log /tmp/52-*.pid /tmp/52-evil-client.py'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';
DELETE FROM audit WHERE action LIKE 'qsu.exec:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- This scenario MUST NOT use `/usr/local/bin/qsu` to send the
  malicious target — qsu's client-side argparse rejects newline-
  containing usernames before they hit the wire, and that would
  short-circuit our test of the SERVER-side defence. The python
  client opens the AF_UNIX socket directly and sends the raw
  JSON frame.
- The socket mode is 0666 by design (see qdistro_root_exec.py
  comment: "an attacker who opens the socket just burns a rate-
  limit slot and still has to pass admin approval"). Any non-
  root local user can connect; rejection happens at the request
  parse step.
- If S2 produces stdout from `/bin/true` (i.e. the exec actually
  ran), the validation is bypassed and we have a real
  vulnerability. Capture the full evil.log and flag as a FAIL
  with high urgency.
