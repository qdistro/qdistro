# 51 — `qsu -u <user>` carries target_user in the action key

**What**: as `admin` (uid 1000) invoke `qsu -u work /usr/bin/id`.
The pending request's action MUST be `qsu.exec:work`, NOT
`qsu.exec:root`. Approve `forever_argv`; verify the streamed
output of `id` reports uid=2000 (work), gid=2000, groups=…
(work's groups). Then issue a SECOND qsu call with target_user
root — `qsu -u root /usr/bin/id`. The first call's cache row
must NOT short-circuit it: a new pending row appears for
`qsu.exec:root`, because the cache key is `(uid, action)` and
the actions differ.

**Why**: `doc/sudo.md` §"Target user" + the broker source comment
at `_ask_broker` ("Action name includes target_user so a single
admin click can't grant 'run anything as root' when what they saw
was 'run id as nobody'") make this an explicit security claim.
Without per-target action keys, admin approving `qsu -u nobody
some-tool` would inadvertently allow the same argv at root. No
existing test covers the target_user-in-action shape end-to-end —
s58 only exercises target=root.

## Setup

```bash
VM=${VMNAME:-qd-sudo}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin -f qsu 2>/dev/null; true'
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

### S1 — qsu -u work /usr/bin/id from admin

Admin invokes qsu targeting `work` (a non-root target). The broker
recognises the delegated path because peer uid is non-zero ... wait:
actually the peer uid of qsu's client is admin (1000) NOT root.
The delegated identity in the broker is what's claimed via
`RequestPermissionAs(caller_uid=admin_uid, ...)`. The action key
is the load-bearing surface.

```bash
B64=$(base64 -w0 <<'EOF'
# Run qsu as admin (not as work) — admin is allowed to escalate.
runuser -u admin -- bash -c '/usr/local/bin/qsu -u work /usr/bin/id \
  >/tmp/51-id-work.log 2>&1 & echo $! >/tmp/51-id-work.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/51-s1-pending.png
```

**Assert** (`/tmp/51-s1-pending.png`):
- One pending row visible.
- **Action: `qsu.exec:work`** (NOT `qsu.exec:root`).
- Details contain `target_user=work`, `argv=/usr/bin/id`.

### S2 — admin picks `forever_argv` and approves

```bash
$VMGUI "$VM" screenshot /tmp/51-s2a-radios.png

# Select the "Forever, only this exact argv tuple" scope via the admin app's
# dedicated keyboard shortcut (Ctrl+Shift+6 → forever_argv; scope order at
# admin_app/qdistro_admin_app.py:2040-2046). The previous step asked the runner to
# MOUSE-CLICK the 6th radio, but click input to the XWayland Qt app is
# platform-blocked on the CI template (AGENTS.md 3a/3b): ~1-in-2 runs the click
# missed, the approval landed scope=once, and the strict cache-row assertion below
# FAILED. The keyboard-shortcut path is deterministic and reaches the SAME state
# the test asserts — the scope=forever_argv assertion stays the tripwire, so a
# regression in the shortcut / _set_scope path still FAILS loud (no masking).
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_LEFTSHIFT KEY_6
sleep 1
$VMGUI "$VM" screenshot /tmp/51-s2b-selected.png
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 2

$VMEXEC "$VM" 'wait $(cat /tmp/51-id-work.pid) 2>/dev/null; cat /tmp/51-id-work.log'
```

**Assert**:
- `/tmp/51-id-work.log` contains `uid=2000(work)` and
  `gid=2000(work)` — the id command ran AS work, not root.
- Cache row:
  ```bash
  SQL_B64=$(base64 -w0 <<'SQL_EOF'
  SELECT caller_uid, action, scope FROM approvals
    WHERE action LIKE 'qsu.exec:%';
  SQL_EOF
  )
  $VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
  ```
  Output: `1000|qsu.exec:work|forever_argv`. The caller_uid is
  admin's uid (1000) and the action's target is `work`.

### S3 — same argv targeting `root` does NOT cache-hit

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- bash -c '/usr/local/bin/qsu -u root /usr/bin/id \
  >/tmp/51-id-root.log 2>&1 & echo $! >/tmp/51-id-root.pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
$VMGUI "$VM" screenshot /tmp/51-s3-pending-root.png
```

**Assert** (`/tmp/51-s3-pending-root.png`):
- A NEW pending row appears.
- Action: `qsu.exec:root` (different from S2's stored `qsu.exec:work`).
- Details contain `target_user=root`.

The forever_argv cache row for `(uid=1000,
action=qsu.exec:work)` did NOT short-circuit a request for
`(uid=1000, action=qsu.exec:root)`. This is the load-bearing
target_user-in-action property.

### S4 — deny the root call to clean up

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
sleep 1
$VMEXEC "$VM" 'wait $(cat /tmp/51-id-root.pid) 2>/dev/null; cat /tmp/51-id-root.log'
```

**Assert**: log contains `request denied`, qsu rc=1.

### S5 — audit rows are keyed by distinct actions

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT action, decision FROM audit
  WHERE action LIKE 'qsu.exec:%'
  ORDER BY id DESC LIMIT 5;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

**Assert**: Two newest rows have distinct actions:
- `qsu.exec:root|0` (S4 deny).
- `qsu.exec:work|1` (S2 allow).

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/51-*.log /tmp/51-*.pid'
B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Notes for the runner

- This scenario flips the usual role: the qsu invoker is **admin**,
  not work. Admin → work is a legitimate flow ("admin drops to
  work's silo to run something"). The broker still treats it as
  delegated (qsu's qdistro-root-exec service runs as root and
  calls RequestPermissionAs with admin as the claimed caller).
- The bus policy may need to permit non-root delegators if the
  broker's `RequestPermissionAs restricted to root delegator`
  check fires. qsu calls always go through qdistro-root-exec
  which IS root, so the check passes; if S1 fails with
  `restricted to root delegator`, that's a broker policy bug,
  not a scenario problem.
- The `id` output may include extra group memberships; the
  load-bearing substrings are `uid=2000(work)` and `gid=2000`.
