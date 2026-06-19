# 43 — qsu request reaches admin app with argv-aware scope radios

**What**: invoke `/usr/local/bin/qsu /bin/true` as `work` (uid
2000). The admin app's pending list shows one row whose action is
`qsu.exec:root`. The detail pane's Scope group shows EIGHT radios
in this exact order: `Just this once`, `1 hour`, `24 hours`,
`Forever, any command`, `Forever, only this exact program`,
`Forever, only this exact argv tuple`, `Forever, this argv
basename anywhere`, `Forever, this argv prefix + any trailing args`.
Approve the request with the default `once` scope; verify qsu
runs `/bin/true` and exits rc=0; verify NO cache row is written
(once-scope is not persisted).

**Why**: `doc/sudo.md` §"Approval scopes" defines the picker as
the admin's only mechanism to choose persistence granularity at
approval time. The argv-aware Forever scopes (`forever_argv` /
`forever_basename` / `forever_prefix`) were added in task(072) but
no GUI test confirms they're rendered in the admin app — only the
unit test for `_VALID_SCOPES` knows they exist. A regression
hiding them behind a feature flag, an enum re-order, or a
`if hasattr(self, '_argv_scopes')` typo would silently revert the
UI to pre-task(072) state without breaking any backend test.

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
```

## Steps

### S1 — launch admin app

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
$VMGUI "$VM" screenshot /tmp/43-s1-empty.png
```

**Assert**: pending list empty.

### S2 — qsu invocation triggers a pending request

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /bin/true \
  >/tmp/43-qsu.log 2>&1 & echo $! >/tmp/43-qsu.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/43-s2-pending.png
```

**Assert** (`/tmp/43-s2-pending.png`):
- One row in the pending list. Visible columns include
  `uid=2000`, `action=qsu.exec:root`.
- The detail pane (right side or below the table) reads:
  - `uid=2000  pid=<some pid>`.
  - `Action: qsu.exec:root`.
  - `exe` is the qsu client's process executable. In the current
    Python-installed qsu path this is expected to be `/usr/bin/python`
    or `/usr/bin/python3.13`; the requested command itself is verified
    through the `argv` details below.
  - `Details: ...` containing `argv=/bin/true` (and
    `argv[00]=/bin/true`, `target_user=root`).
- The Scope panel renders all eight radios with these EXACT labels
  (order matters for `_scope_buttons` enumeration in
  `admin_app/qdistro_admin_app.py`):
  1. `Just this once` (default; filled)
  2. `1 hour`
  3. `24 hours`
  4. `Forever, any command`
  5. `Forever, only this exact program`
  6. `Forever, only this exact argv tuple`
  7. `Forever, this argv basename anywhere`
  8. `Forever, this argv prefix + any trailing args`

  Use OCR to capture each label; if any label is missing, that's
  the regression to flag.

### S3 — approve with default `once` scope

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 2

$VMEXEC "$VM" 'wait $(cat /tmp/43-qsu.pid) 2>/dev/null; cat /tmp/43-qsu.log; echo "qsu-rc=$?"'
$VMGUI "$VM" screenshot /tmp/43-s3-afterapprove.png
```

**Assert**:
- qsu process exited rc=0 (`/bin/true` succeeded).
- `/tmp/43-s3-afterapprove.png` shows the pending list empty
  again — the row was consumed by the approve.

### S4 — once-scope does NOT persist a cache row

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT COUNT(*), GROUP_CONCAT(match_kind || '/' || scope, ', ')
  FROM approvals WHERE action LIKE 'qsu.exec:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
```

**Assert**: count is `0` and the GROUP_CONCAT is empty/NULL.
The audit row exists but no cache row — per
`scope_to_row("once")` returning `None` in
`broker/qdistro_admin_cache.py`.

### S5 — second qsu /bin/true re-prompts (no cache to hit)

```bash
B64=$(base64 -w0 <<'EOF'
sudo -u work bash -c '/usr/local/bin/qsu /bin/true \
  >/tmp/43-qsu2.log 2>&1 & echo $! >/tmp/43-qsu2.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/43-s5-pending-again.png
```

**Assert**: pending list has one row again — the once-scope from
S3 left nothing behind, so the same invocation re-prompts.

### S6 — deny the second invocation to clean up

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 1
$VMEXEC "$VM" 'wait $(cat /tmp/43-qsu2.pid) 2>/dev/null; cat /tmp/43-qsu2.log'
```

**Assert**: log contains `request denied`, qsu rc=1.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/43-*.log /tmp/43-*.pid'
B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Notes for the runner

- The `Action:` label string in the detail pane reads exactly
  `Action: qsu.exec:root` — the target_user suffix is part of the
  broker action key (see `admin_broker._action_for_qsu`). If you
  see `Action: qsu.exec` (no `:root`), the broker is mis-naming
  the action and any rule keyed `qsu.exec:root` won't match — a
  real regression worth filing.
- Approve / Deny keyboard chords go via `virsh send-key` (Ctrl+Y
  / Ctrl+N); `vm-gui key ctrl+y` does NOT work consistently with
  XWayland under the GUI test compositor (see AGENTS.md pitfall 3).
- This scenario is the entry point for the qsu series — 44-46
  exercise the same shape but pick different forever-* scopes.
