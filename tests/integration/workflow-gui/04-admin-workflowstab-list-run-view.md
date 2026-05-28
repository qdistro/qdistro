# 04 — admin app Workflows tab: list + run view + Refresh

**What**: with several workflows loaded and at least one run on record,
open the Qt admin app, switch to the Workflows tab, and verify the tab
renders the workflow definitions table and the recent-runs table with the
expected columns and data. Then add a new workflow on disk, reload the
broker, click Refresh, and verify the new workflow appears without
restarting the app.

**Why**: the Workflows tab is the admin's only window into what the engine
is doing. This scenario is the read-path counterpart to scenarios 01–03:
it checks the *presentation* — both tables populate, columns line up, and
the live `Refresh` round-trips `ListWorkflows`/`ListWorkflowRuns` so an
admin editing workflow YAML sees the change without relaunching.

## Setup

```bash
VM=${VMNAME:-$(virsh list --name --state-running | head -1)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

B64=$(base64 -w0 <<'EOF'
pkill -u admin -f qdistro_admin_app 2>/dev/null || true
rm -f /etc/qdistro/workflows/wfgui-*.yaml 2>/dev/null || true
mkdir -p /etc/qdistro/workflows

# Two workflows up front; a cron one that will also produce a run.
cat > /etc/qdistro/workflows/wfgui-list-a.yaml <<'YAML'
- name: wfgui-list-a
  description: cron lister A
  trigger:
    type: cron
    interval_seconds: 5
  steps:
    - type: call_broker
      method: ListRules
YAML
cat > /etc/qdistro/workflows/wfgui-list-b.yaml <<'YAML'
- name: wfgui-list-b
  description: dbus-signal placeholder B
  trigger:
    type: qbus_event
    event: RulesReloaded
  needs:
    - vault/dev/some-token
  steps:
    - type: run_hook
      hook: noop_hook
    - type: wait_for_process
      pid: 1
YAML

systemctl restart qdistro-admin-broker.service
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 12   # let wfgui-list-a's 5s cron fire at least once
```

## Steps

### S1 — open the admin app and switch to the Workflows tab

```bash
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/04-admin-workflowstab-list-run-view-s1-tabs.png
# Runner: OCR-click the "Workflows" tab label (5th tab), then screenshot.
# $VMGUI "$VM" click <cx> <cy>
sleep 0.5
$VMGUI "$VM" screenshot /tmp/04-admin-workflowstab-list-run-view-s1-workflows.png
```

**Assert (both tables populated):**
- Under the "Workflows" heading, the table shows two rows:
  `wfgui-list-a` (trigger `cron`, steps `1`) and `wfgui-list-b`
  (trigger `qbus_event`, steps `2`, needs `vault/dev/some-token`).
- The column headers read `name`, `trigger`, `steps`, `needs`,
  `description`.
- Under the "Recent runs" heading, the table has at least one
  `wfgui-list-a` row with state `completed`; its columns are `run_id`,
  `workflow`, `state`, `started`, `finished`, `error`.

### S2 — add a third workflow, reload, click Refresh

```bash
B64=$(base64 -w0 <<'EOF'
cat > /etc/qdistro/workflows/wfgui-list-c.yaml <<'YAML'
- name: wfgui-list-c
  description: added live
  trigger:
    type: cron
    interval_seconds: 9999
  steps:
    - type: call_broker
      method: ListCache
YAML
# Reload workflows without restarting the broker (admin app stays up).
systemctl reload qdistro-admin-broker.service 2>/dev/null \
  || kill -HUP "$(systemctl show -p MainPID --value qdistro-admin-broker.service)"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 1

# Click the Refresh button (OCR target "Refresh"), then screenshot.
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/04-admin-workflowstab-list-run-view-s2-prerefresh.png
# Runner: OCR-click the "Refresh" button, then screenshot.
# $VMGUI "$VM" click <cx> <cy>
sleep 0.5
$VMGUI "$VM" screenshot /tmp/04-admin-workflowstab-list-run-view-s2-afterrefresh.png
```

**Assert (live refresh):**
- After clicking Refresh, the Workflows table now shows a third row,
  `wfgui-list-c` (trigger `cron`, steps `1`, description `added live`),
  which was NOT present in the S1 screenshot.
- The app was not relaunched between S1 and S2 (same window) — proving
  Refresh re-queried the broker live.

## Teardown

```bash
B64=$(base64 -w0 <<'EOF'
pkill -u admin -f qdistro_admin_app 2>/dev/null || true
rm -f /etc/qdistro/workflows/wfgui-*.yaml 2>/dev/null || true
systemctl restart qdistro-admin-broker.service
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'rm -f /tmp/04-admin-workflowstab-list-run-view-*.png 2>/dev/null || true'
```

## Notes for the runner

- `wfgui-list-b`'s `qbus_event` trigger won't necessarily produce a run
  during the scenario — it's there to exercise the *definition* row
  (trigger type, multi-step count, needs column), not a run. Don't FAIL
  S1 for the absence of a `wfgui-list-b` run.
- If the auto-refresh on `RulesReloaded` already added `wfgui-list-c`
  before you click Refresh, that's fine — the assertion is that the row
  is present after S2, by either path. Note in the report which fired.
- If `systemctl reload` isn't defined for the unit, the SIGHUP fallback in
  S2 reloads workflows the same way (the broker reloads workflows on
  SIGHUP alongside rules).
