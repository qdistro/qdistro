# Workflow GUI test runner — for graphic-aware subagents

These scenarios validate what the admin sees of the **workflow
orchestration engine** on a live VM: the admin app's **Workflows** tab
rendering definitions and completed / running / failed runs (04), and the
approval queue that gates a non-`auto_run` workflow (05). The engine itself
— triggers firing runs, the secret-delivery lifecycle (channel exists during
the run, scrubbed after), failure → scrub → failed run — is tested headless
in `tests/integration/vm/workflow-headless.bats` (`s130-workflow-headless.sh`,
wf01/02/03/05); the former GUI scenarios 01–03 were merged into 04 on
2026-10-14, keeping their rendered-row assertions.

## Shared harness — read this first

The orchestrator/runner split, the `scripts/vm/{vm-exec,vm-gui}` tooling,
the base64-wrapping rule for quoted scripts, OCR-first click preview/confirmation,
and `virsh send-key` for modifier chords are **identical** to the
permissions-gui suite. Read
`tests/integration/permissions-gui/AGENTS.md` top to bottom before running
any scenario here — everything in its "Hard-learned pitfalls", "Running a
scenario", and "Report format" sections applies verbatim.

This file only documents what is *different* for workflow scenarios.

## Provisioning

Use `scripts/vm/spin-test-vm-gui.sh` (same as permissions-gui): it brings
up the broker, the shipped admin app and its launcher, and the qdwin session
(both scenarios run on the qdwin lane). The workflow
engine loads inside the broker automatically (`_setup_workflow_engine`),
so no extra service is needed — but it only picks up workflows present at
broker start or on a reload.

## Workflow-specific conventions

1. **Seed workflows under `/etc/qdistro/workflows/*.yaml`** (system dir;
   the loader also reads `~/.config/qdistro/workflows/`). After writing a
   YAML, make the broker pick it up with either:
   - `systemctl restart qdistro-admin-broker.service` (drains runs too), or
   - `systemctl reload`/`kill -HUP` / a `ReloadRules` D-Bus call (as
     root: the broker refuses it from an admin-uid busctl/dbus-send/
     inline Python) — the broker reloads workflows on the same trigger
     as rules.
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

5. **Admin app → Workflows tab.** The Qt admin app (started with
   `qdwin_start_admin_app`) has a `Workflows` tab (5th tab, after
   Pending/Cache/Rules/History). Its top table columns are
   `name | trigger | steps | needs | description`; the bottom "Recent runs"
   table is `run_id | workflow | state | started | finished | error |
   cleanup | cleanup pending | cleanup error`. A `Refresh` button re-queries
   `ListWorkflows`/`ListWorkflowRuns`; `Approve selected run` opens the
   "Review workflow plan" dialog for a pending run. Activate the tab by
   locating the `Workflows` label, previewing the marked target, and
   confirming that click per the shared permissions-gui contract; then
   `qdwin_screenshot`.

6. **Scope run waits to this scenario.** Runs from earlier invocations stay
   in `workflow_runs`; record a start time in Setup and wait for rows with
   `started_at >=` it, never by workflow name alone.

## Teardown hard rule

Every scenario that seeds `/etc/qdistro/workflows/wfgui-*.yaml`, a test
cgroup, or a vault item must remove all three in Teardown (and defensively
in Setup), then restart the broker so the next scenario starts clean.
