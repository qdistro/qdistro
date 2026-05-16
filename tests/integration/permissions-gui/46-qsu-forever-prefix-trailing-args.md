# 46 — `forever_prefix` hits trailing args; differing prefix re-prompts

**What**: install `forever_prefix` for argv prefix
`[/usr/bin/systemctl, status]`. Then issue qsu calls that vary in
the **trailing** args and verify:
1. `qsu /usr/bin/systemctl status` (exact prefix, no trailing) →
   cache hit.
2. `qsu /usr/bin/systemctl status sshd` (prefix + one trailer) →
   cache hit.
3. `qsu /usr/bin/systemctl status sshd cron` (prefix + two
   trailers) → cache hit.
4. `qsu /usr/bin/systemctl restart sshd` (prefix verb differs:
   `restart` ≠ `status`) → re-prompt.

**Why**: `doc/sudo.md` describes `forever_prefix` as "Approves a
command + any args" — the right scope for verbs that the admin
considers safe regardless of which service or file they're applied
to (`systemctl status <anything>`, `journalctl -u <anything>`,
`apt-cache search <anything>`). The cache match is list-equality on
`argv[:len(prefix)]`, not a string-prefix on shlex.join — see
`_cache_hit` `if row_kind == "prefix": return tuple(argv[:n]) ==
tuple(row_argv)`. A regression where the comparator does
string-prefix would inadvertently hit `[systemctl, statuss]` (typo),
or worse, `[systemctl, status; rm -rf]` if argv were shlex-joined.
The list-equality property is load-bearing.

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

B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
```

## Steps

### S1 — first qsu /usr/bin/systemctl status pends

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /usr/bin/systemctl status \
  >/tmp/46-status1.log 2>&1 & echo $! >/tmp/46-status1.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/46-s1-pending.png
```

**Assert**: pending row; details show `argv=/usr/bin/systemctl status`.

### S2 — admin picks `forever_prefix` and approves

```bash
$VMGUI "$VM" screenshot /tmp/46-s2a-radios.png
# Runner: click "Forever, this argv prefix + any trailing args"
# (8th radio). Internally `forever_prefix` pins argv[:len(prefix)]
# as a TUPLE. Here the prefix is the full argv from S1:
# ["/usr/bin/systemctl", "status"]. (Prior label
# "Forever, this argv[0] (any trailing args)" was renamed
# 2026-05-16 alongside this scenario — see todo
# qsu-forever-prefix-label-misleading.md.)
$VMGUI "$VM" screenshot /tmp/46-s2b-selected.png

B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 3

$VMEXEC "$VM" 'wait $(cat /tmp/46-status1.pid) 2>/dev/null; head -3 /tmp/46-status1.log'
```

**Assert**:
- `/tmp/46-s2b-selected.png` shows `forever_prefix` radio filled.
- Log: `systemctl status` (no service named) prints a usage line
  or returns rc=1 — either is fine, the assertion is "the command
  actually ran" (i.e. log is non-empty after wait).
- Cache:
  ```bash
  SQL_B64=$(base64 -w0 <<'SQL_EOF'
  SELECT match_kind, scope FROM approvals WHERE action='qsu.exec:root';
  SQL_EOF
  )
  $VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
  ```
  Output: `prefix|forever_prefix`.

### S3 — prefix + one trailing arg → cache hit

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /usr/bin/systemctl status sshd \
  >/tmp/46-status2.log 2>&1 & echo $! >/tmp/46-status2.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 3
$VMGUI "$VM" screenshot /tmp/46-s3-stillempty.png
$VMEXEC "$VM" 'wait $(cat /tmp/46-status2.pid) 2>/dev/null; head -3 /tmp/46-status2.log'
```

**Assert**: pending list empty; log shows `sshd.service` or
similar systemctl output for the sshd unit.

### S4 — prefix + two trailing args → cache hit

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /usr/bin/systemctl status sshd cron \
  >/tmp/46-status3.log 2>&1 & echo $! >/tmp/46-status3.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 3
$VMGUI "$VM" screenshot /tmp/46-s4-stillempty.png
$VMEXEC "$VM" 'wait $(cat /tmp/46-status3.pid) 2>/dev/null; head -5 /tmp/46-status3.log'
```

**Assert**: pending list empty; log shows status output for at
least one of `sshd` / `cron`.

### S5 — differing prefix verb (`restart`) → re-prompt

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /usr/bin/systemctl restart sshd \
  >/tmp/46-restart.log 2>&1 & echo $! >/tmp/46-restart.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/46-s5-pending.png
```

**Assert** (`/tmp/46-s5-pending.png`): one pending row, details
show `argv=/usr/bin/systemctl restart sshd`. The forever_prefix
row for `[systemctl, status]` did NOT match because
`tuple(["systemctl","restart"]) != tuple(["systemctl","status"])`.

### S6 — deny restart to clean up

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 1
$VMEXEC "$VM" 'wait $(cat /tmp/46-restart.pid) 2>/dev/null; cat /tmp/46-restart.log'
```

**Assert**: log contains `request denied`.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/46-*.log /tmp/46-*.pid'
B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Notes for the runner

- The admin app's radio label `Forever, this argv prefix + any
  trailing args` reflects the actual semantic: `forever_prefix`
  pins `argv[:len(prefix)]` from the originally-approved call. If
  admin approved `qsu systemctl status` then the prefix is
  length 2; trailing means "argv beyond index 1". If admin
  approved `qsu systemctl` alone then the prefix is length 1
  and "trailing" is "any subcommand." The scenario uses the
  length-2 case because it's the harder property to enforce.
- The cache backend's `_cache_hit` checks
  `tuple(argv[:n]) == tuple(row_argv)` where `n = len(row_argv)`.
  A regression that uses `argv[0] == row_argv[0]` (only argv[0])
  would still pass S3 and S4 but would also cache-hit S5 (since
  argv[0] is the same) — that's the bug this scenario flags.
