# Workflow GUI test runner — for graphic-aware subagents

These scenarios validate the **workflow orchestration engine** end-to-end
on a live VM: triggers firing real runs, the secret-delivery lifecycle
(channel exists during the run, scrubbed after), failure→scrub→failed-run,
and the admin app's read-only **Workflows** tab.

## Shared harness — read this first

The orchestrator/runner split, the `scripts/vm/{vm-exec,vm-gui}` tooling,
the base64-wrapping rule for quoted scripts, OCR-first click targeting,
and `virsh send-key` for modifier chords are **identical** to the
permissions-gui suite. Read
`tests/integration/permissions-gui/AGENTS.md` top to bottom before running
any scenario here — everything in its "Hard-learned pitfalls", "Running a
scenario", and "Report format" sections applies verbatim.

This file only documents what is *different* for workflow scenarios.

## Provisioning

Use `scripts/vm/spin-test-vm-gui.sh` (same as permissions-gui): it brings
up the broker, admin-app launchers, and an admin compositor session. The workflow
engine loads inside the broker automatically (`_setup_workflow_engine`),
so no extra service is needed — but it only picks up workflows present at
broker start or on a reload.

## Workflow-specific conventions

1. **Seed workflows under `/etc/qdistro/workflows/*.yaml`** (system dir;
   the loader also reads `~/.config/qdistro/workflows/`). After writing a
   YAML, make the broker pick it up with either:
   - `systemctl restart qdistro-admin-broker.service` (drains runs too), or
   - `systemctl reload`/`kill -HUP` / a `ReloadRules` D-Bus call — the
     broker reloads workflows on the same trigger as rules.
   Prefix scenario-authored files with `wfgui-` so the drain/teardown can
   `rm -f /etc/qdistro/workflows/wfgui-*.yaml` without touching
   hand-authored workflows.

2. **Triggering a process_spawn workflow** without a real launcher: create
   the watched cgroup and move a process into it.
   ```bash
   CG=/sys/fs/cgroup/system.slice/qdistro-git-sign-test.scope
   mkdir -p "$CG"
   setsid sleep 60 </dev/null >/dev/null 2>&1 &
   echo $! > /tmp/wf-target.pid
   echo $(cat /tmp/wf-target.pid) > "$CG/cgroup.procs"
   ```
   The `cgroup_pattern` in the seeded workflow must glob this path
   (e.g. `system.slice/qdistro-git-sign-*.scope`). The poller baselines
   pre-existing pids at start, so move the process in *after* the broker
   is watching (i.e. after restart + a poll interval).

3. **Vault items for deliver_secret** are provisioned via
   `qdistro-pwd-admin` (run as the vault-owning user):
   ```bash
   printf '%s' "$KEY" | qdistro-pwd-admin add dev github-ssh-key
   qdistro-pwd-admin unlock dev      # vault must be unlocked at run time
   ```
   A `needs: vault/dev/github-ssh-key` maps to vault `dev`, tag
   `github-ssh-key` (see `workflow/pwd_secret_source.py:parse_item`).

4. **Audit ground truth** lives in
   `/var/lib/qdistro/audit/workflow_audit.sqlite` (tables `workflow_runs`,
   `workflow_steps`, `workflow_audit`) — separate from the permission
   audit DB. Query it via the base64-SQL pattern from the shared AGENTS.md.
   The secret VALUE must never appear there — several scenarios assert this.

5. **Admin app → Workflows tab.** The Qt admin app
   (`/usr/local/bin/qdistro-start-admin-app`) has a `Workflows` tab
   (5th tab, after Pending/Cache/Rules/History). Its top table columns are
   `name | trigger | steps | needs | description`; the bottom "Recent runs"
   table is `run_id | workflow | state | started | finished | error`. A
   `Refresh` button re-queries `ListWorkflows`/`ListWorkflowRuns`. Activate
   the tab by OCR-clicking the `Workflows` tab label, then screenshot.

## Teardown hard rule

Every scenario that seeds `/etc/qdistro/workflows/wfgui-*.yaml`, a test
cgroup, or a vault item must remove all three in Teardown (and defensively
in Setup), then restart the broker so the next scenario starts clean.
