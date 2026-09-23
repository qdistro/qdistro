# Agent instructions for writing qdlocker tests

qdlocker is the qdistro **screen locker** (a standalone Python+QML peer
client to qdwin, bound to `qdwin_locker_v1`). Its tests guard a security
boundary: *the lock cannot be bypassed and unlock requires a real auth
result.* Read this before you touch anything under `tests/`.

## Golden rule: never reduce coverage

New test work is **additive**. Do not delete a test, delete an
assertion, weaken a match, widen a uid/argv/generation comparison, raise
a timeout to mask a hang, turn a fail into a `skip`, or assert on a stub
where the test meant to exercise the real gate. If an existing test
looks wrong, **flag it in your report and leave it** — a human decides
whether the test or the product is at fault. Silently making a test
green is a coverage regression, not a fix.

## Layout

- `tests/unit/` — pure-Python pytest. No VM, no compositor, no real
  D-Bus or fprintd; fakes are injected (`sys.modules`, `monkeypatch`,
  stub `QObject`s). qci runs the suite from the repo root with:

  ```
  python3 -m pytest tests/unit
  ```

  Current files:
  - `test_auth_fprintd.py` — the fingerprint (`AuthBackend._fprint_async`)
    path is timeout-bounded and fails CLOSED; generation/strike accounting.
  - `test_controller.py` — `LockController` state machine, including the
    stale-outcome / session-generation race guards.
  - `test_ctrl_peercred.py` — the `qdlocker.sock` SO_PEERCRED gate: serves
    only the session owner, fails closed on unreadable creds.
- `tests/gui/` — VM/compositor scenarios (`NN-*.md`) driven by
  graphic-aware subagents against a live qdwin + qdlocker session. These
  need a VM/GUI and cannot run on a plain unit host. See
  `tests/gui/AGENTS.md` (read it before authoring a scenario) and
  `tests/gui/README.md`.

Test config (testpaths, `pythonpath = ["."]`, the `cheat_aware` marker)
lives in the repo-root `pyproject.toml` under
`[tool.pytest.ini_options]`. The failure hook lives in
`tests/unit/conftest.py`. Module imports of `qdlocker` work via
`pythonpath`; do not litter `sys.path` hacks across test files.

## PyQt6, never PySide6

qdlocker pins **PyQt6** — `pyproject.toml` sets `qt_api = "pyqt6"` so
pytest-qt does not auto-import PySide6 first. If PySide6 loads first its
bundled `libQt6Core.so` shadows PyQt6 and the subsequent PyQt6 import
fails with a missing `Qt_*_PRIVATE_API` symbol. Do not add PySide6 as a
test dependency, and keep `tests/unit/conftest.py` **import-clean of
Qt** — the marker hook there must stay pure pytest so it can never
perturb the Qt load order.

## Evidence on failure

Every assertion must make its evidence visible on the failing path, and
you should be able to state what user-visible capability it `ensures:`
(e.g. "a cross-uid peer cannot read the locker's prompt-length side
channel", "a stale SUCCESS from a superseded lock cannot unlock the
screen"). Prefer assert messages that print expected vs. actual. If you
cannot state what an assertion ensures, you do not yet understand what
you are protecting — find out before you weaken or delete it.

## `@pytest.mark.cheat_aware` (opt-in, security-critical)

Apply the opt-in `cheat_aware` marker to the high-risk lock/unlock
assertions only — lock cannot be bypassed, unlock requires real auth,
PIN/password/fingerprint verification, the ctrl-socket peer gate,
auto-unlock / idle / stale-outcome policy. On **failure** it prints
structured context — what the assertion `protects`, plausible `cheats`
to fake a pass, and the `consequence` of a silent regression — so the
next agent sees the stakes before touching it. It is inert on pass and
opt-in; ordinary tests do not need it.

```python
@pytest.mark.cheat_aware(
    protects="a stale auth outcome from a superseded session cannot unlock",
    severity="critical",
    cheats=["emit with the current generation", "drop the fails == [] check"],
    consequence="a laggy SUCCESS from a previous lock unlocks the screen",
)
def test_...():
    ...
```

Decorator only — never edit a test body to add it. All kwargs are
optional; the failure block degrades gracefully if some are omitted.
The marker is registered in `pyproject.toml`'s `markers` list; the hook
that renders it is in `tests/unit/conftest.py`.

## No images in the repo

Do not commit PNG or other image files. GUI scenarios capture
screenshots at runtime into the run directory; they are never committed.
A scenario describes what to capture and assert, not a baked-in golden
image.
