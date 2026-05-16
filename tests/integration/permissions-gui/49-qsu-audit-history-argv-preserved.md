# 49 — `ListHistory` carries qsu argv losslessly as `as` (not shlex-joined)

**What**: drive a single qsu invocation end-to-end through the
admin app (allow `forever_argv` on
`/usr/local/bin/qsu /usr/bin/echo hello world`). Then call
`ListHistory(limit=10)` over D-Bus as admin and inspect the
returned row. The `argv` field must come back as a D-Bus
`array of string` whose elements are the exact argv tuple
`["/usr/bin/echo", "hello world"]` — preserving the embedded space
that would be ambiguous in any shlex-joined / space-joined form.
`caller_exe` is `/usr/bin/python3<MINOR>` — the qsu wrapper at
`/usr/local/bin/qsu` is a bash 2-liner that exec's into
`/usr/bin/python3 /usr/local/lib/qdistro/qsu.py`, so by the time
the socket connect happens `/proc/<pid>/exe` resolves to the
interpreter. The load-bearing assertion is "it's the python
interpreter on `/usr/bin`," NOT a path under `/tmp` or a
non-existent string — the audit must capture a real, traceable
binary. `decision=true`, `scope=forever_argv`, `source=prompt`.

**Why**: `doc/sudo.md` §Audit log explicitly promises that
`AuditLog.log()` accepts the argv list, `broker.ListHistory()`
carries it across the wire, and the admin app's History tab can
render it. The audit's load-bearing property is "argv is shipped
end-to-end" so an admin reviewing yesterday's escalations can spot
when an argv with whitespace was approved (a lossy space-join would
make `rm /tmp/foo bar` and `rm "/tmp/foo bar"` indistinguishable —
the exact bug `argv_dbus = dbus.Array(...)` in
`broker.ListHistory` was added to prevent). No existing test asserts
the wire-level shape of this field.

## Setup

```bash
VM=${VMNAME:-qd-sudo}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
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

### S1 — launch admin app

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
```

### S2 — invoke qsu with an argv that has an embedded space

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /usr/bin/echo "hello world" \
  >/tmp/49-qsu.log 2>&1 & echo $! >/tmp/49-qsu.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/49-s2-pending.png
```

**Assert** (`/tmp/49-s2-pending.png`):
- One pending row, action `qsu.exec:root`, uid 2000.
- Details show `argv=/usr/bin/echo 'hello world'` (shlex-joined,
  which IS the human-readable form sent in `details['argv']`).
- `argv[01]=hello world` (NO outer quotes — the per-element keys
  are lossless).
- The detail pane's `exe` line points to `/usr/bin/python3<...>`
  (the qsu wrapper exec'd into python before the connect, so
  `/proc/<pid>/exe` resolves to the interpreter).

### S3 — admin selects `forever_argv` and approves

```bash
$VMGUI "$VM" screenshot /tmp/49-s3a-forever-argv-selected.png
# Runner: click "Forever, only this exact argv tuple" radio (6th).
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 2

$VMEXEC "$VM" 'wait $(cat /tmp/49-qsu.pid) 2>/dev/null; cat /tmp/49-qsu.log'
```

**Assert**:
- `/tmp/49-qsu.log` contains exactly `hello world` followed by
  newline (echo's output streamed through qsu).
- Cache row exists:
  ```bash
  SQL_B64=$(base64 -w0 <<'SQL_EOF'
  SELECT caller_uid, action, match_kind, scope FROM approvals
    WHERE action='qsu.exec:root';
  SQL_EOF
  )
  $VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
  ```
  Output: `2000|qsu.exec:root|argv_exact|forever_argv`.

### S4 — query `ListHistory` and inspect the argv field shape

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- python3 - <<'PYEOF'
import dbus, json
bus = dbus.SystemBus()
obj = bus.get_object("com.qdistro.AdminBroker1",
                     "/com/qdistro/AdminBroker1")
iface = dbus.Interface(obj, "com.qdistro.AdminBroker1")
rows = iface.ListHistory(10)
# Find the qsu.exec:root row this scenario just wrote.
for r in rows:
    if str(r.get("action", "")).startswith("qsu.exec:"):
        out = {
            "action":      str(r["action"]),
            "caller_uid":  int(r["caller_uid"]),
            "caller_exe":  str(r["caller_exe"]),
            "decision":    bool(r["decision"]),
            "scope":       str(r["scope"]),
            "source":      str(r["source"]),
            "argv":        [str(a) for a in r["argv"]],
            "argv_len":    len(r["argv"]),
        }
        print(json.dumps(out, indent=2))
        break
else:
    print(json.dumps({"error": "no qsu.exec audit row found"}))
PYEOF
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: stdout is the JSON shape below, with these load-bearing
fields:

```json
{
  "action": "qsu.exec:root",
  "caller_uid": 2000,
  "caller_exe": "/usr/bin/python3.13",
  "decision": true,
  "scope": "forever_argv",
  "source": "prompt",
  "argv": ["/usr/bin/echo", "hello world"],
  "argv_len": 2
}
```

The load-bearing pieces are:
- `argv` is a list of length 2, NOT a single string.
- `argv[1]` is exactly `hello world` with embedded space preserved.
- `caller_exe` starts with `/usr/bin/python3` (the python minor
  may vary across Tumbleweed updates — match a regex like
  `^/usr/bin/python3(\.\d+)?$`, not the exact patch version).
- `source=prompt` (this allow came from admin's click, not a rule
  or cache pre-hit).

### S5 — second qsu invocation hits cache; audit gets a `source=cache` row

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /usr/bin/echo "hello world" \
  >/tmp/49-qsu2.log 2>&1 & echo $! >/tmp/49-qsu2.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMEXEC "$VM" 'wait $(cat /tmp/49-qsu2.pid) 2>/dev/null; cat /tmp/49-qsu2.log'

B64=$(base64 -w0 <<'EOF'
runuser -u admin -- python3 - <<'PYEOF'
import dbus, json
bus = dbus.SystemBus()
obj = bus.get_object("com.qdistro.AdminBroker1",
                     "/com/qdistro/AdminBroker1")
iface = dbus.Interface(obj, "com.qdistro.AdminBroker1")
rows = iface.ListHistory(10)
sources = [str(r.get("source")) for r in rows
           if str(r.get("action", "")).startswith("qsu.exec:")]
print(json.dumps(sources))
PYEOF
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**:
- `/tmp/49-qsu2.log` is exactly `hello world\n` (cache hit ran
  the command transparently).
- The sources list (newest first) is `["cache", "prompt"]` or has
  `cache` at least once for the second invocation.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/49-*.log /tmp/49-*.pid'
SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';
DELETE FROM audit WHERE action LIKE 'qsu.exec:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Notes for the runner

- `ListHistory` requires admin/root caller; run via `runuser -u
  admin --` (the bus policy allows admin uid).
- The `argv` field's D-Bus signature is `as`. dbus-python returns
  it as a list of `dbus.String`; convert each element to `str()`
  for clean JSON output.
- If the broker ever down-converts argv to a shlex-joined string
  on the wire, this scenario will see `argv` as a 1-element list
  containing `"/usr/bin/echo 'hello world'"` — that's the
  regression to flag.
- The `caller_exe = /usr/bin/python3.X` reality (because
  `/usr/local/bin/qsu` is a bash → python exec wrapper) means
  history-tab admins can't visually distinguish a qsu invocation
  from any other python3 script. The action prefix
  (`qsu.exec:<target>`) is the actual qsu fingerprint; do not
  rely on caller_exe to filter qsu history. Tracked as a
  follow-up todo (compiled qsu binary or rename-after-exec).
