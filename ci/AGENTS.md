# Agent instructions for qdistro CI

You are running or triaging local CI for the qdistro umbrella checkout.

## Ground rules

- Do not mutate `qdistro-daily` or any `qdistro-daily-*` VM unless the user
  explicitly asks. CI should use disposable `qci-*` VMs.
- Do not edit source while running CI. If you identify an easy fix, write it as
  a recommendation in the report or create a separate branch after the CI run is
  complete.
- Treat script exit status as authoritative. Visual agents classify and explain
  failures; they do not turn a failing script green.
- Save evidence under the current run directory. Never leave the only evidence
  in `/tmp`.
- On failure, preserve the VM unless the user asked for deletion.

## Test integrity / anti-cheat

- Do not weaken an assertion, widen a match, or drop a status check to
  make a test pass. New work is additive; flag wrong tests, don't quietly
  change them.
- Do not raise or remove a timeout to hide a real failure — diagnose it.
  Never regress a `require`/`fail_loud` (see
  `tests/integration/vm/helpers.bash`) back into a silent `skip`.
- Skip is not green. qci records `pass`/`fail`/`skip`/`blocked` for a
  reason (`record_skip`/`record_blocked` in `ci/bin/qci`); never convert a
  required failure into a warning or skip to dodge a red gate.
- A test's expected behavior changes only with a corresponding product-code
  change, and your report must name that change.
- Every PASS cites evidence: command + output, journal delta, unit/socket
  state, or artifact path. A bare "PASS" is not a result.
- Full details and the per-assertion evidence model: see
  `ci/prompts/anti-cheat-guidance.md` (and `tests/AGENTS.md` for authoring).

## Standard flow

1. Run `qdistro/ci/bin/qci preflight`.
2. If preflight passes, run `qdistro/ci/bin/qci host`.
3. For full validation, run `qdistro/ci/bin/qci full`.
4. Read `report.md` or `report.html` in the run directory.
5. If a VM failed and was preserved, use the VM name from `manifest.txt` and the
   linked artifacts before rerunning anything broad.

## Debugging a failed run

Start from the artifacts, not from a fresh VM. The run directory under
`qdistro/ci/runs/<gate>-<utc>-<pid>/` already holds most of what you need.

1. **See what failed.** `qci report --latest` / open `report.md` (or `.html`);
   for machine parsing read `results.tsv` (`gate  subject  status  exit_code
   exit_class  kind  log  notes  category`). `qci triage --latest` and
   `qci list-runs` help locate the run. Each failing row links its log under the
   run dir; journals/screenshots/per-scenario artifacts are in `journals/`,
   `screenshots/`, `gui/`, `vm/`, and `bats/`.

2. **Find the preserved VM.** Failed disposable VMs are kept for debugging.
   `manifest.txt` records `preserved_failed_vm=<name>` and
   `preserved_failed_vm_state=<hibernated|powered_off|running>`; the
   `lifecycle` rows in `results.tsv` say the same.

3. **Wake the VM — it is hibernated, not running.** To stop preserved failed
   VMs from exhausting host RAM (a long `bats` run spins one disposable VM per
   file), qci hibernates each failed VM with `virsh managedsave` instead of
   leaving it running. Resume the exact failed state with:

   ```bash
   virsh -c qemu:///session start <vm>     # restores hibernated/powered-off state
   ```

   `hibernated` resumes the live guest where it failed; `powered_off` boots
   fresh from the preserved disk — the on-disk logs/journals are intact either
   way. In practice qci VMs are `powered_off`: the `qdistro-template` CPU is
   host-passthrough with a non-migratable `invtsc` flag, so `managedsave`
   (which uses the migration path) fails and qci falls back to power-off. Set
   `QCI_KEEP_FAILED_VM_RUNNING=1` on the run to keep failed VMs running instead.

4. **Inspect inside the VM** (after `virsh start`), via
   `scripts/vm/vm-exec <vm> '<cmd>'` (root over qemu-guest-agent):
   `journalctl -b --no-pager`, `systemctl --user --machine=admin@.host status
   <unit>`, check `/run/user/1000/wayland-*` and the relevant sockets,
   `scripts/vm/vm-gui <vm> screenshot` for visual state. Save anything new under
   the run dir, never only in `/tmp`.

5. **Rerun narrowly against the preserved VM**, not the whole gate:
   `qci replay <scenario> --vm <vm>`, `qci bats --vm <vm> --file <f.bats>`, or
   `qci gui --vm <vm> --scenario <path.md>`. An explicit `--vm` is never
   auto-deleted on pass.

6. **Clean up when done.** `qci cleanup` (honours `--dry-run`/`--age-hours`)
   removes stale `qci-*` VMs and their managedsave images; never touches
   `qdistro-daily*`. Manually: `virsh -c qemu:///session destroy <vm>` then
   `virsh -c qemu:///session undefine <vm> --remove-all-storage --managed-save`
   (the `--managed-save` flag is required to undefine a hibernated VM).

## Visual scenario flow

`qci gui` creates one prompt per markdown scenario under
`agent-notes/*.prompt.md`. For each prompt:

1. Read the scenario file and its nearest `AGENTS.md`.
2. Pin `VMNAME` to the VM named in the prompt.
3. Execute setup, steps, assertions, and cleanup serially.
4. Save screenshots and command logs into the artifact directory named in the
   prompt.
5. Return nonzero for FAIL or ERROR.

When deciding root cause:

- Missing or malformed Wayland/qdshell protocol behavior belongs in qdwin or
  qdwin's libweston integration unless evidence proves otherwise.
- GUI automation flake should have evidence: input command, screenshot before
  and after, and journal delta.
- Visual mismatch should cite the screenshot path and the visible mismatch.

## Triage output

When asked to triage a run, return:

- most likely root cause;
- evidence paths;
- minimal next probe;
- whether the preserved VM should be kept;
- a concrete fix recommendation, with file paths when known.
