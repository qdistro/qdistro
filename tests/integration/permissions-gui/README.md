# tests/integration/permissions-gui

User-authored GUI acceptance scenarios for qdistro . Each
`NN-*.md` file describes setup, steps, and pixel-level assertions in
prose. A graphic-aware subagent executes them against a running VM
following the instructions in `AGENTS.md`.

## Scenario index by area

Numbering is roughly chronological; each scenario stands on its own.

- **01–10** — admin app + TUI smoke (visual / scope picker / approve /
  deny / mouse / CLI round-trip / restart-resilience / cache revoke).
- **11–17** — cross-user `RelayMessage` flow (headless + visual,
  approve / deny / forbidden scope, realapp variants).
- **18** — pod-apps launcher badge.
- **19–21** — tier-5 loopback / cold-start / close-cleanup.
- **22–23** — `ApprovalRevoked` and `RevokeAllForUid` signal contracts.
- **24–28** — declarative rules: allow-short-circuit, deny-short-circuit,
  inotify hot-reload, first-match-wins ordering, exe glob match.
- **29** — `CheckPermission` `"unknown"` fast-path (no prompt, no audit).
- **30** — rate-limit raises `.RateLimited`.
- **31** — fire-and-forget `RequestPermission` (no waiter).
- **32** — `forever_exe` cache scope grants only the approved exe.
- **33** — `RunCacheGc` deletes expired rows (and lookup filters them).
- **34** — admin-app navigation across multiple pending requests.
- **35** — TUI + Qt admin app concurrent subscribers stay in sync.
- **36–37** — clipboard transfer policy: same-silo allow, cross-silo
  default-deny + rule allow.
- **38** — `ListRules` surface for tooling.
- **39** — `SaveRule` validation rejects bad filename / YAML / shape.
- **40–41** — clipboard *receive* gate: same-silo short-circuit (40),
  cross-silo default-deny + rule allow + MIME glob (`text/*`) + rate-
  limit (41).
- **42** — `CheckHandoffActivation`: same-silo allow, cross-silo
  default-deny + per-`app_id` rule allow + non-admin bus-policy deny.
- **43–55** — `qsu` admin-UX + audit + security invariants. 43
  (prompt + scope radios rendered), 44 (`forever_argv` isolation),
  45 (`forever_basename` cross-binary), 46 (`forever_prefix`
  trailing args), 47 (delegated `forever_exe` rejected with
  `ScopeNotPermitted`), 48 (TUI argv on its own line, not 30
  `argv[NN]=` rows), 49 (`ListHistory` argv shape is lossless
  `as`), 50 (rule `argv_prefix:` pre-approves with `source=rule`
  + `rule_path`), 51 (`qsu -u target` ⇒ action key
  `qsu.exec:<target>`), 52 (invalid target_user rejected at
  qdistro-root-exec, never reaches broker), 53 (per-uid in-flight
  cap at 4), 54 (sanitized env strips LD_* / PYTHONPATH), 55
  (qsu end-to-end under SELinux Enforcing — zero new AVCs;
  requires SSH transport, qga cannot setenforce).
- **56–57** — tier-4 RDP transport visual acceptance: single guest
  window visible over FreeRDP/vsock and close-cleanup of the RDP path.
- **58** — permission lineage (findings P0-1): a forged `sandbox_engine`
  matches a tier-1 rule in shadow mode but is denied under
  `lineage_enforce=true` unless the caller has a `RegisterLaunch` record;
  `RegisterLaunch` is root-only.

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
