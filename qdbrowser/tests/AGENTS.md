# Agent instructions for writing qdbrowser tests

You are adding or changing tests in qdbrowser, the browser / browser-bridge.
Read this before you touch anything under `tests/`.

## Golden rule: never reduce coverage

New test work is **additive**. Do not delete a test, delete an assertion,
weaken a match (e.g. an exact bus-name compare into a substring match),
widen a comparison, raise a timeout to mask a failure, or turn a failing
assertion into a `skip`. **Skip is not green** — a skip is an admission the
assertion did not run. If an existing test looks wrong, **flag it in your
report and leave it** — a human decides whether the test or the product is
at fault. Silently "fixing" a test by making it pass is a coverage
regression, not a fix.

## Layout

- `tests/test_*.py` — the pytest suite. Most are pure-Python (config,
  autofill orchestrator, bridge handlers, content blocker); the
  widget/webview tests use `pytest-qt` with an offscreen, headless,
  no-sandbox Chromium (set up in `tests/conftest.py`).
- `tests/integration/` — scenario drivers that talk to a running qdbrowser
  over the agent socket; see the top-level `AGENTS.md`.
- `tests/conftest.py` isolates `HOME`/`XDG_*` into a temp dir and forces
  `QT_QPA_PLATFORM=offscreen` + headless Chromium flags **at import time**,
  before any qdbrowser module computes paths. Preserve that ordering and
  the env wiring if you edit it.

## How CI runs the suite

qci runs the tests **per file**, not as one session:

```sh
for t in tests/test_*.py; do python3 -m pytest "$t"; done
```

This is deliberate. QtWebEngine leaves residue (Chromium subprocesses,
profile/cache state, signals firing during destruction) that accumulates
across a single pytest session and causes flaky cross-test failures.
One process per file bounds that residue. Consequences for authors:

- A test must pass when its file is run **in isolation** — do not rely on
  another file's fixtures, import order, or leftover state.
- Keep WebEngine widgets/profiles disposable; the autouse cleanup and
  `pytest_sessionfinish` in `conftest.py` drain Qt events and tear down
  profiles. Match that shape rather than leaking widgets.

## PyQt6, never PySide6

Use **PyQt6** (`qt_api = "pyqt6"` is pinned in `pyproject.toml`). PySide6's
`libQt6Core` can shadow PyQt6 and silently break or skip Qt tests. Do not
add PySide6 as a test dependency. Import `PyQt6.QtWebEngineWidgets` before
`QApplication` (conftest already does).

## Evidence discipline

Every assertion must make its evidence visible on the failing path, and you
must be able to state what user-visible capability it `ensures:` (e.g. "the
browser bridge cannot leak credentials or be impersonated"). A bare PASS is
not a result; on failure show expected vs. actual. Do not change a test's
expected behavior without a corresponding product-code change, and say in
your report which product change forced it.

## `@pytest.mark.cheat_aware` (opt-in, security-critical)

`tests/conftest.py` registers an opt-in `cheat_aware` marker. It is inert on
PASS; on FAIL a `pytest_runtest_makereport` hookwrapper prints structured
context — what the assertion `protects`, the `severity`, the plausible
`cheats` someone might use to fake a pass, and the `consequence` of a silent
regression — so the next agent sees the stakes before touching it. The hook
is **pure pytest** (no Qt import), so it stays valid in the per-file runs.

Apply it to high-risk assertions only — the browser-bridge identity /
permission boundary, intent-token binding, credential/cookie/autofill
handling. Ordinary tests do not need it. All kwargs are optional; the report
block degrades gracefully if some are missing.

```python
@pytest.mark.cheat_aware(
    protects="the browser bridge cannot leak credentials or be impersonated",
    severity="critical",
    cheats=["drop the HMAC assertion", "widen the bridge-name match"],
    consequence="a same-uid process autofills credentials into a page",
)
def test_...():
    ...
```

Currently annotated (the credential/impersonation invariants):

- `test_pwd_autofill.py::TestMintIntentToken`-adjacent
  `test_intent_token_in_bridge_call` — fill requests are HMAC-bound to the
  session secret.
- `test_pwd_autofill.py::TestSelectBridgeNames::test_only_numeric_suffixes_accepted`
  — only `BrowserBridge.<pid>` names are trusted; a same-uid attacker name
  is filtered out.
- `test_bridge_adapter_handlers.py::test_polkit_denies_blocks_dispatch` — a
  polkit-denied mutating method is refused before dispatch.
- `test_bridge_adapter_handlers.py::test_recv_loop_denies_when_pid_resolution_fails`
  — an unresolvable caller PID is denied (AccessDenied), never allowed
  through.

## No image files

Do not commit PNG or other image files. Screenshots captured during
integration scenarios are generated at runtime into the run directory and
are never committed.
