# tests/integration/permissions-gui

User-authored GUI acceptance scenarios for qdistro . Each
`NN-*.md` file describes setup, steps, and pixel-level assertions in
prose. A graphic-aware subagent executes them against a running VM
following the instructions in `AGENTS.md`.

## Running

Dispatch a subagent (Explore or general-purpose) and point it at
`AGENTS.md` plus the scenario of interest:

```
Read tests/integration/permissions-gui/AGENTS.md, then run
tests/integration/permissions-gui/01-tui-approver-visual.md against VM
qdistro-dev-260421-0052. Return the report in the required format.
```

The subagent drives the VM via `scripts/vm/vm-exec` and
`vm-gui`, takes screenshots with `virsh screenshot`, reads them as
images, and returns PASS/FAIL per assertion.

## Why scenarios live here, not in `tests/`

`tests/unit/` is pytest — code-only, mocked broker, fast. These
scenarios need a real VM, a real compositor, and pixel output; they
are authored as prose so non-programmers can write them and the
set that matters for "does this look right" stays legible. They are
the spec for the graphic-aware agent, not a replacement for pytest.
