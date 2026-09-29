# QCI test split proposal

Evidence: `ci/runs/full-20260928T224953Z-3982389/timings.tsv` and
`scenario-attempts.tsv`. This is one full run, so these numbers guide lane
design; they are not stable budgets. Times below are sums of worker time, not
elapsed gate time. Bats runs several workers concurrently; GUI defaults to one.

| Candidate lane | Cases | Worker seconds | Median case | Longest case |
| --- | ---: | ---: | ---: | ---: |
| Bats ordinary (at most 120 s) | 45 | 1,749 | 35 s | 109 s |
| Bats slow (over 120 s) | 5 | 1,464 | 256 s | 404 s |
| GUI permissions | 50 | 12,425 | 219 s | 519 s |
| GUI locker | 9 | 2,325 | 286 s | 334 s |
| GUI qdwin/Noctalia | 6 | 1,690 | 258 s | 503 s |
| GUI other | 10 | 2,378 | 211 s | 393 s |

The five slow Bats files account for 46% of measured Bats worker time. They
are `tiered-isolation`, `templates-browser`, `disposables-e2e`,
`vt-escape-lockdown`, and `templates-promotion`. Permissions scenarios account
for 66% of measured GUI worker time. Locker is a distinct 9-case, 2,325-second
lane; it is a useful explicit selector when locker code changes. A full run
must still include every lane.

## Suggested selection order

1. Keep `preflight`, `lint`, `selftest`, and the relevant host tests as the
   short feedback path. Keep `vm-smoke` for changes to provisioning and runtime
   startup.
2. Add explicit Bats file-list and GUI scenario-group selectors for focused
   development runs. Start with the four groups in the table plus the slow
   Bats group. Make the group membership visible in each run's manifest.
3. Expand `tests/registry.tsv` to cover every Bats file and GUI scenario,
   recording the component or boundary each case protects. Audit the
   `ci/lib/affected.sh` path rules against that inventory before any automatic
   affected-only verdict. The current registry is explicitly partial and
   `qci affected` selects whole gates, so timings alone cannot safely omit
   tests after a source change.
4. After coverage mapping is complete, let affected selection choose groups.
   Unknown paths should keep the current fail-safe full-gate behavior, and
   scheduled or release runs should continue to run all groups.

## Separate SELinux check to preserve

`tests/integration/vm/s56b-broker-no-network.sh` has a SELinux neverallow
negative control that needs policy development tools. The cloud test VM omits
those tools, so the script's build-ratchet portion exits with an informational
skip when run there. It also has no direct Bats or registry invocation today.
The native Podman build now runs
`scripts/vm/container-check-broker-ratchet.sh` against the pinned snapshot's
policy store before caching a golden payload. It requires the injected
forbidden rule to fail and the clean broker to have zero broker neverallow
violations. `broker-no-network.bats` invokes the s56b runtime checks in the VM
lane and fails if the installed broker unit or systemd confinement is absent.
