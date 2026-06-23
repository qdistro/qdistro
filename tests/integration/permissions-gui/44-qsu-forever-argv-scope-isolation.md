# 44 — `forever_argv` hits the exact argv tuple; different argv re-prompts

**What**: as `work`, run `/usr/local/bin/qsu /bin/echo hello`. Admin
selects `Forever, only this exact argv tuple` (`forever_argv`)
and approves. Then:
1. Repeat `qsu /bin/echo hello` → cache hit, no prompt, runs.
2. Run `qsu /bin/echo hi` → cache miss (different argv tuple),
   admin app shows a new pending row.
3. Run `qsu /bin/echo hello` once more with an EXTRA argument
   `qsu /bin/echo hello world` → cache miss too (argv tuple is
   length-3 now, not equal to length-2 stored row).

**Why**: `doc/sudo.md` Approval scope table calls out
`Forever this exact command` (cache match_kind=`argv_exact`) as
"Most common forever scope" because it gives admin a durable grant
that won't bleed across argument variations. The whole point of
the task(069) cache rewrite was to fix qsu's argv-leak — a
`forever_argv` of `[apt-get, update]` must NOT cache-hit
`[apt-get, install, foo]`. Scenario s57 covers the broker-side
match in isolation; this one validates the user-visible path:
admin click → cache row → subsequent qsu binary invocations
either hit or re-prompt as the admin would expect.

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

### S1 — qsu /bin/echo hello triggers pending

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /bin/echo hello \
  >/tmp/44-echo1.log 2>&1 & echo $! >/tmp/44-echo1.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/44-s1-pending.png
```

**Assert**: one pending row visible; details include
`argv=/bin/echo hello`.

### S2 — admin picks `forever_argv` and approves

```bash
$VMGUI "$VM" screenshot /tmp/44-s2a-radios.png

# Select the "Forever, only this exact argv tuple" scope via the admin app's
# dedicated keyboard shortcut (Ctrl+Shift+6 → forever_argv; scope order at
# admin_app/qdistro_admin_app.py:2040-2046). The previous step asked the runner to
# MOUSE-CLICK the 6th radio, but click input to the XWayland Qt app is
# platform-blocked on the CI template (AGENTS.md 3a/3b), so the click intermittently
# missed and the approval landed scope=once. The keyboard-shortcut path is
# deterministic; the s2b screenshot below still visually confirms forever_argv is
# filled, so the assertion stays the tripwire (no masking).
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_LEFTSHIFT KEY_6
sleep 1
$VMGUI "$VM" screenshot /tmp/44-s2b-selected.png
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 2

$VMEXEC "$VM" 'wait $(cat /tmp/44-echo1.pid) 2>/dev/null; cat /tmp/44-echo1.log'
```

**Assert**:
- `/tmp/44-s2b-selected.png` shows `forever_argv` radio filled.
- `/tmp/44-echo1.log` contains `hello`.
- Cache:
  ```bash
  SQL_B64=$(base64 -w0 <<'SQL_EOF'
  SELECT match_kind, scope, argv FROM approvals
    WHERE action='qsu.exec:root';
  SQL_EOF
  )
  $VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
  ```
  Output: `argv_exact|forever_argv|...`. The `argv` column is the
  JSON-encoded list `["/bin/echo","hello"]` (whatever encoding the
  cache uses; the load-bearing column is `match_kind=argv_exact`).

### S3 — repeat: same argv → cache hit, no prompt

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /bin/echo hello \
  >/tmp/44-echo2.log 2>&1 & echo $! >/tmp/44-echo2.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/44-s3-stillempty.png
$VMEXEC "$VM" 'wait $(cat /tmp/44-echo2.pid) 2>/dev/null; cat /tmp/44-echo2.log'
```

**Assert**:
- `/tmp/44-s3-stillempty.png`: pending list empty.
- `/tmp/44-echo2.log` is `hello\n`.

### S4 — different argv (echo hi) → re-prompt

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /bin/echo hi \
  >/tmp/44-echo3.log 2>&1 & echo $! >/tmp/44-echo3.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/44-s4-pending.png
```

**Assert** (`/tmp/44-s4-pending.png`): one pending row, details
show `argv=/bin/echo hi` — the forever_argv cache row for
`[echo, hello]` did NOT match the different argv tuple.

### S5 — extra argument also re-prompts

Deny the S4 prompt first (Ctrl+N), then issue the longer argv.

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 1
$VMEXEC "$VM" 'wait $(cat /tmp/44-echo3.pid) 2>/dev/null; cat /tmp/44-echo3.log'

B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /bin/echo hello world \
  >/tmp/44-echo4.log 2>&1 & echo $! >/tmp/44-echo4.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/44-s5-pending.png
```

**Assert**: `/tmp/44-s5-pending.png` shows a pending row with
`argv=/bin/echo hello world`. A length-3 argv ≠ the stored
length-2 tuple → cache miss → admin prompt re-appears.

### S6 — clean up the pending prompt

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 1
$VMEXEC "$VM" 'wait $(cat /tmp/44-echo4.pid) 2>/dev/null; cat /tmp/44-echo4.log'
```

**Assert**: log contains `request denied`.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/44-*.log /tmp/44-*.pid'
B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Notes for the runner

- This is the GUI counterpart of `s57-qsu-argv-scopes.sh` phase 1.
  The intent here is to pin admin's UX expectation; s57 already
  pins the broker's cache match logic.
- If S3 puts a new pending row instead of running silently,
  `forever_argv` cache match is broken — likely a `match_kind`
  regression in `_cache_hit` checking against `argv_exact` ==
  `argv_exact` literal equality. Capture cache contents in the
  FAIL.
