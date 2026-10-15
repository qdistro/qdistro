# tests/integration/permissions-gui

User-authored GUI acceptance scenarios for qdistro . Each
`NN-*.md` file describes setup, steps, and pixel-level assertions in
prose. A graphic-aware subagent executes them against a running VM
following the instructions in `AGENTS.md`.

## Lane

Every scenario runs on the product session (qdwin compositor + qdshell on
`wayland-1`; see `AGENTS.md`, "qdwin lane"): the admin app started through
the shipped native-Wayland launcher, the admin TUI as installed, hosted in
`foot` (a test client of every GUI golden; the image ships qterminator).
The labwc/LXQt/XWayland admin lane was retired on 2026-10-14; its scenarios
were migrated or merged (01 absorbed 02 and 05, 40 absorbed 09, 12 absorbed
16, workflow-gui/04 absorbed workflow-gui 01–03) and 55 became the headless
`tests/integration/vm/qsu-enforcing.bats`.

## Scenario index by area

Numbering is roughly chronological; each scenario stands on its own.
Gaps are scenarios whose hard checks were D-Bus replies, sqlite rows or
journal lines only: since 2026-10-02 they run headless (no agent, no
screenshots) in the bats gate — 07, 11, 15, 17, 23–33, 36–39,
40-clipboard, 41, 42, 58, 59 in `tests/integration/vm/permissions-headless.bats`
(one `pgNN-*` @test each), and the qsu ones 44–46, 49–54 in
`tests/integration/vm/s58-qsu-real-flow.sh` (run by `tiered-isolation.bats`).

- **01–10** — admin app + TUI smoke (01: TUI render, help overlay, scope
  picker and deny in foot; Qt visual / approve / mouse / restart-resilience
  / cache revoke and its `ApprovalRevoked` signal, formerly 22).
- **12–14** — cross-user `RelayMessage` flow, visual approve / deny /
  forbidden scope; 12 also delivers to two real qnotebook receivers.
- **18** — pod-apps launcher badge.
- **19–21** — tier-5 loopback / cold-start / close-cleanup.
- **34** — admin-app navigation across multiple pending requests.
- **35** — TUI + Qt admin app concurrent subscribers stay in sync.
- **40-tui** — TUI survives a broker restart, shows the broker offline,
  recovers.
- **43, 47** — `qsu` admin UX: 43 (prompt + scope radios rendered), 47
  (delegated `forever_exe` rejected with a `ScopeNotPermitted` modal). qsu
  end-to-end under SELinux Enforcing (formerly 55) is
  `tests/integration/vm/qsu-enforcing.bats` (`# qci:enforcing`).
- **56–57** — tier-4 RDP transport visual acceptance: single guest
  window visible over FreeRDP/vsock and close-cleanup of the RDP path.

## Running

Dispatch a subagent (Explore or general-purpose) and point it at
`AGENTS.md` plus the scenario of interest:

```
Read tests/integration/permissions-gui/AGENTS.md, then run
tests/integration/permissions-gui/01-tui-approver-visual.md against VM
<vm>. Return the report in the required format.
```

The subagent drives the VM via `scripts/vm/vm-exec`, the qdwin helpers and
`vm-gui` click preview/confirm, takes screenshots with `qdwin_screenshot`,
reads them as images, and returns PASS/FAIL per assertion. Under qci,
`ci/bin/qci gui --scenario <abs path>` does all of this.

## Why scenarios live here, not in `tests/`

`tests/unit/` is pytest — code-only, mocked broker, fast. These
scenarios need a real VM, a real compositor, and pixel output; they
are authored as prose so non-programmers can write them and the
set that matters for "does this look right" stays legible. They are
the spec for the graphic-aware agent, not a replacement for pytest.
