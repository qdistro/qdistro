# 04 — admin app Workflows tab: definitions, completed/running/failed runs, Refresh

<!-- qci:visual: required -->

**Lane: qdwin** (native Wayland, the shipped launcher). Read the "qdwin lane"
section of `tests/integration/permissions-gui/AGENTS.md` first: no xdotool,
no `DISPLAY=:0`; graded frames come from `qdwin_screenshot`; clicks use the
preview / confirm handshake (its pitfall 3b).

**What**: with four workflows loaded — a cron one that completes, a
`qbus_event` placeholder, a `process_spawn` one held RUNNING by a live process,
and a cron one that FAILS — open the Qt admin app, switch to the Workflows
tab, and verify the definitions table and the recent-runs table render the
expected columns and data: a completed run with its start time, a running
run, a failed run with its error. Then add a fifth workflow on disk, reload
the broker, click Refresh, and verify it appears without restarting the app.

**Why**: the Workflows tab is the admin's only window into what the engine is
doing. This scenario checks the *presentation*: both tables populate,
columns line up, each run state the engine records is rendered, and the live
`Refresh` round-trips `ListWorkflows`/`ListWorkflowRuns` so an admin editing
workflow YAML sees the change without relaunching.

The engine behaviour itself is headless-tested
(`tests/integration/vm/workflow-headless.bats`, `s130-workflow-headless.sh`):
wf01 trigger → run → `call_broker|1` step row; wf02 secret delivery with a
live per-run ssh-agent holding the key, scrubbed on process exit, no key in
the audit DB or broker journal; wf03 delivery then a whitelist-refused step,
`deliver_secret|1,call_broker|0`, secrets scrubbed, the refusal in the run's
error. The rendered rows those GUI scenarios (workflow-gui 01–03, now merged
here) checked are asserted below on secret-free fixtures; the secret-bearing
runs and their scrubbing stay asserted headlessly.

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
CG=/sys/fs/cgroup/system.slice/qdistro-wfgui-list-ps-test.scope
[ -s /run/qci-wfgui/list-ps.pid ] && kill "$(cat /run/qci-wfgui/list-ps.pid)" 2>/dev/null
rm -f /run/qci-wfgui/list-ps.pid
rmdir "$CG" 2>/dev/null || true
rm -f /etc/qdistro/workflows/wfgui-*.yaml 2>/dev/null || true
mkdir -p /etc/qdistro/workflows

# A cron workflow that runs and completes.
cat > /etc/qdistro/workflows/wfgui-list-a.yaml <<'YAML'
- name: wfgui-list-a
  description: cron lister A
  # Without auto_run a fired trigger only parks a PENDING run for admin
  # approval (finding F3, see 05), which expires; S1 needs a completed run.
  auto_run: true
  trigger:
    type: cron
    interval_seconds: 5
  steps:
    - type: call_broker
      method: ListRules
YAML
# A definition row only: multi-step, a needs column, a qbus_event trigger.
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
# A process_spawn workflow that stays RUNNING while its triggering process
# lives (the former workflow-gui/02's on-screen running state, without a
# secret: `needs` only gates deliver_secret, so the declared need is shown
# but nothing is delivered).
cat > /etc/qdistro/workflows/wfgui-list-ps.yaml <<'YAML'
- name: wfgui-list-ps
  description: process-held run (running state)
  auto_run: true
  trigger:
    type: process_spawn
    cgroup_pattern: "system.slice/qdistro-wfgui-list-ps-*.scope"
    poll_interval: 0.25
  needs:
    - vault/dev/some-token
  steps:
    - type: call_broker
      method: ListRules
    # Long enough to outlast Setup + S1; the sleeper is killed after S1.
    - type: wait_for_process
      pid: $trigger.pid
      timeout: 1200
YAML
# A cron workflow whose step the engine's method whitelist refuses (the
# former workflow-gui/03's failed run).
cat > /etc/qdistro/workflows/wfgui-list-fail.yaml <<'YAML'
- name: wfgui-list-fail
  description: whitelist-refused step (failed state)
  auto_run: true
  trigger:
    type: cron
    interval_seconds: 5
  steps:
    - type: call_broker
      method: DecideRequest
YAML

install -d -m 0700 /run/qci-wfgui
date +%s > /run/qci-wfgui/04.t0
systemctl restart qdistro-admin-broker.service
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'

# Bounded waits, read from the engine's own audit DB and scoped to runs that
# STARTED after this setup (a reused VM's old rows must not satisfy them).
# A timeout is a Setup ERROR (S1 would have nothing to show).
RUN_B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh || exit 2
DB=/var/lib/qdistro/audit/workflow_audit.sqlite
T0=$(cat /run/qci-wfgui/04.t0)
runs_since() {  # <workflow> <state> [error-substring]
  local n
  n=$(sqlite3 "$DB" "SELECT count(*) FROM workflow_runs WHERE workflow_name='$1' AND state='$2' AND started_at >= $T0${3:+ AND error LIKE '%$3%'};" 2>/dev/null)
  printf '%s %s runs since setup: %s' "$1" "$2" "${n:-0}"
  [ "${n:-0}" -ge 1 ] 2>/dev/null
}
_await "a completed wfgui-list-a run since setup" 60 1 runs_since wfgui-list-a completed || exit 1
_await "a failed wfgui-list-fail run (whitelist refusal) since setup" 60 1 \
  runs_since wfgui-list-fail failed 'not in whitelist' || exit 1
EOF
)
$VMEXEC "$VM" "echo $RUN_B64 | base64 -d | bash"

# Fire wfgui-list-ps: move a long-lived sleeper into the watched cgroup AFTER
# the poller baselined it (the broker restart above + a few poll intervals),
# then wait for the run to be RUNNING. The sleeper is killed explicitly after
# S1's capture, so its lifetime is not a race against the steps.
SPAWN_B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh || exit 2
sleep 2  # the process_spawn poller baselines existing pids; there is no "baselined" signal to wait for
CG=/sys/fs/cgroup/system.slice/qdistro-wfgui-list-ps-test.scope
mkdir -p "$CG"
setsid sleep 900 </dev/null >/dev/null 2>&1 &
echo $! > /run/qci-wfgui/list-ps.pid
echo "$(cat /run/qci-wfgui/list-ps.pid)" > "$CG/cgroup.procs"
DB=/var/lib/qdistro/audit/workflow_audit.sqlite
T0=$(cat /run/qci-wfgui/04.t0)
running_since() {
  local n
  n=$(sqlite3 "$DB" "SELECT count(*) FROM workflow_runs WHERE workflow_name='wfgui-list-ps' AND state='running' AND started_at >= $T0;" 2>/dev/null)
  printf 'wfgui-list-ps running runs: %s' "${n:-0}"
  [ "${n:-0}" -ge 1 ] 2>/dev/null
}
_await "wfgui-list-ps running since setup" 60 1 running_since
EOF
)
$VMEXEC "$VM" "echo $SPAWN_B64 | base64 -d | bash"
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
# The running run must still be running in the frame: only now end its process.
$VMEXEC "$VM" 'kill "$(cat /run/qci-wfgui/list-ps.pid)" 2>/dev/null; rm -f /run/qci-wfgui/list-ps.pid; true'
```

If a needed row is outside the visible part of a table (the recent-runs table
can hold several cron rows), scroll that table or widen the column with the
preview/confirm pointer and capture again to a NEW name; grade from the frame
that shows the row.

**Assert (definitions):**
- Under the "Workflows" heading, the column headers read `name`, `trigger`,
  `steps`, `needs`, `description`.
- The table shows four rows:
  `wfgui-list-a` (trigger `cron`, steps `1`);
  `wfgui-list-b` (trigger `qbus_event`, steps `2`, needs `vault/dev/some-token`);
  `wfgui-list-ps` (trigger `process_spawn`, steps `2`, needs
  `vault/dev/some-token`);
  `wfgui-list-fail` (trigger `cron`, steps `1`).

**Assert (recent runs):**
- The "Recent runs" column headers read `run_id`, `workflow`, `state`,
  `started`, `finished`, `error` (the table goes on with `cleanup`,
  `cleanup pending`, `cleanup error`).
- At least one `wfgui-list-a` row has state `completed` and a non-empty,
  readable `started` timestamp.
- A `wfgui-list-ps` row has state `running`, and its `finished` cell is `-`
  (the run is blocked in `wait_for_process` on the live process).
- A `wfgui-list-fail` row has state `failed`, and its `error` cell is not
  `-` but a `call_broker ... not in whitelist` message.

### S2 — add a fifth workflow, reload, click Refresh

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
- After clicking Refresh, the Workflows table shows a fifth row,
  `wfgui-list-c` (trigger `cron`, steps `1`, description `added live`),
  which was NOT present in the S1 screenshot.
- The app was not relaunched between S1 and S2 (same window) — proving
  Refresh re-queried the broker live.

## Teardown

```bash
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && qdwin_stop_admin_app'
B64=$(base64 -w0 <<'EOF'
[ -s /run/qci-wfgui/list-ps.pid ] && kill "$(cat /run/qci-wfgui/list-ps.pid)" 2>/dev/null
rm -f /run/qci-wfgui/list-ps.pid /run/qci-wfgui/04.t0
rmdir /sys/fs/cgroup/system.slice/qdistro-wfgui-list-ps-test.scope 2>/dev/null || true
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
- The cron workflows fire every 5 s, so the recent-runs table grows while
  you work; any qualifying row satisfies its assertion.
- If the auto-refresh on `RulesReloaded` already added `wfgui-list-c`
  before you click Refresh, that's fine — the assertion is that the row
  is present after S2, by either path. Note in the report which fired.
- If `systemctl reload` isn't defined for the unit, the SIGHUP fallback in
  S2 reloads workflows the same way (the broker reloads workflows on
  SIGHUP alongside rules).
- Do not restart the broker between Setup and S1: it would end the running
  run for the wrong reason.
