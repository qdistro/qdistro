# 02 — secret delivery: ephemeral ssh-agent socket exists, then gone

**What**: a `process_spawn` git-sign workflow delivers a real SSH key from
the vault into a per-run ssh-agent and waits on the triggering process.
While that process lives, assert the agent socket EXISTS (and holds the
key); after the process exits and the run completes, assert the socket and
its directory are GONE — and that the key material never hit disk, audit,
or logs.

**Why**: this is the crown-jewel security property of the engine — a
secret is released for exactly the lifetime of the consuming task and then
scrubbed. A scrub that silently no-ops, or a socket that survives the run,
is a credential leak that no unit test on the host can prove against a
real vault + real ssh-agent under systemd. This scenario is the live
ground truth for `deliver_secret` → `wait_for_process` → scrub.

## Setup

```bash
VM=${VMNAME:-$(virsh list --name --state-running | head -1)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

B64=$(base64 -w0 <<'EOF'
pkill -u admin -f qdistro_admin_app 2>/dev/null || true
rm -f /etc/qdistro/workflows/wfgui-*.yaml 2>/dev/null || true
rmdir /sys/fs/cgroup/system.slice/qdistro-git-sign-test.scope 2>/dev/null || true

# 1. Provision a real ssh key into the 'dev' vault as tag github-ssh-key.
ssh-keygen -t ed25519 -N '' -q -f /tmp/wf-sign-key
qdistro-pwd-admin create dev 2>/dev/null || true
qdistro-pwd-admin unlock dev 2>/dev/null || true
cat /tmp/wf-sign-key | qdistro-pwd-admin add dev github-ssh-key
shred -u /tmp/wf-sign-key /tmp/wf-sign-key.pub 2>/dev/null || rm -f /tmp/wf-sign-key /tmp/wf-sign-key.pub

# 2. Seed the git-sign workflow watching a test cgroup.
mkdir -p /etc/qdistro/workflows
cat > /etc/qdistro/workflows/wfgui-git-sign.yaml <<'YAML'
- name: wfgui-git-sign
  description: deliver SSH signing key for one process, then scrub
  trigger:
    type: process_spawn
    cgroup_pattern: "system.slice/qdistro-git-sign-*.scope"
    poll_interval: 0.25
  needs:
    - vault/dev/github-ssh-key
  steps:
    - deliver_secret:
        item: vault/dev/github-ssh-key
        as: ssh-agent
        expose_as: SSH_AUTH_SOCK
        ttl: 120
        scrub_on: workflow_exit
    - wait_for_process: $trigger.pid
YAML

systemctl restart qdistro-admin-broker.service
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 4   # broker up; process_spawn poller has baselined the empty cgroup
```

## Steps

### S1 — spawn a process into the watched cgroup (fires the workflow)

```bash
B64=$(base64 -w0 <<'EOF'
CG=/sys/fs/cgroup/system.slice/qdistro-git-sign-test.scope
mkdir -p "$CG"
setsid sleep 30 </dev/null >/dev/null 2>&1 &
echo $! > /tmp/wf-target.pid
# Move it in AFTER the poller baselined, so it's seen as a NEW spawn.
echo "$(cat /tmp/wf-target.pid)" > "$CG/cgroup.procs"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2   # let the trigger fire + deliver_secret create the agent
```

### S2 — while the process lives, the agent socket exists and holds the key

```bash
B64=$(base64 -w0 <<'EOF'
# The engine roots per-run secret dirs here.
find /run/qdistro/workflow-secrets -name 'agent.sock' 2>/dev/null
SOCK=$(find /run/qdistro/workflow-secrets -name 'agent.sock' 2>/dev/null | head -1)
if [ -n "$SOCK" ]; then
  echo "SOCK=$SOCK"
  SSH_AUTH_SOCK="$SOCK" ssh-add -l >/dev/null 2>&1 && echo AGENT_HAS_KEY || echo AGENT_NO_KEY
else
  echo NO_SOCK
fi
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert (socket present during run):**
- The output includes a `SOCK=/run/qdistro/workflow-secrets/ssh-*/agent.sock`
  line (the per-run agent socket exists while the process is alive).
- The output includes `AGENT_HAS_KEY` (the real key was loaded into the
  agent — delivery actually happened end-to-end).

### S3 — admin app shows the run as running/in-flight

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/02-secret-delivery-ephemeral-socket-s3-tabs.png
# Runner: OCR-click the "Workflows" tab label, then screenshot.
# $VMGUI "$VM" click <cx> <cy>
sleep 0.5
$VMGUI "$VM" screenshot /tmp/02-secret-delivery-ephemeral-socket-s3-workflows.png
```

**Assert (admin sees the workflow):**
- The Workflows table lists `wfgui-git-sign` with trigger `process_spawn`,
  steps `2`, and needs containing `vault/dev/github-ssh-key`.
- The Recent runs table has a `wfgui-git-sign` row whose state is
  `running` (the wait_for_process step is still blocked on the live pid).

### S4 — end the process; the socket is scrubbed and the run completes

```bash
$VMEXEC "$VM" 'kill "$(cat /tmp/wf-target.pid)" 2>/dev/null; true'
sleep 3   # wait_for_process returns, run completes, scrub runs

B64=$(base64 -w0 <<'EOF'
echo "remaining sockets:"
find /run/qdistro/workflow-secrets -name 'agent.sock' 2>/dev/null
echo "remaining dirs:"
ls -A /run/qdistro/workflow-secrets 2>/dev/null
# Key material must never have hit the workflow audit DB.
sqlite3 /var/lib/qdistro/audit/workflow_audit.sqlite \
  "SELECT state FROM workflow_runs WHERE workflow_name='wfgui-git-sign' ORDER BY started_at DESC LIMIT 1;"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert (socket gone after exit):**
- The "remaining sockets" find prints nothing (no `agent.sock` survives).
- The per-run secret dir was removed (no `ssh-*` dir lingers under
  `/run/qdistro/workflow-secrets`).
- The latest `wfgui-git-sign` run state is `completed`.

### S5 — no plaintext key leaked into the audit DB

```bash
B64=$(base64 -w0 <<'EOF'
# Dump the whole workflow audit DB as text and grep for any private-key
# marker. The engine records item NAMES only, never values.
sqlite3 /var/lib/qdistro/audit/workflow_audit.sqlite '.dump' \
  | grep -c 'PRIVATE KEY' || true
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert (no secret in audit):**
- The grep count is `0` — no `PRIVATE KEY` material anywhere in the audit
  DB dump.

## Teardown

```bash
B64=$(base64 -w0 <<'EOF'
pkill -u admin -f qdistro_admin_app 2>/dev/null || true
kill "$(cat /tmp/wf-target.pid)" 2>/dev/null || true
rm -f /tmp/wf-target.pid
rm -f /etc/qdistro/workflows/wfgui-*.yaml 2>/dev/null || true
rmdir /sys/fs/cgroup/system.slice/qdistro-git-sign-test.scope 2>/dev/null || true
qdistro-pwd-admin delete dev github-ssh-key 2>/dev/null || true
systemctl restart qdistro-admin-broker.service
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'rm -f /tmp/02-secret-delivery-ephemeral-socket-*.png 2>/dev/null || true'
```

## Notes for the runner

- The whole point is the *transition*: S2 must show the socket PRESENT and
  S4 must show it GONE. If S2 shows `NO_SOCK`, delivery never happened —
  check the vault is unlocked and the cgroup pattern matched (a real FAIL).
  If S4 still shows a socket, scrub failed — that is a credential-leak
  FAIL; quote the surviving path.
- S3 is a timing window: the admin app must be opened while `sleep 30` is
  still alive. If you're slow, bump the `sleep 30` in S1 to `sleep 90`.
- Do not restart the broker between S1 and S4 — it would scrub the secret
  for the wrong reason (shutdown sweep) and mask a real scrub-on-exit bug.
