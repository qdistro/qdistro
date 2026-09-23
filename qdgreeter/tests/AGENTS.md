# Agent instructions for writing qdgreeter tests

You are adding or changing tests in qdgreeter — the qdistro boot greeter
(graphical login / session start, a replacement for tuigreet). Read this
before touching anything under `tests/`.

## Golden rule: never reduce coverage

New test work is **strictly additive**. Do not delete a test, delete an
assertion, weaken a match, widen a comparison, raise a timeout to mask a
failure, or turn a `pytest.raises`/hard failure into a `skip`. If an
existing test looks wrong, **flag it in your report and leave it** — a
human decides whether the test or the product is at fault. Silently
"fixing" a test by making it pass is a coverage regression, not a fix.

This repo guards login: the highest-stakes invariant is that **a session
cannot start without a valid authentication exchange**, and the
**password never leaks to a non-secret prompt or a log**. Be especially
careful not to weaken anything touching auth, `start_session`, PAM
auth_message handling, or password serialization.

## Layout

- `tests/unit/` — pure-Python pytest. No real `$GREETD_SOCK`, no display.
  - `test_qdgreeter_protocol.py` — greetd JSON-IPC wire format and a
    round-trip against an in-process fake greetd over a real UNIX socket.
  - `test_qdgreeter_session.py` — the `GreetController` auth flow driven
    against a scripted fake greetd client.
- `tests/conftest.py` — test-root conftest: registers the `cheat_aware`
  marker and the failure-reporting hook (see below). **Pure pytest — it
  must not import Qt.**

**qci runs this repo's tests as `python3 -m pytest tests` from the repo
root.** Keep that invocation green.

## Qt binding: PySide6 vs PyQt6 — handle with care

This repo ships in environments where **PySide6** is present, and
pytest-qt will auto-select PySide6 first if it can, loading PySide6's
bundled `libQt6Core.so`. That then breaks a subsequent PyQt6 import with
a missing `Qt_*_PRIVATE_API` symbol error. `pyproject.toml` pins
`[tool.pytest.ini_options] qt_api = "pyqt6"` to keep the selection
deterministic. The Qt-using tests additionally `pytest.importorskip(...)`
their binding and set `QT_QPA_PLATFORM=offscreen` when headless.

- **Do not import any Qt binding from `tests/conftest.py`.** The marker
  hook there is pure pytest by design; importing Qt at collection time
  would defeat the per-test `importorskip` guards and could re-trigger
  the binding-shadowing crash.
- Do not change `qt_api` or remove the `importorskip` guards to make a
  Qt test run somewhere it currently skips — flag the environment gap
  instead.
- Tests that need a real display/VM: run with `--collect-only` to verify
  collection, and flag them in your report rather than reporting a pass
  you could not observe.

## `@pytest.mark.cheat_aware` (opt-in, security-critical)

`tests/conftest.py` provides an opt-in
`@pytest.mark.cheat_aware(...)` marker (propagated from
qdistro's `tests/unit/conftest.py`). It is **inert on pass**; on
**failure** it prints structured context — what the assertion `protects`,
the plausible `cheats` someone might use to fake a pass, and the
`consequence` of a silent regression — so the next agent sees the stakes
before touching it. All kwargs are optional and the block degrades
gracefully if some are missing.

Apply it to high-risk auth assertions only (session-start gating,
secret-vs-visible prompt handling, password-in-log guards). It is opt-in,
not blanket; ordinary wire-format tests do not need it.

```python
@pytest.mark.cheat_aware(
    protects="a session cannot start without a successful auth exchange",
    severity="critical",
    cheats=["accept auth_error as success", "skip the start_session gate"],
    consequence="an unauthenticated user lands in the admin session",
)
def test_...():
    ...
```

## Evidence discipline

Every assertion must make its evidence visible on the failing path, and
you must be able to state what user-visible capability it `ensures:`
(e.g. "a wrong password never reaches `start_session`"). Pass cites
evidence; Fail shows both expected and actual; Skip(reason) only for
genuinely-not-applicable cases — a missing dependency is a loud failure,
not a silent skip. If you cannot state what an assertion ensures, you do
not yet understand what you are protecting — find out before you touch
it.

## Constraints

- **Do not commit PNG or other image files to the repo.** Any screenshots
  taken during display/VM verification are runtime artifacts; they are
  never committed.
