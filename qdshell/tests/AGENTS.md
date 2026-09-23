# Agent instructions for writing qdshell tests

You are adding or changing tests in qdshell — the qdwin shell (panel /
locker / notifications / approval UI, forked from Noctalia). Read this
before you touch anything under `tests/` or `Tests/`. The anti-cheat
rationale is shared with the sibling qdistro repo:
`../qdistro/ci/prompts/anti-cheat-guidance.md`.

## Golden rule: never reduce coverage

New test work is **strictly additive**. Do not delete a test, delete an
assertion, weaken a match, widen an argv/scope/identity comparison, raise
a timeout to mask a failure, loosen an `assert.deepStrictEqual` to a
substring/`ok` check, weaken `runner.judge()` so any framebuffer scores
PASS, loosen an expectation `.md`, or turn a real regression into a
`skip`/`xfail`. If an existing test looks wrong, **flag it in your report
and leave it** — a human decides whether the test or the product is at
fault. Silently making a test pass is a coverage regression, not a fix.

**Skip is not green.** A skip is an admission the assertion did not run;
never reach for it to dodge a red gate.

## Test layout

Three independent test frameworks live here. They cover different layers:

- **`Tests/tst_*.qml`** — QML unit tests (`qmltestrunner`). View/component
  logic that needs a QML engine but no live compositor. Run headless with
  `QT_QPA_PLATFORM=offscreen`.
- **`tests/test_*.js`** — pure-logic Node unit tests (CommonJS,
  `require("assert")`). These cover the **security-critical gate logic**
  extracted into plain JS modules under `Services/Qdshell/`:
  - `test_broker_gate.js` — `BrokerGate.parseStringVerdict` allow/deny;
    fail-closed to `deny` on broker-unavailable / malformed / unknown
    verdict; `qdwinDecision` allow=0 / deny=1.
  - `test_clipboard_broker.js` — `ClipboardBroker` cross-silo transfer
    verdict; fail-closed to `deny`; `hasKnownIdentity` boundary.
  - `test_clipboard_silo.js` — silo derivation; `instance_id` must never
    leak into a silo; missing app_id must NOT collapse into a shared
    engine bucket (fail safe to `""`).
  - `test_qdwin_only_guard.js` — source-text guard that no foreign-WM
    dispatch (`swaymsg`/`hyprctl`/…), dead identity flags, or foreign-WM
    config writers re-enter the tree. qdshell is **qdwin-only**.
  - the remaining `test_*.js` cover non-gate logic (taskbar, layout,
    notification theming, etc.).
- **`tests/ui/test_*.py`** — agent-assisted UI pytest. Each test drives a
  LIVE qdshell session and screenshots it, then a judge compares the
  framebuffer description against a human-authored golden in
  `tests/ui/expectations/*.md`. **These need a live qdwin VM to RUN**
  (`QDSHELL_UI_TESTS=1` + `QDSHELL_UI_VM=<vm>`); the host nested-compositor
  fallback SIGSEGVs on headless Wayland and so fails loudly rather than
  passing on a blank framebuffer. Collection works on the host without a
  VM. These are UI-render regression tests, not gate-bypass tests.

## How qci runs the tests

- Host gate: `scripts/ci-local.sh` runs `qmltest` (every `Tests/tst_*.qml`),
  `jstest` (every `tests/test_*.js` via `node`, skipped on a node-less
  host), `qmllint` (informational unless `--strict`), and `qmlformat
  --check` over `Services/Qdshell/`. Non-zero exit on any qmltest/jstest
  failure. `--no-int` skips the broker bats gate; `--quick` is qmltest
  only.
- Integration gate: the qdshell↔broker bats (`broker-e2e.bats`) is driven
  from the adjacent **qdistro** repo (`../qdistro/tests/integration/vm/`)
  and needs a broker-present VM; skipped when that repo/VM is absent.
- UI gate: `python3 -m pytest tests/ui` runs the screenshot suite against
  a live qdwin VM (see above). This is the agent-assisted `gui` path.

## `@pytest.mark.cheat_aware` (opt-in, pytest only)

`tests/ui/conftest.py` registers an opt-in `cheat_aware` marker
(propagated from qdistro `tests/unit/conftest.py`). The hook is **pure
pytest — no Qt/Quickshell import** — so it is valid even though the UI
tests only RUN on a VM. On a test's FAILURE it prints structured context:
what the assertion `protects`, the plausible `cheats` someone might use to
fake a pass, and the `consequence` of a silent regression. It is inert on
PASS and degrades gracefully if some kwargs are omitted. Apply it to
high-risk pytest tests only:

```python
@pytest.mark.cheat_aware(
    protects="the HooksGate approval UI cannot be bypassed",
    severity="critical",
    cheats=["weaken the judge to PASS", "turn a regression into skip/xfail"],
    consequence="an approval gate silently authorizes a denied action",
)
@pytest.mark.parametrize("surface", SETTINGS_SURFACES, ids=lambda s: s.id)
def test_settings_tab(...):
    ...
```

Currently applied to `test_settings_tab` (covers the Hooks/Lock Screen
config surfaces) and `test_panel` (covers the notifications panel).

### Equivalent discipline for the QML / JS gate tests

The **most security-critical tests in this repo are the Node tests**
(`test_broker_gate.js`, `test_clipboard_broker.js`, `test_clipboard_silo.js`,
`test_qdwin_only_guard.js`), not pytest. `cheat_aware` is a pytest marker
and **must not be forced onto them.** Apply the same discipline by other
means: every gate assertion must state, in a comment above it, the
user-visible capability it `ensures:` and must fail closed. Examples:

- `ensures: an unavailable/malformed broker reply fails closed to deny`
- `ensures: a cross-silo clipboard transfer the broker denied stays denied`
- `ensures: instance_id never leaks into a clipboard silo identity`
- `ensures: no foreign-WM dispatch re-enters the qdwin-only tree`

Do not loosen `deepStrictEqual` to a weaker check, do not broaden an
identity/silo comparison, and do not delete a fail-closed branch to make a
refactor pass. If a gate test must change, the diff must change the
`Services/Qdshell/*.js` product code too, and your report must say which
product change forced it.

## Evidence discipline (all layers)

Every assertion must make its evidence visible on the failing path. The
Node tests use `assert.*` (prints expected vs actual on failure); keep new
assertions in that shape and add a one-line summary `console.log` at the
end of the file. The pytest UI tests already dump `missing`/`extra`/judge
output and the `png` path on a failing verdict — preserve that. For a PASS
to mean anything it must be earned: cite the command/output, journal
delta, or artifact path, never a bare "PASS". See
`../qdistro/ci/prompts/anti-cheat-guidance.md`.

## Constraints

- **PyQt6, never PySide6.** If you add any host pytest that imports Qt,
  guard it with `pytest.importorskip("PyQt6.QtWidgets")` and use PyQt6;
  PySide6's `libQt6Core` shadows PyQt6 and would silently skip the tests.
  Do not add PySide6 as a test dependency. (qdshell's current pytest is
  Quickshell-driven over IPC and imports no Qt directly.)
- **Do not commit PNG or other image files to the repo.** The UI
  screenshots under `tests/ui/artifacts/` are generated at runtime into
  the qci run directory; a scenario describes what to capture and assert,
  never a baked-in golden image. Expectations are human-authored markdown
  (`tests/ui/expectations/*.md`), not images.
