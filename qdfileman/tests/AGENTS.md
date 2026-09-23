# Agent instructions for writing qfileman tests

Read this before you touch anything under `tests/`. These rules are ported
from qdistro's test-integrity tooling and apply to this repo.

## Golden rule: never reduce coverage

Test work is **strictly additive**. Do not delete a test, delete or weaken
an assertion, widen an exact match into a substring/membership check, raise
a timeout to mask a failure, or convert a `fail`/`assert` into a `skip`. If
an existing test looks wrong, **flag it in your report and leave it** — a
human decides whether the test or the product is at fault. Silently
"fixing" a test by making it pass is a coverage regression, not a fix.

## Layout

- `tests/` — pure-Python pytest plus PyQt6 widget tests. qci runs the whole
  suite from the repo root with:

  ```
  python3 -m pytest
  ```

- `conftest.py` — shared fixtures (`tmp_dir`, `nested_tmp_dir`, `tmp_tree`),
  an autouse Qt-cleanup fixture, and the `cheat_aware` marker (registration
  + failure-report hook). Qt runs under the offscreen platform plugin by
  default (`QT_QPA_PLATFORM=offscreen`); override with `QT_QPA_PLATFORM=xcb`
  for an on-screen run. Most tests run headless on the host; none currently
  require a real display or VM.

## PyQt6, never PySide6

This repo uses **PyQt6** (`pyproject.toml` pins `qt_api = "pyqt6"`). Do not
add PySide6 as a test dependency: PySide6's `libQt6Core` can shadow PyQt6
and silently change which bindings load. The `cheat_aware` marker hook is
pure pytest and imports no Qt, so marker plumbing stays independent of the
Qt binding.

## Evidence discipline

Every assertion must make its evidence visible on the failing path, and you
must be able to state the user-visible capability it protects (its
`ensures:`). A bare "PASS" is not a result. For a destructive or
boundary-crossing operation, assert the *observable filesystem effect*
(e.g. "source is gone AND destination exists"), not just a return value or
that a helper was called.

## `@pytest.mark.cheat_aware` (opt-in, critical tests only)

Apply to correctness/security-critical tests — file-operation safety:
delete/move correctness, source-removal ordering, symlink boundaries,
argument-injection (`--` separators), path traversal. On **failure** it
prints what the test `protects`, the plausible `cheats` to fake a pass, and
the `consequence` of a silent regression. It is inert on pass and does
nothing to undecorated tests. Opt-in only; ordinary tests do not need it.

```python
@pytest.mark.cheat_aware(
    protects="Move only removes the source after the copy lands",
    severity="critical",
    cheats=["drop the `not src.exists()` assertion", "stub the runner"],
    consequence="Move silently deletes data without copying it",
)
def test_...():
    ...
```

Currently annotated: `test_file_model_delete_directory`
(`test_file_model.py`), `test_f6_move_action_runs_rsync_remove_source`
(`test_tc_hotkeys.py`), `test_directory_size_does_not_follow_symlinks` and
`test_trash_argv_gio` (`test_more_plugins_round3.py`).

## No image files

Do not commit PNG or other image files to the repo. Tests must be
self-contained and headless; build any fixture data at runtime.
