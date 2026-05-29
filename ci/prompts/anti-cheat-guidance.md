# Anti-cheat guidance for qdistro CI/test agents

You are running, triaging, or modifying qdistro CI or tests. A green run
only means something if the green is earned. The failure modes below are
the cheap ways to fake green; do none of them.

## The rules

1. **Do not weaken an assertion to make a test pass.** Loosening a
   `grep -qF` to a substring match, widening an argv comparison, dropping
   a `[ "$status" -eq 0 ]`, or changing an expected denial into an
   expected allow is a regression in coverage, not a fix. If the
   assertion is genuinely wrong, say so in your report and leave it for a
   human — see `tests/AGENTS.md` "golden rule".

2. **Do not raise or remove a timeout to mask a real failure.** A test
   that needs a longer wait is usually telling you something started
   slowly or never started. Diagnose it (journal, unit state, socket)
   instead of padding the deadline. qdistro already standardized on
   *loud* failure for missing preconditions: `tests/integration/vm/helpers.bash`
   provides `require` and `fail_loud` precisely so a missing dependency
   surfaces as a FAIL rather than a silent `skip`. Never regress a
   `require`/`fail_loud` call back into `skip "..."` — that is exactly
   the "all tests pass (most skipped)" green CI those helpers were added
   to kill.

3. **Do not turn a required failure into a warning or a skip.** qci
   already records a real status taxonomy — `pass`/`fail`/`skip`/`blocked`
   into `results.tsv` (see `ci/bin/qci`, `record_skip`/`record_blocked`).
   **Skip is not green.** A skip is an admission that the assertion did
   not run; do not reach for it to dodge a red gate. Visual scenario
   agents classify and explain failures, they do not turn a failing
   script green (see `ci/AGENTS.md` "Ground rules").

4. **Do not change a test's expected behavior without a product-code
   justification.** If the product changed and the test must follow, the
   diff must change product code too, and your report must say which
   product change forced the test change. A test edit with no
   corresponding product edit is, by default, a coverage regression.

5. **Every PASS must cite evidence.** A bare "PASS" is not a result. Cite
   the command and its output, the journal delta, the unit/socket state,
   or the artifact path (screenshot, log) that proves it. Save evidence
   under the qci run directory, never only in `/tmp` (see `ci/AGENTS.md`).

## Per-assertion evidence ("CheckResult")

When you add a new bats assertion or write an agent scenario verdict,
make each assertion carry its own evidence, modeled on a `CheckResult`:

- **Pass** — include the evidence: the command run and the relevant
  output line, the journal delta, or the artifact path.
- **Fail** — include both `expected` and `actual`. "Did not match" is not
  enough; show the expected string and what was actually observed.
- **Skip(reason)** — only for genuinely-not-applicable cases, with the
  reason. In VM bats tests a missing dependency is a `require`/`fail_loud`,
  not a skip.

State what each assertion `ensures:` — the user-visible capability it
protects. This is the test's reason to exist. Examples from qdistro:

- `ensures: qsu approval cannot be silently broadened to a different argv`
- `ensures: a denied clipboard transfer between silos stays denied`
- `ensures: the qdshell bar renders after idle, so the user can still act`

If you cannot state what an assertion ensures, you do not yet understand
what you are protecting — find out before you weaken or delete it.

The helper shims in `tests/integration/vm/helpers.bash`
(`assert_success`, `assert_output_contains`, `require`, `fail_loud`)
already print the failing command's output to stderr on failure. Keep new
assertions in that shape: visible evidence on the failing path.

For security-critical unit assertions (broker, qsu, browser bridge,
workflow, SELinux policy, approval cache), the opt-in
`@pytest.mark.cheat_aware(...)` marker in `tests/unit/conftest.py` prints
structured context (what the assertion protects, plausible cheats, the
consequence of a silent regression) when it fails. Use it on the
high-risk assertions so a future agent sees the stakes before touching
them.

## Where this applies

- VM bats tests: `tests/integration/vm/`
- Unit tests: `tests/unit/`
- GUI scenarios: `tests/integration/{permissions-gui,qdwin-noctalia,workflow-gui}/`
- The test-authoring rules: `tests/AGENTS.md`
- The CI-run rules: `ci/AGENTS.md`
