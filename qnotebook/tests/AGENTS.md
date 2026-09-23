# Agent instructions for writing qnotebook tests

Read this before you touch anything under `tests/`. qnotebook is a notes
app whose load-bearing property is **never lose or corrupt user note
content** (atomic writes + 3-way merge ladder + round-trip fidelity).

## Golden rule: never reduce coverage

New test work is **additive**. Do not delete a test, delete an assertion,
weaken a match (e.g. drop a byte-equality check down to just
`result.ok`), widen an accepted-status/rung comparison, add a module to an
audit allow-list instead of fixing it, raise a timeout to mask a failure,
or turn a real assertion into a `skip`. If an existing test looks wrong,
**flag it in your report and leave it** — a human decides whether the test
or the product is at fault. Silently "fixing" a test by making it pass is
a coverage regression, not a fix.

## Layout

- `tests/` — pytest, run from the repo root. qci runs the suite via
  `python3 -m pytest` (from repo root; `testpaths = ["tests"]` in
  `pyproject.toml`). This host can run them headless.
- `tests/conftest.py` — `qapp` (session `QApplication`), `tmp_notebook`
  (a small 4-page markdown notebook), a post-test Qt flush, and the
  `cheat_aware` marker (below).
- `tests/fixtures/` — sample notebooks/vaults used by data-safety tests
  (e.g. `obsidian_vault/` for preservation checks). Text only.
- Data-safety core lives in `qnotebook/safe_save.py`; the highest-value
  tests are `test_atomic_write.py`, `test_no_direct_file_writes.py`,
  `test_strict_preserve.py`, `test_safe_save_merge_ladder.py`,
  `test_safe_save_*` and `test_*preserve*` / `test_obsidian_*`.

## PyQt6, never PySide6

Use **PyQt6** only. `pyproject.toml` pins `qt_api = "pyqt6"` and tests are
headless via `QT_QPA_PLATFORM=offscreen` (set in `conftest.py`). Do not
add PySide6 as a test dependency: its `libQt6Core` can shadow PyQt6 and
silently change behavior. After each test the conftest runs
`processEvents + gc.collect` to release C++ objects.

## Evidence discipline

Every assertion must make its evidence visible on the failing path, and
you must be able to state what user-visible capability it `ensures:`
(e.g. "a disjoint concurrent edit is never silently dropped"). Prefer
byte-level assertions (`p.read_bytes() == expected`) for save paths over a
bare `result.ok` — a green that doesn't check the bytes is not earned. A
PASS that cites no evidence is not a result; a FAIL must show expected vs
actual, not just "did not match".

## `@pytest.mark.cheat_aware` (opt-in, data-safety-critical)

`tests/conftest.py` registers an opt-in marker. On **failure** it prints
structured context — what the assertion `protects`, the plausible `cheats`
someone might use to fake a pass, and the `consequence` of a silent
regression — so the next agent sees the stakes before touching it. The
marker and its report hook are **pure pytest (no Qt import)**. It is
inert on pass and opt-in: ordinary tests do not need it. Apply it to
high-risk data-loss assertions only (atomic write, the merge ladder,
strict-preserve, the no-direct-write audit, vault preservation).

```python
@pytest.mark.cheat_aware(
    protects="a concurrent external edit is never silently dropped",
    severity="critical",
    cheats=["assert only result.ok and drop the byte check",
            "widen accepted status to (ok, conflict)"],
    consequence="user loses note content written by another process",
)
def test_...():
    ...
```

Currently annotated: `test_atomic_write.py::test_reader_never_sees_truncated_content`,
`test_no_direct_file_writes.py::test_no_direct_write_outside_exempt`,
`test_strict_preserve.py::test_strict_preserve_default_restores`,
`test_safe_save_merge_ladder.py::test_disjoint_hunks_fast_path`.

## Constraints

- **No PNG or other image files committed for tests.** Image-handling
  paths are exercised with in-memory `QImage`/bytes or generated at
  runtime; do not bake a golden image into the repo. Fixtures are text
  (`.md`).
- Optional-tool tests (`git` / `wiggle` / `mergiraf` / `enchant` /
  matplotlib) gate with `pytest.mark.skipif(not HAS_*, ...)`. That is a
  genuine not-applicable skip, not a way to dodge a red assertion — never
  convert a real data-safety assertion into a skip.
