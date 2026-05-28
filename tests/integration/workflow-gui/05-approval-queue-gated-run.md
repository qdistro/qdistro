# 05 — human-in-the-loop: pending approval queue gates the run

**What**: seed a workflow that does NOT opt into `auto_run`. Fire its
trigger and confirm the engine parks a **PENDING** run instead of
executing it. In the admin app's Workflows tab, select the pending run and
click **Approve selected run**; confirm the run then executes to
`completed`. A non-admin uid must not be able to approve.

**Why**: this is acceptance test #5 from `qdistro-workflow-engine.md`
("AI agent submits a workflow draft → admin's approval queue gains an
entry → run is gated on approve"). It is the GUI counterpart to the
`auto_run`/`ApproveWorkflowRun` enforcement (finding F3): a loaded workflow
must NOT auto-execute, and only an admin may release a parked run. Approval
re-checks the workflow's conditions server-side, so this also confirms the
gate isn't a UI-only formality.

## Setup

```bash
VM=${VMNAME:-$(virsh list --name --state-running | head -1)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

B64=$(base64 -w0 <<'EOF'
pkill -u admin -f qdistro_admin_app 2>/dev/null || true
rm -f /etc/qdistro/workflows/wfgui-*.yaml 2>/dev/null || true
mkdir -p /etc/qdistro/workflows

# auto_run is OMITTED -> defaults false -> each fire parks a PENDING run.
# A short cron interval gives us a deterministic fire to approve. The step
# is side-effect-light (ListRules) so approval completes quickly.
cat > /etc/qdistro/workflows/wfgui-approval.yaml <<'YAML'
- name: wfgui-approval
  description: human-in-the-loop demo (no auto_run)
  trigger:
    type: cron
    interval_seconds: 5
  steps:
    - type: call_broker
      method: ListRules
YAML

systemctl restart qdistro-admin-broker.service
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 12   # let the 5s cron fire at least once -> a PENDING run is parked
```

## Steps

### S1 — confirm the fire parked a PENDING run (did NOT execute)

```bash
# Read the run state straight from the broker surface (admin uid).
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- gdbus call --system \
  --dest org.qdistro.AdminBroker1 \
  --object-path /org/qdistro/AdminBroker1 \
  --method org.qdistro.AdminBroker1.ListWorkflowRuns 50
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash" | tee /tmp/05-runs-before.txt
```

**Assert (parked, not run):**
- The output contains a `wfgui-approval` run with state `pending`.
- There is NO `completed` `wfgui-approval` run yet (the workflow did not
  auto-execute on the cron fire).

### S2 — a non-admin uid cannot approve

```bash
RUN_ID=$($VMEXEC "$VM" "runuser -u admin -- gdbus call --system \
  --dest org.qdistro.AdminBroker1 \
  --object-path /org/qdistro/AdminBroker1 \
  --method org.qdistro.AdminBroker1.ListWorkflowRuns 50" \
  | grep -o \"[0-9a-f]\\{8,\\}\" | head -1)
echo "RUN_ID=$RUN_ID"

# As the 'dev' (non-admin) uid: must be denied at the bus level.
B64=$(base64 -w0 <<EOF
runuser -u dev -- gdbus call --system \
  --dest org.qdistro.AdminBroker1 \
  --object-path /org/qdistro/AdminBroker1 \
  --method org.qdistro.AdminBroker1.ApproveWorkflowRun "$RUN_ID" \
  2>&1 || true
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash" | tee /tmp/05-nonadmin-approve.txt
```

**Assert (denied):**
- The non-admin call fails (D-Bus `AccessDenied` / policy rejection); the
  run is still `pending` afterwards.

### S3 — admin approves via the Workflows tab

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/05-approval-queue-s3-tabs.png
# Runner: OCR-click the "Workflows" tab, screenshot, then OCR-click the
# pending run row in the "Recent runs" table to select it.
# $VMGUI "$VM" click <cx> <cy>   # Workflows tab
# $VMGUI "$VM" click <rx> <ry>   # the wfgui-approval / pending row
$VMGUI "$VM" screenshot /tmp/05-approval-queue-s3-selected.png
# Runner: OCR-click the "Approve selected run" button.
# $VMGUI "$VM" click <bx> <by>
sleep 1
$VMGUI "$VM" screenshot /tmp/05-approval-queue-s3-afterapprove.png
```

**Assert (gated run executes only after approve):**
- Before clicking Approve, the `wfgui-approval` run row shows state
  `pending`.
- After clicking "Approve selected run" (and the tab auto-refreshing), the
  same run transitions to `running` then `completed` — no new run id; the
  parked run is the one that executed.

### S4 — confirm completion on the broker surface

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- gdbus call --system \
  --dest org.qdistro.AdminBroker1 \
  --object-path /org/qdistro/AdminBroker1 \
  --method org.qdistro.AdminBroker1.ListWorkflowRuns 50
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash" | tee /tmp/05-runs-after.txt
```

**Assert:**
- The previously-pending `wfgui-approval` run is now `completed`.
- The run id matches the one observed pending in S1/S2 (approval released
  the parked run, it was not a fresh auto-run).

## Teardown

```bash
B64=$(base64 -w0 <<'EOF'
pkill -u admin -f qdistro_admin_app 2>/dev/null || true
rm -f /etc/qdistro/workflows/wfgui-*.yaml 2>/dev/null || true
systemctl restart qdistro-admin-broker.service
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Notes

- Pending runs are **deduped** per `(workflow, cgroup)` (here: per
  workflow, no cgroup), so the 5s cron firing repeatedly while unapproved
  produces exactly ONE pending row, not one per tick — the queue does not
  flood. A second `ListWorkflowRuns` a few seconds after S1 should still
  show a single pending `wfgui-approval`.
- To flip a workflow to unattended execution, add `auto_run: true` to its
  YAML; it then runs on each fire with no approval step (still subject to
  `conditions`/`needs`).
