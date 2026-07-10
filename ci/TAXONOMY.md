# qci test taxonomy

This is the canonical, cross-repo vocabulary for *what kind of evidence a
test row carries*. The qdistro umbrella runs suites of very different
confidence under one `host`/`gui`/`bats` gate — a qdwin "meson unit test"
that only pattern-matches source text is **not** the same strength of
evidence as a qterminator test against a Python fake, which in turn is not
the same as a VM bats test exercising the real service. Today the gate
reports pass/fail uniformly. This taxonomy makes that difference *visible*
in the report without changing any verdict.

**This is reporting only. It adds no coverage floor, no pass-rate gate, and
no per-category threshold.** A category is metadata on a result row; it
never changes whether the gate passes.

## Category vocabulary

| category | meaning | typical evidence strength |
| --- | --- | --- |
| `unit` | Pure in-process test of one component; no real daemon, socket, VM, or display. | medium — logic is exercised, but against the unit's own boundary. |
| `integration` | More than one component together, real subprocesses/sockets, still host-side (no VM). | high — real wiring, real IPC. |
| `gui` | Driven through a real display / compositor / visual agent (screenshots, vision pytest, GUI scenarios). | high but environment-sensitive. |
| `vm` | Runs against a booted VM (bats `vm_run`, vm-smoke, snapshot-daily). | highest — the real OS image and services. |
| `slow` | An orthogonal *cost* tag, not a confidence band: integration-grade runtime/setup. May co-apply with `unit`/`integration`. | n/a (cost marker). |
| `source_invariant` | Asserts a property of **source text** (a pattern/AST/grep invariant), not runtime behaviour. The code is never executed. | LOW — proves the text, not the behaviour. Must be labelled honestly so it is never mistaken for a behavioural test. |
| `fake_backend` | Exercises real product code against a **fake/stub** of its backend (in-repo Python fake, stub socket, mock D-Bus). | medium — product logic runs, but the backend is simulated. |
| `real_backend` | Exercises product code against the **real** backend it talks to in production (real D-Bus broker, real ssh-agent, real udisks). | high — closest to production. |

`slow` is a cost tag and may combine with a confidence category (e.g. a
`needs_ssh` real-backend relay test is `integration` + `slow`).

## How each layer tags a row

| layer | mechanism | example |
| --- | --- | --- |
| **pytest** (qdistro, qdbrowser, qdlocker, …) | `@pytest.mark.<category>` registered in that repo's `pyproject.toml` / `conftest.py`. | `@pytest.mark.slow`, `@pytest.mark.needs_ssh`, `@pytest.mark.integration` |
| **meson** (qdwin, qdshell C/QML) | meson test `suite :` tag. | `test(..., suite : 'source_invariant')` |
| **vitest / npm** (WebExtensions) | a tag in the test name or a `describe`/project tag the runner can grep. | `describe('[integration] …')` |
| **bats** (VM integration) | bats file lives under `tests/integration/vm/` → `vm` category by gate. | (implicit by gate) |
| **qci results.tsv** | the additive 9th `category` column (see below). | `record_result host qdistro-pytest pass 0 pass pytest … unit` |

qdistro's pytest markers are registered in `qdistro/pyproject.toml`
(`[tool.pytest.ini_options].markers`) and `tests/unit/conftest.py`. Run the
fast host subset with:

```bash
python3 -m pytest -m "not slow and not needs_ssh"
```

## qci results.tsv `category` column

`qci` writes a 9th column, `category`, to every `results.tsv` row. It is
**additive and backward-compatible**: a report generated from an older
`results.tsv` (8 columns) simply renders with an empty category and skips
the category section.

The category is derived by `category_for()` in `ci/lib/run.sh` from a row's
gate + kind (a coarse, honest default: `bats`→`integration`, vm kinds→`vm`,
gui/vision/image→`gui`, everything host-side→`unit`). A caller may pass an
explicit 9th argument to `record_result` to override when a suite's true
confidence differs from its gate default — e.g. a `source_invariant` or
`fake_backend` suite.

`report.md` / `report.html` gain a **Test categories** section: a
per-category tally of total / pass / fail / blocked / skip, plus an explicit
**Dependency-missing skips** callout (a skip whose notes look like a missing
dependency is a bake/host regression, never an expected skip). `summary.json`
gains a machine-readable `categories` map and `dependency_missing_skips`
list.

## Per-suite labelling map (current state + action items)

The coarse `category_for()` default classifies *gates*, not the true
confidence of each *suite*. The honest per-suite picture, and what still
needs labelling at the source, is:

| suite | runner | today's category | true category | action |
| --- | --- | --- | --- | --- |
| qdistro `tests/unit` | pytest | `unit` | `unit` (+ `slow`/`needs_ssh` on the relay tests) | DONE — markers registered + applied. |
| qdistro `tests/integration/vm` | bats | `integration`/`vm` | `vm` / `real_backend` | OK by gate. |
| **qdwin meson "unit tests"** | meson | `unit` (by host gate) | **`source_invariant`** — these are source-text pattern matches, the code is not executed | **ACTION (separate repo):** retag these meson tests with `suite : 'source_invariant'` in qdwin's `meson.build`. qdwin source is NOT in this worktree, so this cannot be edited from here — it must be done in the qdwin repo. Until then the host report over-states their strength. |
| qterminator pytest | pytest | `unit` | `fake_backend` — runs product code against a Python fake terminal | ACTION (separate repo): mark these `@pytest.mark.fake_backend` in qterminator. |
| qdshell UI vision pytest | pytest/vision | `gui` | `gui` | OK. |
| WebExtension npm tests | npm/vitest | `unit` | mixed `unit`/`integration` | ACTION (separate repos): tag integration specs `[integration]` in the test name. |

### qdwin relabel (item 3) — required action, do NOT edit from here

qdwin's static-source meson tests must be relabelled `source_invariant`
(honesty fix, no behavioural refactor). Because qdwin lives in a **separate
repo** that is not part of this worktree, the relabel is recorded here as an
action item rather than applied:

> In `qdwin/.../meson.build`, change the `suite :` argument of every meson
> test that only greps/parses source text (no compiled binary is run) from
> its current suite to `suite : 'source_invariant'`. This is a pure
> metadata change — it renames how the row is reported, not what it checks.

## Flake / retry notes (item 4 — convention)

Classified GUI retry is available through `QCI_GUI_RETRY=1`. It retries exactly
once on a fresh VM and only for the narrow infrastructure/tooling classifiers
listed in `ci/README.md`; product failures, missing verdicts, and agent timeouts
are not retried. Every attempted retry is recorded in `flake.tsv`, and a
retried pass carries the attempt count in its result notes, so retry cannot
silently turn a flake green. Manual reruns should still put `flake: <why>` or
`retried=N` in their notes when the relationship is known.

## Deferred P3 actions (out of scope for task 06)

The following P3 items were explicitly deferred; they are recorded here so
they are not lost:

- **P3 action 3 — shared ruff blocking (completed)**: `gate_host()` uses the
  shared `ci/ruff-shared.toml` profile and treats findings as a host-gate
  failure. A missing `ruff` executable remains an explicit skip.

- **P3 action 4 — refactor pytest loop**: Replace the hand-rolled batched
  `for` loop in `gate_host()` with `pytest-xdist` or a cleaner dispatch.
  Low risk, pure refactor; deferred to avoid churn during coverage rollout.

- **P3 action 5 — self-hosted runner**: Provision and register a self-hosted
  GitHub Actions runner for the qdistro suite so VM-gated tests can run in CI
  without manual `qci run` invocations. Requires infra work outside this repo.
