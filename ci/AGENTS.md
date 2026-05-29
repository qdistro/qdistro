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
