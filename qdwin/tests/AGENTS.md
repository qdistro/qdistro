# Agent instructions for writing qdwin tests

You are adding or changing tests in qdwin, the C/Wayland (libweston)
compositor. Read this before you touch anything under `tests/`. It is the
top-level test-authoring contract; the per-directory `AGENTS.md` files
(`tests/host/AGENTS.md`, `tests/gui/AGENTS.md`, `tests/apps/AGENTS.md`)
describe how to *run* each scenario suite and take precedence on harness
mechanics.

qdwin is **not a pytest project**. It is meson + C, with shell helpers and
markdown GUI scenarios. The `@pytest.mark.cheat_aware` marker used in the
Python sibling repos (`qdistro/tests/unit/`) **does not apply here — do not
add it.** The discipline it encodes still applies: every test must declare,
in its name or a comment, what user-visible compositor behavior it
protects.

## Golden rule: never reduce coverage

New test work is **additive**. Do not delete a test, delete or weaken an
assertion, widen a `grep`/regex match, drop a status check, raise a timeout
to mask a failure, turn a FAIL into a SKIP, or disable a meson `test()` to
go green. If an existing test looks wrong, **flag it in your report and
leave it** — a human decides whether the test or the compositor is at
fault. Silently "fixing" a test by making it pass is a coverage regression,
not a fix.

## Layout map

- `tests/*` meson unit tests — declared with `test()` in the top-level
  `meson.build`. Currently `xdg-activation-policy`
  (`qdwin/test_xdg_activation_policy.py`), a Python script that greps
  `qdwin/qdwin.c` to assert the xdg-activation gate policy stays in place.
  It is run *by meson*, not by pytest; it takes the C source path as argv
  and prints `FAIL: <reason>` on a broken invariant. Add new policy /
  source-invariant checks the same way: a standalone script wired with
  `test(...)`, exiting non-zero with an explanatory line on failure.
- `tests/host/` — headless host-side GUI scenarios. `NN-*.md` scenarios
  executed by vision-capable agents; `start.sh` / `stop.sh` /
  `screenshot.sh` / `ctrl.sh` / `lib.sh` are the bash harness. weston runs
  `--backend=headless`; no windows on the host. See `tests/host/AGENTS.md`.
- `tests/gui/` — qdwin-on-VM GUI scenarios. `NN-*.md` plus the executable
  `agent-*.sh` smokes (`agent-click-smoke.sh`,
  `agent-mvp-session-smoke.sh`, `agent-protocol-audit.sh`,
  `agent-cursor-clickthrough-smoke.sh`) and `qdwin-helpers.sh`. See
  `tests/gui/AGENTS.md`.
- `tests/apps/` — third-party app-compatibility scenarios driven through a
  `qdwin-bystander` client; `NN-*.md` plus `qdwin-apps-helpers.sh`. See
  `tests/apps/AGENTS.md`.

## How qci runs the tests

qci builds and runs qdwin roughly as:

```
meson setup build-qci
meson compile -C build-qci
meson test -C build-qci
bash -n <each tests/*.sh>     # syntax-check every shell helper/smoke
```

Two consequences for authors:

1. A new unit/policy check only runs if it is registered with `test()` in
   `meson.build` and the build compiles. An orphaned script qci never
   invokes is not coverage. Wire it in.
2. Every shell file under `tests/` must pass `bash -n` (parse without
   executing). Keep helpers and smokes `bash -n`-clean — no syntax that
   only the interactive shell tolerates. The GUI `NN-*.md` scenarios are
   executed by agents, not by `meson test`; the executable `agent-*.sh`
   smokes are the path that runs under CI on a VM.

## Evidence on failure

A failing test must print **what it expected vs. what it got**, and the
test's name or a leading comment must say **what user-visible compositor
behavior it protects**. A bare non-zero exit is not enough.

- Meson/Python policy checks: print `FAIL: <expected vs actual>` (the
  pattern `test_xdg_activation_policy.py` already uses) before returning
  non-zero, so the meson test log shows the broken invariant.
- Shell smokes: on a failed assertion, echo the expected string, the
  observed value (the offending journal line, the missing global, the
  focus handle that did not match), and the artifact path
  (`/tmp/.../*.png`, the qdwin/weston log) before exiting non-zero.
- GUI `NN-*.md` scenarios: per-assertion PASS/FAIL with the screenshot
  region or `list` snapshot that justified the verdict — the report format
  in the per-directory `AGENTS.md`.

State what each test `ensures:` — the capability it protects. Examples in
this repo: "xdg-activation cannot be triggered without a focused-surface
gate", "click at (X,Y) raises and focuses the toplevel under the cursor",
"alt+tab cycles focus via qdwin_shell_v1 and survives close-and-refocus",
"maximized window leaves room for chrome + panel (not fullscreen)". If you
cannot state what a test ensures, you do not yet understand what you are
protecting — find out before you weaken or delete it.

## Anti-cheat rules (meson / C / shell)

A green run only counts if the green is earned. Do none of these:

1. **Do not weaken an assertion to make a test pass.** Loosening a
   `grep -qF` to a substring, widening a regex in a policy check, dropping
   an exit-status check, or flipping an expected denial into an expected
   allow is a coverage regression, not a fix.
2. **Do not raise or remove a timeout to hide flakiness.** A smoke that
   needs a longer wait is usually telling you a unit, socket, or surface
   started slowly or never started. Diagnose it (weston/qdwin log, the
   ctrl-socket / bystander FIFO state, `loginctl`) instead of padding the
   deadline. Prefer a bounded poll that fails loudly with the last
   observed state over a blind `sleep N` followed by an assertion.
3. **Do not disable a meson `test()` to go green.** Commenting out,
   removing, or guarding-away a `test(...)` entry so `meson test` reports
   success is hiding a regression, not fixing one.
4. **Do not turn a FAIL into a SKIP** (an early `exit 0`, an `echo "SKIP:
   ..."` that the caller treats as green, or a missing-precondition branch
   that returns success). A skipped check did not run; skip is not green.
   A missing test dependency is an environment/bake regression and must
   surface as a FAIL, not a quiet pass — fix the environment, not the
   assertion.
5. **Do not change a test's expected behavior without a matching
   compositor-code change.** If `qdwin.c` (or qdshell) genuinely changed
   and a test must follow, the diff must include that source change and
   your report must name which behavior change forced the test edit. A
   test edit with no corresponding source edit is, by default, a coverage
   regression.
6. **Every PASS cites evidence.** A bare "PASS" is not a result — cite the
   command and its relevant output line, the log delta, the global/socket
   state, or the screenshot path that proves it.

## No images in the repo

**Do not commit PNG or other image files.** Screenshots taken during GUI
scenarios are generated at runtime under `/tmp/qdwin-host-tests/<id>/` (or
captured to a host path on the VM suites) and are referenced from the
report, never checked in. `.gitignore` already excludes `*.screenshot.png`,
`shot-*.png`, and `**/screenshots/`; keep it that way. A scenario describes
what to capture and assert, not a baked-in golden image.
