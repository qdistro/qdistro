# 01 — one trigger → one run → audit row chain

**What**: seed a cron workflow with a short interval, let it fire at
least one run inside the broker, then verify the run is visible both in
the admin app's Workflows tab (Recent runs, state `completed`) and in the
workflow audit DB (a `workflow_runs` row plus its step rows).

**Why**: this is the spine of the whole engine — a registered trigger
must actually fire a run on the broker's shared loop, the run must execute
its steps, and the run must be durably audited. If any link breaks (trigger
never fires, run dispatch deadlocks the loop, audit not written) this
scenario catches it. The single `call_broker: ListRules` step needs no
vault, so it isolates trigger→run→audit from the secret machinery.

## Setup

```bash
VM=${VMNAME:-$(virsh list --name --state-running | head -1)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

# Drain admin app + any prior scenario workflows, seed the cron workflow.
B64=$(base64 -w0 <<'EOF'
pkill -u admin -f qdistro_admin_app 2>/dev/null || true
rm -f /etc/qdistro/workflows/wfgui-*.yaml 2>/dev/null || true
mkdir -p /etc/qdistro/workflows
cat > /etc/qdistro/workflows/wfgui-tick.yaml <<'YAML'
- name: wfgui-tick
  description: cron tick that lists rules (no secret needed)
  trigger:
    type: cron
    interval_seconds: 5
  steps:
    - type: call_broker
      method: ListRules
YAML
# Restart so the engine loads + registers the cron trigger fresh.
systemctl restart qdistro-admin-broker.service
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# Let the broker come up and the 5s cron fire at least twice.
sleep 14
```

## Steps

### S1 — confirm a run was recorded in the workflow audit DB

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT workflow_name, state FROM workflow_runs WHERE workflow_name='wfgui-tick' ORDER BY started_at DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/workflow_audit.sqlite"
# Expect a line like:  wfgui-tick|completed
```

**Assert (audit row):**
- The query returns at least one row whose workflow_name is `wfgui-tick`
  and whose state is `completed` (not `failed`/`running`).

### S2 — the run's step was recorded

```bash
SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT s.step_type, s.success FROM workflow_steps s JOIN workflow_runs r ON s.run_id=r.run_id WHERE r.workflow_name='wfgui-tick' ORDER BY s.started_at DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/workflow_audit.sqlite"
# Expect:  call_broker|1
```

**Assert (step row):**
- The latest step for a `wfgui-tick` run is `call_broker` with success `1`.

### S3 — admin app Workflows tab shows the run

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
# Focus the window, then OCR-click the "Workflows" tab label.
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/01-one-trigger-one-run-audit-row-s3-tabs.png
# Runner: OCR the screenshot, click the center of the "Workflows" tab label.
# $VMGUI "$VM" click <cx> <cy>
sleep 0.5
$VMGUI "$VM" screenshot /tmp/01-one-trigger-one-run-audit-row-s3-workflows.png
```

**Assert (Workflows tab):**
- The top "Workflows" table has a row with name `wfgui-tick`, trigger
  `cron`, steps `1`.
- The bottom "Recent runs" table has at least one row for `wfgui-tick`
  with state `completed` and a non-empty `started` timestamp.

## Teardown

```bash
B64=$(base64 -w0 <<'EOF'
pkill -u admin -f qdistro_admin_app 2>/dev/null || true
rm -f /etc/qdistro/workflows/wfgui-*.yaml 2>/dev/null || true
systemctl restart qdistro-admin-broker.service
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'rm -f /tmp/01-one-trigger-one-run-audit-row-*.png 2>/dev/null || true'
```

## Notes for the runner

- The cron floor is 1s and the seed uses 5s; 14s of sleep guarantees ≥2
  fires. If S1 returns no row, the trigger never fired — that's a real
  FAIL, not a timing flake; capture `journalctl -u qdistro-admin-broker`
  tail in the justification.
- S3 depends on the broker still running the same engine instance that
  fired the run in S1. Do not restart the broker between S1 and S3.
