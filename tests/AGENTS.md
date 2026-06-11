# Agent instructions for writing qdistro tests

You are adding or changing tests in qdistro. Read this before you touch
anything under `tests/`. For the rules about *running* CI see
`ci/AGENTS.md`; for the anti-cheat rationale see
`ci/prompts/anti-cheat-guidance.md`.

## Golden rule: never reduce coverage

New test work is **additive**. Do not delete a test, delete an assertion,
weaken a match, widen an argv/scope comparison, raise a timeout to mask a
failure, or turn a `require`/`fail_loud` into a `skip`. If an existing
test looks wrong, **flag it in your report and leave it** — a human
decides whether the test or the product is at fault. Silently "fixing" a
test by making it pass is a coverage regression, not a fix.

## Layout map

- `tests/unit/` — pure-Python pytest. No D-Bus, no Qt display, no VM;
  runs in <1s on the host. Covers broker cache/audit, the
  `qdistro-approvals` CLI, scope round-trips, and (with PyQt6) the admin
  app. See `tests/unit/README.md`.
- `tests/integration/vm/` — bats tests plus `sNN-*` shell drivers in
  `scripts/vm/`. Require a running VM; the bats harness runs on the host
  and `vm-exec`s into the guest. See `tests/integration/vm/README.md`.
- `tests/integration/permissions-gui/`,
  `tests/integration/qdwin-noctalia/`,
  `tests/integration/workflow-gui/` — markdown GUI scenarios
  (`NN-*.md`) executed by graphic-aware visual agents, not by bats. Each
  directory has its own `AGENTS.md` describing the runner contract; read
  the nearest one before authoring a scenario.

## Unit tests (`tests/unit/`)

### Markers and the fast subset

The `unit` suite holds a few integration-grade tests that spin up real
subprocesses (e.g. the ssh-agent relay tests). They are tagged so the fast
host run can deselect them. The marker vocabulary is registered in
`pyproject.toml` (`[tool.pytest.ini_options].markers`) and `conftest.py`,
and is shared with `ci/TAXONOMY.md`:

- `slow` — integration-grade test with real subprocesses or multi-second
  setup.
- `integration` — exercises more than one component together (still
  in-process, no VM).
- `needs_ssh` — requires `ssh-agent`/`ssh-add`/`ssh-keygen` on `PATH`.
- `cheat_aware` / `no_attest` — registered in `conftest.py` (see below).

Fast subset (skip the integration-grade tests on a host without ssh
tooling or when you want a sub-second run):

```bash
python3 -m pytest -m "not slow and not needs_ssh"
```

Run `pytest --markers` to see the registered set; new markers go in
`pyproject.toml` (or `conftest.py` for the programmatic ones) so
`--strict-markers` stays clean.

**De-flaking rule:** prefer a readiness signal (an `Event`, a poll for the
condition you actually need — e.g. a server thread reaching its blocked
state) over a fixed `time.sleep`. A sleep that merely *orders* threads or
*approximates* "the other side is ready now" is a latent flake; replace it
with a deterministic wait that asserts the precondition and fails loudly on
timeout. Keep the SAME assertions — only make the readiness deterministic.
A `sleep` that is itself the work under test (slowness deliberately
injected into a fake to open a race window) is fine; leave it and add the
relevant marker.

- `conftest.py` wires `sys.path` so the per-component source dirs
  (`broker/`, `cli/`, `pwd/`, `workflow/`, `browser_bridge/`, …) import as
  top-level modules. Add a new `sys.path.insert` there when you test a new
  component dir; do not litter `sys.path` hacks across test files.
- **Use PyQt6, never PySide6.** The admin-app tests guard their imports
  with `pytest.importorskip("PyQt6.QtWidgets")`, so in an environment
  where PySide6's `libQt6Core` shadows PyQt6 those ~150 Qt tests would
  *silently skip* instead of failing. `conftest.py` turns that into a hard
  collection error when `QDISTRO_REQUIRE_PYQT6=1` (qci sets it). Do not
  add PySide6 as a test dependency and do not paper over the guard.
- `test_permission.py` is the in-VM acceptance script (deployed to
  `/usr/local/bin/qdistro-test-permission`), not a unit test; it is in
  `collect_ignore`. Leave it there.

### `@pytest.mark.cheat_aware` (opt-in, security-critical)

A sibling task is adding an opt-in `@pytest.mark.cheat_aware(...)` marker
to `tests/unit/conftest.py`. Its purpose: on failure, print structured
context — what the assertion `protects`, the plausible `cheats` someone
might use to fake a pass, and the `consequence` of a silent regression —
so the next agent sees the stakes before touching it. Apply it to
high-risk assertions only (broker, qsu, browser bridge, workflow, SELinux
policy, approval cache). It is opt-in, not blanket; ordinary tests do not
need it.

```python
@pytest.mark.cheat_aware(
    protects="qsu approval cannot be broadened silently",
    severity="critical",
    cheats=["change expected denial to skip", "broaden argv match"],
    consequence="admin approval authorizes the wrong command",
)
def test_...():
    ...
```

## VM / bats tests (`tests/integration/vm/`)

- Source the shared helpers with `load helpers` at the top of the `.bats`
  file (bats resolves `helpers.bash` relative to the test file).
- Run guest commands through the driver helpers in
  `tests/integration/vm/helpers.bash`:
  - `vm_run <cmd>` — run as root in the VM (routes via qemu-guest-agent,
    or SSH when `VM_SSH_PORT` is set for enforcing-mode VMs). Captures
    `$output` and `$status`.
  - `vm_run_admin <cmd>` — run as the `admin` user (uid 1000) with a real
    PAM session and admin's `--user` systemd; use it for anything that
    needs `systemctl --user`, `qdlocker.sock`, `qdshell.sock`,
    `noctalia-shell.service`, etc.
  - `assert_success` / `assert_output_contains` — tiny shims that print
    the failing command's output to stderr.
- **Waiters — prefer them over hand-rolled `sleep` loops.** A sibling task
  is adding `step`/`subtest` grouping plus the waiters
  `wait_for_unit`, `wait_for_socket`, `wait_for_file`,
  `wait_for_journal_line`, and `wait_until_succeeds` to
  `helpers.bash`. These are the standard way to wait or poll for a
  condition: they poll until the condition holds or a bounded deadline
  elapses, then fail loudly with the last observed state. Do not write
  bare `sleep 5` followed by an assertion — that is both flaky and a
  hidden timeout. Use the waiter that names what you are waiting for.
- **Missing preconditions fail loudly — never `skip`.** Use `require` for
  a precondition check and `fail_loud` for an unconditional failure
  branch (e.g. after a driver prints `SKIP:` in its output). Deps belong
  in the bake; a missing dep is a bake regression and must surface as a
  FAIL, not masquerade as "all tests pass (most skipped)".

  ```bash
  vm_run "command -v xfreerdp >/dev/null"
  require "xfreerdp not installed on VM (need freerdp3 package)"
  ```
- **vm-exec qga JSON-quoting is fragile.** `vm-exec` builds qemu-ga's JSON
  payload by string concatenation, so any embedded `"` breaks the parse.
  Prefer a single run-from-script driver under `scripts/vm/` invoked with
  a quote-free command line; when you must inline a script or SQL with
  quotes, base64-wrap it (`echo <b64> | base64 -d | bash`). Avoid embedded
  double-quotes in the command you hand `vm_run`.

## Evidence discipline (all layers)

Every assertion must make its evidence visible on the failing path, and
you must be able to state what user-visible capability it `ensures:`
(e.g. "a denied cross-silo clipboard transfer stays denied"). Follow the
per-assertion `CheckResult` model — Pass{evidence} / Fail{expected,actual}
/ Skip(reason) — described in `ci/prompts/anti-cheat-guidance.md`. The
`require`/`fail_loud`/`assert_*` helpers already print evidence on
failure; keep new assertions in that shape. For GUI scenarios, the
per-assertion report format is defined by each directory's `AGENTS.md`
and by `ci/prompts/gui-scenario-agent.md`.

## Constraints

- **Do not commit PNG or other image files to the repo.** Screenshots
  used during GUI tests are generated at runtime and saved into the qci
  run directory; they are never committed. A scenario describes what to
  capture and assert, not a baked-in golden image.

- **OCR assertions — deferred.** Shared OCR helpers were proposed but are
  **postponed pending a decision**; there is no OCR helper API to use yet.
  For now, GUI text checks go through visual agents per the
  `tests/integration/permissions-gui/AGENTS.md` flow (a vision-capable
  agent reads text off the screenshot, or `tesseract` when explicitly
  available). Do not author or document an OCR helper API.
