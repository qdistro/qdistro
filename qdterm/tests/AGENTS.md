# Agent instructions for writing QTerminator tests

Read this before you touch anything under `tests/`. QTerminator is a Qt
port of the Terminator terminal emulator (PyQt6 + QTermWidget); the tests
guard terminal-emulation correctness and the agent-control / shell-
integration surfaces that let external clients drive a terminal.

## Golden rule: never reduce coverage

New test work is **additive**. Do not delete a test, delete an assertion,
weaken a match, widen a comparison, raise a timeout to mask a failure, or
turn a hard assertion into a `skip`. If an existing test looks wrong,
**flag it in your report and leave it** — a human decides whether the test
or the product is at fault. Silently "fixing" a test by making it pass is
a coverage regression, not a fix.

## Layout map

- `tests/*.py` — pytest unit/component tests. `qci` runs them from the
  repo root with `python3 -m pytest`. Most are GUI-component tests driven
  by `pytest-qt` (`qtbot`) under the offscreen Qt platform; a subset
  (e.g. the OSC parser tests in `test_shell_integration.py`) are pure
  Python.
- `tests/conftest.py` — shared fixtures: forces
  `QT_QPA_PLATFORM=offscreen` (set before any `QApplication`), provides a
  `_FakeQTermWidget` stub when the `QTermWidget` C++ binding is absent,
  and an autouse fd-cleanup fixture (each QTermWidget opens PTY fds — skip
  the cleanup and tests exhaust the fd limit). It also registers the
  opt-in `cheat_aware` marker (below). Add shared fixtures here; do not
  litter per-file hacks.
- `tests/integration/<feature>/NN-*.md` — markdown scenarios executed by
  agents, not by pytest (agent-control, shell-integration, tmux-mode,
  asciinema, triggers, qterm-todo). They are not collected by
  `python3 -m pytest`.

## PyQt6, never PySide6

This project uses **PyQt6** (`qt_api = "pyqt6"` in `pyproject.toml`).
PySide6's `libQt6Core` can shadow PyQt6 and silently break or skip Qt
tests. Do not add PySide6 as a test dependency and do not paper over Qt
import guards. Tests run headless via `QT_QPA_PLATFORM=offscreen` (the
conftest sets this); a few visual scenarios need a real display/VM and are
called out in their scenario `.md`.

## Evidence discipline

Every assertion must make its evidence visible on the failing path, and
you must be able to state what user-visible capability it protects (e.g.
"send_text is refused until the client attached", "the control socket
rejects a foreign uid"). Prefer asserting on concrete values (error codes,
buffer bounds, captured text) over presence-only checks. If you cannot
state what an assertion protects, you do not yet understand it — find out
before you weaken or delete it.

## `@pytest.mark.cheat_aware` (opt-in, critical tests)

`tests/conftest.py` registers an opt-in marker. On **failure** of a marked
test it prints a structured block — what the test `protects`, the
plausible `cheats` an agent might use to fake a green, and the
`consequence` of a silent regression — so the next agent sees the stakes
before touching it. It is inert on PASS and opt-in: ordinary tests do not
need it. Apply it to the high-risk surfaces only: agent-control socket
auth / input gating, shell-integration OSC parsing safety, and any
secret-exposure or command-injection assertion.

```python
@pytest.mark.cheat_aware(
    protects="the agent-control socket rejects connections from other uids",
    severity="critical",
    cheats=["assert the connection stays open", "drop the uid check"],
    consequence="any local user could drive another user's terminals",
)
def test_uid_mismatch_rejected(...):
    ...
```

All kwargs are optional; the report block degrades gracefully when some
are missing. The marker registration and report hook are pure pytest (no
Qt import) so they work even where the QTermWidget binding/display is
unavailable.

## No image files

Do not commit PNG or other image files as test fixtures or golden images.
Screenshots produced during scenarios are generated at runtime, not
baked into the repo.
