# 03 — failure mid-step → secrets scrubbed → run marked failed

**What**: a workflow delivers a real secret into a per-run ssh-agent and
then runs a step that fails. Verify that (a) the run is marked `failed` in
both the audit DB and the admin Workflows tab, and (b) the delivered
secret was still scrubbed despite the failure — the agent socket is gone.

**Why**: scrub-on-success is the easy path; the dangerous one is failure.
A bug that scrubs only on the happy path leaves credentials live whenever
a later step errors — exactly when the system is already in a bad state.
This scenario forces a mid-step failure *after* a successful delivery and
proves the engine fails closed: secret revoked, run audited as failed.

## Setup

```bash
VM=${VMNAME:-$(virsh list --name --state-running | head -1)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

B64=$(base64 -w0 <<'EOF'
pkill -u admin -f qdistro_admin_app 2>/dev/null || true
rm -f /etc/qdistro/workflows/wfgui-*.yaml 2>/dev/null || true
rmdir /sys/fs/cgroup/system.slice/qdistro-failstep-test.scope 2>/dev/null || true

# Provision a real key into vault 'dev'.
ssh-keygen -t ed25519 -N '' -q -f /tmp/wf-fail-key
qdistro-pwd-admin create dev 2>/dev/null || true
qdistro-pwd-admin unlock dev 2>/dev/null || true
cat /tmp/wf-fail-key | qdistro-pwd-admin add dev fail-key
rm -f /tmp/wf-fail-key /tmp/wf-fail-key.pub

# Seed a workflow: deliver the secret, then call a NON-whitelisted broker
# method (DecideRequest) which the engine refuses -> the step fails.
mkdir -p /etc/qdistro/workflows
cat > /etc/qdistro/workflows/wfgui-failstep.yaml <<'YAML'
- name: wfgui-failstep
  description: deliver a secret then fail on a forbidden call_broker
  trigger:
    type: process_spawn
    cgroup_pattern: "system.slice/qdistro-failstep-*.scope"
    poll_interval: 0.25
  needs:
    - vault/dev/fail-key
  steps:
    - deliver_secret:
        item: vault/dev/fail-key
        as: ssh-agent
        scrub_on: workflow_exit
    - type: call_broker
      method: DecideRequest
YAML

systemctl restart qdistro-admin-broker.service
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 4
```

## Steps

### S1 — fire the workflow (spawn into the watched cgroup)

```bash
B64=$(base64 -w0 <<'EOF'
CG=/sys/fs/cgroup/system.slice/qdistro-failstep-test.scope
mkdir -p "$CG"
setsid sleep 5 </dev/null >/dev/null 2>&1 &
echo $! > /tmp/wf-fail-target.pid
echo "$(cat /tmp/wf-fail-target.pid)" > "$CG/cgroup.procs"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# The deliver step succeeds, then call_broker(DecideRequest) is rejected
# immediately (not whitelisted) -> run fails fast. Give it a moment.
sleep 3
```

### S2 — the run is recorded as failed, with the secret scrubbed

```bash
B64=$(base64 -w0 <<'EOF'
echo "--- latest run state ---"
sqlite3 /var/lib/qdistro/audit/workflow_audit.sqlite \
  "SELECT state FROM workflow_runs WHERE workflow_name='wfgui-failstep' ORDER BY started_at DESC LIMIT 1;"
echo "--- step outcomes (oldest->newest) ---"
sqlite3 /var/lib/qdistro/audit/workflow_audit.sqlite \
  "SELECT s.step_type, s.success FROM workflow_steps s JOIN workflow_runs r ON s.run_id=r.run_id WHERE r.workflow_name='wfgui-failstep' ORDER BY s.started_at;"
echo "--- remaining agent sockets ---"
find /run/qdistro/workflow-secrets -name 'agent.sock' 2>/dev/null
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert (failed run, scrubbed secret):**
- The latest `wfgui-failstep` run state is `failed`.
- The step rows show `deliver_secret` success `1` followed by
  `call_broker` success `0` (delivery worked; the forbidden call failed).
- The "remaining agent sockets" find prints nothing — the secret was
  scrubbed even though the run failed mid-step.

### S3 — admin Workflows tab shows the failed run with an error

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/03-failure-mid-step-scrub-failed-run-s3-tabs.png
# Runner: OCR-click the "Workflows" tab, then screenshot.
# $VMGUI "$VM" click <cx> <cy>
sleep 0.5
$VMGUI "$VM" screenshot /tmp/03-failure-mid-step-scrub-failed-run-s3-workflows.png
```

**Assert (admin shows failure):**
- The Recent runs table has a `wfgui-failstep` row with state `failed`.
- That row's `error` column is non-empty (a `call_broker ... not in
  whitelist` message), not `-`.

## Teardown

```bash
B64=$(base64 -w0 <<'EOF'
pkill -u admin -f qdistro_admin_app 2>/dev/null || true
kill "$(cat /tmp/wf-fail-target.pid)" 2>/dev/null || true
rm -f /tmp/wf-fail-target.pid
rm -f /etc/qdistro/workflows/wfgui-*.yaml 2>/dev/null || true
rmdir /sys/fs/cgroup/system.slice/qdistro-failstep-test.scope 2>/dev/null || true
qdistro-pwd-admin delete dev fail-key 2>/dev/null || true
systemctl restart qdistro-admin-broker.service
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'rm -f /tmp/03-failure-mid-step-scrub-failed-run-*.png 2>/dev/null || true'
```

## Notes for the runner

- The forbidden `call_broker: DecideRequest` is the deliberate failure
  injection — it is rejected by the engine's method whitelist before any
  broker state changes, so it's a safe, deterministic way to fail step 2.
- The load-bearing assertion is S2's "remaining agent sockets" being
  empty. A surviving socket here is a credential-leak FAIL — quote the
  path and the failed run state together as the justification.
