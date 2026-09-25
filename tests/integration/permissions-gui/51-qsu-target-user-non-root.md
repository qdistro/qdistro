# 51 — `qsu -u <user>` carries target_user in the action key

<!-- qci:visual: required -->

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
# Stale S3 gate from a prior run must not exist. S3 awaits this file
# and must not see it until the host touches it after S2 is settled.
$VMEXEC "$VM" 'rm -f /tmp/51-go-s3'
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
source /tmp/qci-gui-waiters.sh
bg_start 51-id-work admin '/usr/local/bin/qsu -u work /usr/bin/id'
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
# Exactly one Ctrl+Y. Do not send it again later in this scenario.
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y
sleep 2

# bg_wait, never `wait $(cat X.pid)` — that does not wait in a separate guest
# shell (AGENTS.md, "A backgrounded job"). A TIMEOUT here IS this step's failure.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 51-id-work 60'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log 51-id-work; echo "rc=$(bg_rc 51-id-work)"'
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

S2 is settled only after that cache row is observed. Only then, and
not before, create the guest marker with a separate vm-exec. Do not
send Ctrl+Y again after this marker; a second Ctrl+Y is a driver
error. Do not start the root request in the same guest script as the
S2 approval key — that key stays the single host `virsh send-key`
above, and `bg_start 51-id-root` belongs only to S3.

```bash
$VMEXEC "$VM" 'touch /tmp/51-go-s3'
```

### S3 — same argv targeting `root` does NOT cache-hit

The root `qsu` must not start until `/tmp/51-go-s3` exists. That file
is the host gate from the end of S2. If the marker is missing,
`await_file` times out: that is a driver ERROR (exit nonzero), not a
product FAIL. Do not treat it as a cache-hit failure, and do not send
Ctrl+Y to paper over it.

```bash
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
# Missing /tmp/51-go-s3 is a driver ERROR, not a product FAIL.
if ! await_file /tmp/51-go-s3 30; then
  echo "ERROR: /tmp/51-go-s3 missing; S2 was not settled before the root request" >&2
  exit 1
fi
bg_start 51-id-root admin '/usr/local/bin/qsu -u root /usr/bin/id'
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
# bg_wait, never `wait $(cat X.pid)` — that does not wait in a separate guest
# shell (AGENTS.md, "A backgrounded job"). A TIMEOUT here IS this step's failure.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 51-id-root 60'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log 51-id-root; echo "rc=$(bg_rc 51-id-root)"'
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
$VMEXEC "$VM" 'rm -f /tmp/51-*.log /tmp/51-*.pid /tmp/51-go-s3'
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
- S3's root `qsu` is gated on `/tmp/51-go-s3`. Touch that marker
  only after the S2 cache row `1000|qsu.exec:work|forever_argv` is
  observed, via its own vm-exec. Do not send Ctrl+Y again after
  this marker; a second Ctrl+Y is a driver error. A missing marker
  is ERROR (the guest script exits nonzero), not a product FAIL.
  Do not start `51-id-root` in the same guest script as the S2
  approval key.
