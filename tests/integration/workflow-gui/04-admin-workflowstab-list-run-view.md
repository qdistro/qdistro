# 04 — admin app Workflows tab: list + run view + Refresh

<!-- qci:visual: required -->

**Lane: qdwin** (native Wayland, the shipped launcher). Read the "qdwin lane"
section of `tests/integration/permissions-gui/AGENTS.md` first: no xdotool,
no `DISPLAY=:0`; graded frames come from `qdwin_screenshot`; clicks use the
preview / confirm handshake (its pitfall 3b).

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
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui            # click-preview / click-confirm
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh   # qdwin_screenshot (host side)
qdwin_set_vm "$VM"
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}

# Session up, idle locker held off and proven unlocked. A nonzero exit is a
# Setup ERROR.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_admin_lane_setup'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'

B64=$(base64 -w0 <<'EOF'
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
# Wait (bounded) for wfgui-list-a's 5 s cron to have completed a run, read
# from the engine's own audit DB, instead of a fixed sleep. A timeout is a
# Setup ERROR (S1's runs table would have nothing to show).
RUN_B64=$(base64 -w0 <<'EOF'
for _ in $(seq 1 60); do
  n=$(sqlite3 /var/lib/qdistro/audit/workflow_audit.sqlite     "SELECT count(*) FROM workflow_runs WHERE workflow_name='wfgui-list-a' AND state='completed';" 2>/dev/null)
  [ "${n:-0}" -ge 1 ] 2>/dev/null && { echo "completed runs: $n"; exit 0; }
  sleep 1
done
echo "no completed wfgui-list-a run within 60s" >&2; exit 1
EOF
)
$VMEXEC "$VM" "echo $RUN_B64 | base64 -d | bash"
```

## Steps

### S1 — open the admin app and switch to the Workflows tab

```bash
# The shipped launcher, first-paint mode (a nonzero exit FAILS S1).
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_start_admin_app'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
qdwin_screenshot "$ART/04-admin-workflowstab-list-run-view-s1-tabs.png"
# Runner: locate the "Workflows" tab label (5th tab) in that frame and click it
# with the handshake:
#   $VMGUI "$VM" click-preview <cx> <cy> "Workflows tab"
#   $VMGUI "$VM" click-confirm <preview-manifest>
# The tab queries the broker when shown; settle, then capture.
sleep 1
qdwin_screenshot "$ART/04-admin-workflowstab-list-run-view-s1-workflows.png"
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

$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_focus_window "admin approvals.*"'
qdwin_screenshot "$ART/04-admin-workflowstab-list-run-view-s2-prerefresh.png"
# Runner: locate the "Refresh" button in that frame and click it with the
# preview / confirm handshake, then settle and capture.
sleep 1
qdwin_screenshot "$ART/04-admin-workflowstab-list-run-view-s2-afterrefresh.png"
```

**Assert (live refresh):**
- After clicking Refresh, the Workflows table now shows a third row,
  `wfgui-list-c` (trigger `cron`, steps `1`, description `added live`),
  which was NOT present in the S1 screenshot.
- The app was not relaunched between S1 and S2 (same window) — proving
  Refresh re-queried the broker live.

## Teardown

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
B64=$(base64 -w0 <<'EOF'
rm -f /etc/qdistro/workflows/wfgui-*.yaml 2>/dev/null || true
systemctl restart qdistro-admin-broker.service
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
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
