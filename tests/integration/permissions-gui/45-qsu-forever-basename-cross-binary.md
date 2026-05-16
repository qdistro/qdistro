# 45 — `forever_basename` cross-binary: same basename hits, different basename misses

**What**: install `forever_basename` for `qsu /usr/bin/python3 -c …`.
Then issue three follow-up qsu calls and verify:
1. `qsu /usr/bin/python3 -c 'pass'` → cache hit (same exact argv).
2. `qsu /usr/local/bin/python3 -c 'pass'` → cache hit (same
   basename `python3`, different argv[0] path).
3. `qsu /usr/bin/perl -e1` → re-prompt (basename `perl` ≠
   `python3`).

**Why**: `doc/sudo.md` Approval scope table calls
`forever_basename` (`match_kind=basename`) the right scope when
admin wants to "loosen the path; tighten the command identity" —
the daily admin reality is "I move binaries around between PATH
entries, but `python3` is `python3` is `python3` and I shouldn't
have to re-approve when an update shuffles paths." This is also
the most-likely-to-leak-by-collision scope (a basename like
`bash` matches more than admin probably meant). No GUI test
currently exercises the cross-path match; s57 covers it as a
D-Bus probe.

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

# Ensure an alternate python3 path exists so the cross-path test
# is meaningful. /usr/local/bin/python3 is a symlink we create
# expressly for this scenario; rm in Teardown.
$VMEXEC "$VM" 'ln -sf /usr/bin/python3 /usr/local/bin/python3-45-symlink'

$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
```

## Steps

### S1 — first qsu /usr/bin/python3 pends

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /usr/bin/python3 -c "print(\"py1\")" \
  >/tmp/45-py1.log 2>&1 & echo $! >/tmp/45-py1.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/45-s1-pending.png
```

**Assert**: pending row visible; details contain `/usr/bin/python3`.

### S2 — admin picks `forever_basename` and approves

```bash
$VMGUI "$VM" screenshot /tmp/45-s2a-radios.png
# Runner: click "Forever, this argv basename anywhere" (7th radio).
$VMGUI "$VM" screenshot /tmp/45-s2b-selected.png

B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 2

$VMEXEC "$VM" 'wait $(cat /tmp/45-py1.pid) 2>/dev/null; cat /tmp/45-py1.log'
```

**Assert**:
- `/tmp/45-s2b-selected.png` shows `forever_basename` radio filled.
- `/tmp/45-py1.log` contains `py1`.
- Cache row:
  ```bash
  SQL_B64=$(base64 -w0 <<'SQL_EOF'
  SELECT match_kind, match_value, scope FROM approvals
    WHERE action='qsu.exec:root';
  SQL_EOF
  )
  $VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
  ```
  Output: `basename|python3|forever_basename`. The match_value
  is the basename only (NOT the full argv[0] path) — the cache
  layer extracts `basename(argv[0])` at decide time.

### S3 — same exact argv → cache hit

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /usr/bin/python3 -c "print(\"py2\")" \
  >/tmp/45-py2.log 2>&1 & echo $! >/tmp/45-py2.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/45-s3-stillempty.png
$VMEXEC "$VM" 'wait $(cat /tmp/45-py2.pid) 2>/dev/null; cat /tmp/45-py2.log'
```

**Assert**: pending list empty; log contains `py2`.

### S4 — different argv[0] path, SAME basename → cache hit

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /usr/local/bin/python3-45-symlink -c "print(\"py3\")" \
  >/tmp/45-py3.log 2>&1 & echo $! >/tmp/45-py3.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/45-s4-stillempty.png
$VMEXEC "$VM" 'wait $(cat /tmp/45-py3.pid) 2>/dev/null; cat /tmp/45-py3.log'
```

**Assert**:
- `/tmp/45-s4-stillempty.png`: pending list empty (cache hit).
- `/tmp/45-py3.log`: contains `py3`.

Note: `python3-45-symlink` is the symlink we created in Setup; the
cache row's basename is `python3` (from S2) — but the second call
uses argv[0] basename `python3-45-symlink`. If this still hits,
the cache match is on `_argv_basename(argv)` which IS extracted
fresh from each call's argv[0], and matches against stored
`match_value`. Wait — actually our test will fail in this
configuration because basenames differ. The correct cross-path
test must use an argv[0] whose basename IS `python3`. Adjust:
use `/usr/local/bin/python3` as the alternate path. Re-execute
S4 expecting cache hit if and only if `/usr/local/bin/python3`
exists and has basename `python3`.

```bash
# Confirm an alternate path exists with basename 'python3':
$VMEXEC "$VM" 'test -x /usr/local/bin/python3 && echo HAVE_LOCAL_PYTHON3 || echo NO_LOCAL_PYTHON3'
```

If `NO_LOCAL_PYTHON3`, create one:

```bash
$VMEXEC "$VM" 'ln -sf /usr/bin/python3 /usr/local/bin/python3'

B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /usr/local/bin/python3 -c "print(\"py3a\")" \
  >/tmp/45-py3a.log 2>&1 & echo $! >/tmp/45-py3a.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/45-s4a-stillempty.png
$VMEXEC "$VM" 'wait $(cat /tmp/45-py3a.pid) 2>/dev/null; cat /tmp/45-py3a.log'
```

**Assert**: pending list empty; log shows `py3a` — cache hit on
basename `python3` regardless of argv[0] path.

### S5 — different basename (`perl`) → re-prompt

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /usr/bin/perl -e "print qq(perl1\\n)" \
  >/tmp/45-perl.log 2>&1 & echo $! >/tmp/45-perl.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/45-s5-pending.png
```

**Assert** (`/tmp/45-s5-pending.png`): one pending row, details
show `argv=/usr/bin/perl -e ...`. Basename `perl` ≠ `python3` →
cache row did NOT match.

### S6 — deny perl to clean up

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 1
$VMEXEC "$VM" 'wait $(cat /tmp/45-perl.pid) 2>/dev/null; cat /tmp/45-perl.log'
```

**Assert**: log contains `request denied`.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/45-*.log /tmp/45-*.pid'
$VMEXEC "$VM" 'rm -f /usr/local/bin/python3-45-symlink'
# Only remove /usr/local/bin/python3 if we created it (symlink to
# /usr/bin/python3 specifically); don't yank a real install.
$VMEXEC "$VM" 'if [ -L /usr/local/bin/python3 ] && [ "$(readlink /usr/local/bin/python3)" = "/usr/bin/python3" ]; then rm -f /usr/local/bin/python3; fi'
B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Notes for the runner

- The cache layer stores match_value as `basename(argv[0])` at
  decide time, and at lookup time computes `basename(req.argv[0])`
  the same way. A bug where one side uses `os.path.basename` and
  the other uses `argv[0].rsplit("/")[-1]` would only show up if
  argv[0] has unusual path components (e.g. `./python3`). Not
  worth a separate scenario; mention in notes if you find one
  during teardown.
- This scenario is the GUI counterpart of s57's
  `forever_basename` phase 2 ("different argv[0] same basename
  → hit"). The point of repeating it here is to pin the admin
  app surface: that the radio labelled `Forever, this argv
  basename anywhere` actually maps to the broker's
  `forever_basename` scope key (not, say, the legacy
  `forever_exe` due to a label/value mismatch in the
  `_scope_buttons` tuple).
