# 05 — human-in-the-loop: pending approval queue gates the run

<!-- qci:visual: required -->

**Lane: qdwin** (native Wayland, the shipped launcher). Read the "qdwin lane"
section of `tests/integration/permissions-gui/AGENTS.md` first: no xdotool,
no `DISPLAY=:0`; graded frames come from `qdwin_screenshot`; clicks use the
preview / confirm handshake (its pitfall 3b).

**What**: seed a workflow that does NOT opt into `auto_run`. Fire its
trigger and confirm the engine parks a **PENDING** run instead of executing
it, and keeps parking ONE run however often the trigger fires. A non-admin
uid must not be able to approve it, even with the genuine plan digest. In
the shipped admin app's Workflows tab, select the pending run, click
**Approve selected run**, review the captured plan, confirm **Approve plan**;
the same run then executes (`running`, then `completed`).

**Why**: acceptance test #5 from `qdistro-workflow-engine.md` ("AI agent
submits a workflow draft → admin's approval queue gains an entry → run is
gated on approve"). It is the GUI counterpart to the
`auto_run`/`ApproveWorkflowRun` enforcement (finding F3): a loaded workflow
must NOT auto-execute, and only the admin may release a parked run — here
through the installed admin app, which the broker admits for this authority
call only as the installed program holding the `org.qdistro.broker.approve`
grant (the CI test-only polkit rule; this scenario does not test the password
prompt). The headless twin (`workflow-headless.bats` wf05) approves as root;
only this scenario exercises the admin's review dialog and release path.

The fixture declares no conditions and no resources, so the plan shows its
actions (`call_broker` → `ListRules`, then a `wait_for_process` on a process
this scenario controls) and no concrete resource; approval re-evaluates
conditions server-side, but there are none here to fail.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui            # click-preview / click-confirm
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh   # qdwin_screenshot (host side)
qdwin_set_vm "$VM"
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}

# Session up, the work (uid 2000) silo fixture for the non-admin leg, idle
# locker held off and proven unlocked. A nonzero exit is a Setup ERROR.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_lane_setup --silos'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" 'id work'

B64=$(base64 -w0 <<'EOF'
[ -s /run/qci-wfgui/05-hold.pid ] && kill "$(cat /run/qci-wfgui/05-hold.pid)" 2>/dev/null
rm -f /etc/qdistro/workflows/wfgui-*.yaml 2>/dev/null || true
mkdir -p /etc/qdistro/workflows
install -d -m 0700 /run/qci-wfgui
# The approved run's second step blocks on this process, so its `running`
# state can be seen before this scenario lets it finish.
setsid sleep 1800 </dev/null >/dev/null 2>&1 &
echo $! > /run/qci-wfgui/05-hold.pid
# auto_run is OMITTED -> defaults false -> each fire parks a PENDING run.
cat > /etc/qdistro/workflows/wfgui-approval.yaml <<YAML
- name: wfgui-approval
  description: human-in-the-loop demo (no auto_run)
  trigger:
    type: cron
    interval_seconds: 5
  steps:
    - type: call_broker
      method: ListRules
    - type: wait_for_process
      pid: $(cat /run/qci-wfgui/05-hold.pid)
      timeout: 1200
YAML
date +%s > /run/qci-wfgui/05.t0
systemctl restart qdistro-admin-broker.service
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'
```

## Steps

### S1 — the fires parked exactly ONE pending run, and nothing executed

```bash
# Runs of THIS setup only (started_at >= setup time), read structurally from
# the engine's audit DB. Wait (bounded) for the first pending run, then for at
# least two more cron fires while it is unapproved.
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh || exit 2
DB=/var/lib/qdistro/audit/workflow_audit.sqlite
T0=$(cat /run/qci-wfgui/05.t0)
q() { sqlite3 "$DB" "SELECT run_id||'|'||state FROM workflow_runs WHERE workflow_name='wfgui-approval' AND started_at >= $T0 ORDER BY started_at;"; }
has_pending() { printf 'runs=[%s]' "$(q | paste -sd ' ')"; q | grep -q '|pending$'; }
_await "a pending wfgui-approval run since setup" 60 1 has_pending \
  || { echo "FAIL: no pending wfgui-approval run within 60s" >&2; exit 1; }
sleep 12  # the dedupe assertion NEEDS >= 2 more 5 s cron fires while unapproved; elapsed time is the condition
echo "--- runs since setup ---"
q
q | awk -F'|' '$2 == "pending" { print $1 }' > /run/qci-wfgui/05.run_id
echo "pending_runs=$(q | grep -c '|pending$') executed_runs=$(q | grep -vc '|pending$')"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash" | tee "$ART/05-s1-runs.txt"
RUN_ID=$($VMEXEC "$VM" 'cat /run/qci-wfgui/05.run_id' | grep -E '^[0-9a-zA-Z_-]+$' | tail -n1)
echo "RUN_ID=$RUN_ID"
```

**Assert (parked, deduped, not run):**
- `pending_runs=1 executed_runs=0`: the repeated cron fires produced exactly
  ONE pending `wfgui-approval` run (pending runs are deduped per workflow), and
  none ran (no `running`/`completed`/`failed` row). `RUN_ID` is that run's id.

### S2 — a non-admin uid cannot approve, even with the genuine digest

```bash
# The genuine digest, from the captured plan (root gdbus is a trusted
# control-plane helper; the parse is pure text).
DIGEST=$($VMEXEC "$VM" "gdbus call --system --dest org.qdistro.AdminBroker1 --object-path /org/qdistro/AdminBroker1 --method org.qdistro.AdminBroker1.PreviewWorkflowRun $RUN_ID" \
  | python3 -c 'import ast, json, sys; print(json.loads(ast.literal_eval(sys.stdin.read().strip().splitlines()[-1])[0]).get("definition_digest", ""))')
echo "DIGEST=$DIGEST"
B64=$(base64 -w0 <<EOF
runuser -u work -- gdbus call --system \
  --dest org.qdistro.AdminBroker1 \
  --object-path /org/qdistro/AdminBroker1 \
  --method org.qdistro.AdminBroker1.ApproveWorkflowRun "$RUN_ID" "$DIGEST" 2>&1 || true
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash" | tee "$ART/05-s2-nonadmin-approve.txt"
$VMEXEC "$VM" "sqlite3 /var/lib/qdistro/audit/workflow_audit.sqlite \"SELECT state FROM workflow_runs WHERE run_id='$RUN_ID';\""
```

**Assert (denied):**
- `DIGEST` is a 64-hex-digit SHA-256.
- The call as `work` (uid 2000) fails with a D-Bus `AccessDenied` / policy
  rejection — with the genuine digest, so the refusal is about the caller.
- The run is still `pending` afterwards.

### S3 — open the Workflows tab; the run is pending

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_app'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
qdwin_screenshot "$ART/05-s3-tabs.png"
# Runner: locate the "Workflows" tab label and click it with the handshake:
#   $VMGUI "$VM" click-preview <cx> <cy> "Workflows tab"
#   $VMGUI "$VM" click-confirm <preview-manifest>
sleep 1
qdwin_screenshot "$ART/05-s3-workflows.png"
# Runner: click the wfgui-approval row whose state is `pending` in the
# "Recent runs" table (preview/confirm), then capture.
qdwin_screenshot "$ART/05-s3-selected.png"
```

**Assert:**
- The Recent runs table has the `wfgui-approval` row with state `pending`
  (its run_id begins with the `RUN_ID` printed in S1), and it is the
  selected row in `05-s3-selected.png`.

### S4 — "Approve selected run" shows the captured plan; still pending

```bash
# Runner: click the "Approve selected run" button (preview/confirm).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_qdwin_window_title "Review workflow plan" 30'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "Review workflow plan"'
qdwin_screenshot "$ART/05-s4-review.png"
$VMEXEC "$VM" "gdbus call --system --dest org.qdistro.AdminBroker1 --object-path /org/qdistro/AdminBroker1 --method org.qdistro.AdminBroker1.PreviewWorkflowRun $RUN_ID" > "$ART/05-s4-preview.txt"
$VMEXEC "$VM" "sqlite3 /var/lib/qdistro/audit/workflow_audit.sqlite \"SELECT state FROM workflow_runs WHERE run_id='$RUN_ID';\""
```

**Assert:**
- A "Review workflow plan" dialog is up and shows `Run <RUN_ID>` and
  `Definition SHA-256:` with the full `<DIGEST>` on the line below it (all
  64 hex characters visible) — the same run id as S1 and the same digest
  as S2 (and as `05-s4-preview.txt`).
- Its definition text shows the seeded workflow: `wfgui-approval`, the
  `call_broker` step with `ListRules`, and the `wait_for_process` step; no
  resources are listed (the fixture has none).
- The run is still `pending` while the dialog is open: review alone
  releases nothing.

### S5 — confirm "Approve plan": the same run runs, then completes

```bash
# Runner: click the dialog's "Approve plan" button (preview/confirm).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
B64=$(base64 -w0 <<EOF
source /tmp/qci-gui-waiters.sh || exit 2
state_is() { s=\$(sqlite3 /var/lib/qdistro/audit/workflow_audit.sqlite "SELECT state FROM workflow_runs WHERE run_id='$RUN_ID';"); printf 'state=%s' "\$s"; [ "\$s" = "\$1" ]; }
_await "run $RUN_ID running" 30 0.5 state_is running && echo "run $RUN_ID running"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# Runner: click Refresh (preview/confirm) unless the tab already shows it.
qdwin_screenshot "$ART/05-s5-running.png"
# Let the held step finish, then wait for completion of THE SAME run.
$VMEXEC "$VM" 'kill "$(cat /run/qci-wfgui/05-hold.pid)"; rm -f /run/qci-wfgui/05-hold.pid'
B64=$(base64 -w0 <<EOF
source /tmp/qci-gui-waiters.sh || exit 2
DB=/var/lib/qdistro/audit/workflow_audit.sqlite
T0=\$(cat /run/qci-wfgui/05.t0)
state_is() { s=\$(sqlite3 "\$DB" "SELECT state FROM workflow_runs WHERE run_id='$RUN_ID';"); printf 'state=%s' "\$s"; [ "\$s" = "\$1" ]; }
QCI_AWAIT_QUIET=1 _await "run $RUN_ID completed" 30 0.5 state_is completed
echo "state=\$(sqlite3 "\$DB" "SELECT state FROM workflow_runs WHERE run_id='$RUN_ID';")"
echo "digest=\$(sqlite3 "\$DB" "SELECT definition_digest FROM workflow_runs WHERE run_id='$RUN_ID';")"
echo "events=\$(sqlite3 "\$DB" "SELECT event_type FROM workflow_audit WHERE run_id='$RUN_ID' ORDER BY id;" | paste -sd, -)"
echo "steps=\$(sqlite3 "\$DB" "SELECT step_type||'|'||success FROM workflow_steps WHERE run_id='$RUN_ID' ORDER BY id;" | paste -sd, -)"
echo "runs_since_setup=\$(sqlite3 "\$DB" "SELECT count(*) FROM workflow_runs WHERE workflow_name='wfgui-approval' AND started_at >= \$T0 AND state != 'pending';")"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash" | tee "$ART/05-s5-completed.txt"
# Runner: click Refresh (preview/confirm), then capture.
qdwin_screenshot "$ART/05-s5-completed.png"
```

**Assert (the parked run is the one that executed, after the plan was approved):**
- The guest prints `run <RUN_ID> running`, and `05-s5-running.png` shows that
  run's row as `running` — no new run id.
- `05-s5-completed.txt`: `state=completed`; `digest=<DIGEST>` (the approved
  digest); `events=` contains `run_pending`, `run_approval` and `run_start`
  in that order, then `run_complete`; `steps=call_broker|1,wait_for_process|1`;
  `runs_since_setup=1` (exactly one run executed — the approved one).
- `05-s5-completed.png` shows the same `wfgui-approval` row as `completed`.

## Teardown

```bash
B64=$(base64 -w0 <<'EOF'
[ -s /run/qci-wfgui/05-hold.pid ] && kill "$(cat /run/qci-wfgui/05-hold.pid)" 2>/dev/null
rm -f /run/qci-wfgui/05-hold.pid /run/qci-wfgui/05.t0 /run/qci-wfgui/05.run_id
rm -f /etc/qdistro/workflows/wfgui-*.yaml 2>/dev/null || true
systemctl restart qdistro-admin-broker.service
EOF
)
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Notes

- Pending runs are **deduped** per `(workflow, cgroup)` (here: per
  workflow, no cgroup), so the 5 s cron firing repeatedly while unapproved
  produces exactly ONE pending row, not one per tick — the queue does not
  flood. S1 asserts it.
- To flip a workflow to unattended execution, add `auto_run: true` to its
  YAML; it then runs on each fire with no approval step (still subject to
  `conditions`/`needs`).
- The earlier version of this file approved "as the `dev` uid" with an
  all-zero digest: there is no `dev` user on the GUI VMs, and a zero digest
  is refused for its own reason. The non-admin leg is now `work` with the
  genuine digest.
