# qdistro local CI

`qdistro/ci` is the handoff folder for local continuous integration across this
umbrella checkout. It is meant to be usable by a developer or by an agent with
shell access to the same workspace.

The preferred entrypoint is `just` (run from `qdistro/ci/`):

```bash
just full            # or: just bats --file <f.bats>, just triage --latest
just --list          # all recipes
```

Every recipe delegates to the stable CLI, which is also usable directly (and is
what scripts/agents/run-artifacts invoke):

```bash
qdistro/ci/bin/qci <gate>
```

`just` is only the task surface; the CI engine (run lifecycle, VM/golden
management, parallel pools, report finalization, exit-class accumulation) lives
in bash modules under `qdistro/ci/lib/` that `bin/qci` sources into one process.
See [`AGENTS.md`](AGENTS.md) for the module map.

Every invocation writes artifacts under:

```text
qdistro/ci/runs/<gate>-<utc>-<pid>/
  manifest.txt
  repo-state.tsv
  results.tsv
  report.md
  report.html
  host/
  vm/
  bats/
  gui/
  screenshots/
  journals/
  agent-notes/
```

`report.md` and `report.html` are the primary CI result. They include failing
commands, links to logs/artifacts, excerpts, and first-pass fix recommendations.
For machine parsing, use `results.tsv`, `repo-state.tsv`, and `manifest.txt`.

`results.tsv` carries a 9th `category` column placing each row in the shared
**confidence taxonomy** (`unit`/`integration`/`gui`/`vm`/`source_invariant`/
`fake_backend`/`real_backend`/`slow`). The report's *Test categories* section
tallies results per category and calls out dependency-missing skips. This is
reporting only — it gates nothing. See [`TAXONOMY.md`](TAXONOMY.md) for the
canonical vocabulary, per-layer tagging (pytest markers / meson suites /
vitest tags), and the per-suite relabel action items.

## Gates

| Gate | Purpose |
| --- | --- |
| `preflight` | Verify sibling repos, libvirt session, VM tools, prebaked image, and common host tools. |
| `selftest` | Self-test the qci runner itself (no VM): run the host-only `tests/integration/qci/*.bats` suite that locks down the gate-runner contract — exit-class table, usage/unknown dispatch, headless gate manifest/results.tsv, and the affected/replay/offline plumbing. Runs first in `host`. |
| `host` | Run host tests/builds across all sibling projects: Python pytest repos, WebExtension npm tests/builds, and qdwin/qdshell meson/QML checks. (The qdistro-site website is NOT built here — it ships via a separate website pipeline.) |
| `vm-smoke` | Create or reuse a VM and verify the qdwin/qdshell session, Wayland socket, and core user services. |
| `bats` | Run every `qdistro/tests/integration/vm/*.bats` file. Each file gets a fresh disposable VM, and files run **in parallel** (see [Parallelism & per-run golden](#parallelism--per-run-golden-image)). |
| `gui` | Run executable qdwin GUI smokes, qdshell vision pytest when configured, and markdown scenario assignments for qdwin, qdlocker, qdistro permissions GUI, and qdwin-noctalia. Normal disposable runs provision both GUI profiles: admin/non-qdwin scenarios use the admin compositor harness, while qdwin-dependent rows use `QDISTRO_VM_GUI_SESSION=qdwin`. The agent scenarios run **in parallel** (one disposable VM each). |
| `gui-admin` | Run the GUI gate in admin/non-qdwin mode: qdwin/qdshell, qdlocker, qdwin-noctalia, and tier-4/5 scenarios are recorded as intentional skips while qdistro admin/broker GUI scenarios still run. |
| `full` | Run `preflight`, `host`, `release-manifest`, `bootstrap-release-profile`, `vm-smoke`, `bats`, and `gui`. |
| `snapshot-daily` | Build a `qdistro-daily-YYYY-MM-DD` VM from current source state. |
| `cleanup` | Remove stale `qci-*` disposable VMs/overlays. Never touches `qdistro-daily*`. |

## Typical run

```bash
qdistro/ci/bin/qci full
```

A full run takes a long time. A bare `qci` is bound to its terminal, so a closed
terminal or dropped SSH session kills the run mid-step (SIGHUP). Two safeguards:

```bash
# Preferred: run detached in a timestamped tmux session that survives disconnects.
qdistro/ci/bin/qci-tmux full
#   session: qci-full-<UTCstamp>   log: /tmp/qci-full-<UTCstamp>.log
#   attach:  tmux attach -t qci-full-<UTCstamp>
```

`qci` itself also traps `SIGHUP` now, so even a direct run finalizes the report
and releases VMs on a terminal hangup instead of leaving a half-finished run.

Successful disposable `qci-*` VMs are destroyed. Failed disposable VMs are
preserved by default for debugging and recorded in the report. To delete failed
VMs as well:

```bash
QCI_DELETE_FAILED_VM=1 qdistro/ci/bin/qci full
```

Create the daily snapshot VM requested by the CI policy:

```bash
qdistro/ci/bin/qci snapshot-daily
# default VM name: qdistro-daily-$(date -u +%F)
```

## Parallelism & per-run golden image

The `bats` and `gui` gates run their disposable VMs **concurrently** in a bounded
pool. Each VM is ~4 GiB RAM + `QDWIN_VM_VCPUS` (default 4) vCPUs, so RAM is the
binding constraint; CPU is intentionally overprovisioned.

- **bats concurrency** auto-selects a tier from host RAM + logical CPUs:
  minimal `≤32 GiB → 4`, medium `≥56 GiB & ≥10 cores → 10`, high
  `≥90 GiB & ≥12 cores → 16`. The value is clamped by **current** `MemAvailable`
  (`(avail−6)/5`, ~5 GiB/VM) so a busy host can't be overcommitted.
- **gui concurrency** defaults to **8** (heavier VMs + one agent process each),
  same RAM clamp.

**Per-run golden image:** the expensive part of provisioning is building
qdwin/qdshell from current source (`fresh-vm-bootstrap.sh`, ~150–310 s per VM).
Instead of paying that on every VM, the `bats` gate builds the compositor **once
per run** into a golden qcow2 (`qci-golden-bats-*.qcow2`), then every worker
clones that golden and **skips the build** (per-VM provisioning drops to ~10 s).
The golden is built from *current* source each run (still fresh), cleaned up at
run end, and preserved only if a failed worker that references it is preserved.
(GUI golden is planned — see `ci/ci-hardening-todo.md`.)

### Environment knobs

| Variable | Default | Effect |
| --- | --- | --- |
| `QCI_JOBS` | auto-tier | Override bats pool concurrency (still RAM-clamped). |
| `QCI_GUI_JOBS` | 8 | Override gui pool concurrency (still RAM-clamped). |
| `QCI_GUI_SKIP_QDWIN` | 0 | `1` runs only the admin/non-qdwin GUI lane for `qci gui`; `qci gui-admin` sets this automatically. |
| `QCI_NO_GOLDEN` | 0 | `1` disables the per-run golden; every worker runs the full bootstrap. |
| `QDWIN_VM_VCPUS` | 4 | vCPUs per disposable VM. |
| `QCI_DELETE_FAILED_VM` | 0 | `1` deletes failed VMs instead of preserving them. |
| `QDISTRO_VM_EXEC_TIMEOUT` | 1800 | Overall deadline (s) for a single `vm-exec` in-guest command; on timeout the guest process tree is killed and `vm-exec` exits 124. `0` = unbounded. |
| `QDISTRO_VM_AGENT_RPC_TIMEOUT` | 30 | Host-side cap (s) per `virsh qemu-agent-command` RPC, so a wedged agent call can't outlast the deadline above. `0` = unbounded. |
| `QD_VM_START_MAX_WAIT` | 300 | Backstop cap (s) on guest-agent readiness in `vm-start-and-wait` (raised from 120 for parallel boot contention). |

A per-task timing breakdown (provision vs work seconds per file/scenario) is
written to `<run-dir>/timings.tsv` for spotting outliers.

## Agent-assisted GUI scenarios

Markdown GUI scenarios need a visual runner. `qci gui` always creates prompt
files under `agent-notes/`. If `QCI_AGENT_CMD` is unset, those scenarios are
marked `blocked` so the report cannot be mistaken for a green run.

`QCI_AGENT_CMD` receives the prompt path as its first argument:

```bash
QCI_AGENT_CMD='my-visual-agent-runner' qdistro/ci/bin/qci gui
```

If your runner needs a template, include `{prompt}` — it is substituted with the
prompt-file path and the result is run via `bash -lc` (so `$(cat {prompt})`
inlines the prompt text, which `claude -p` takes as a positional argument rather
than a path):

```bash
QCI_AGENT_CMD='claude -p "$(cat {prompt})" --dangerously-skip-permissions --model haiku' \
  qdistro/ci/bin/qci gui
```

`--dangerously-skip-permissions` is required because the agent must run
`vm-exec`/`virsh` and write its `status.txt` without interactive approval;
`--model haiku` keeps the per-scenario cost down (the scenarios are mechanical
drive-and-screenshot tasks, run one disposable VM each).

The executable qdwin smokes run before the markdown assignments:

- `qdwin/tests/gui/agent-mvp-session-smoke.sh`
- `qdwin/tests/gui/agent-protocol-audit.sh`
- `qdwin/tests/gui/agent-cursor-clickthrough-smoke.sh`
- `qdwin/tests/gui/agent-click-smoke.sh`

The qdshell UI vision pytest is also wired into `gui`; it uses the same local
Codex-backed settings as the rest of the GUI gate.

Prefer a fresh disposable VM for normal GUI CI. Reusing a VM preserved from a
failed smoke run is useful for debugging, but it can carry service restart
loops or stale Wayland socket conflicts into the GUI gate.

## VM policy

- CI-created VMs are named `qci-*`.
- `qdistro-daily` and `qdistro-daily-*` are protected and refused as explicit
  test targets unless `QCI_FORCE_PROTECTED_VM=1` is set.
- Passing CI-created VMs are deleted.
- Failed CI-created VMs are kept by default for triage.
- `qci cleanup` only considers `qci-*` names and has a dry-run mode:

```bash
qdistro/ci/bin/qci cleanup --dry-run --age-hours 24
```

Cleanup asks libvirt for each VM's real disk path and skips domains whose disk
cannot be located. It does not guess paths outside libvirt metadata.

## Host test dependencies

The `host` gate runs tests across all sibling projects. Several projects need
dependencies that are not part of the base qdistro install. Check or install
them in one shot (preflight also flags missing ones as WARN at the start of a
run):

```bash
qdistro/ci/bin/qci-host-deps            # report what is missing (no sudo)
qdistro/ci/bin/qci-host-deps --install  # install via zypper/apt/dnf + pip
```

The individual deps are:

**qdbrowser tests** require `jeepney` (D-Bus bridge client, already a
runtime dependency in `qdbrowser/pyproject.toml`):

```bash
# Ubuntu
sudo apt install python3-jeepney

# openSUSE Tumbleweed
sudo zypper install python3-jeepney
```

**qdshell QML tests** require the `QtQml.WorkerScript` QML module:

```bash
# Ubuntu
sudo apt install qml6-module-qtqml-workerscript

# openSUSE Tumbleweed
sudo zypper install qt6-declarative-imports
```

**qterminator tests** require the `QTermWidget` Python binding, which is
built from source as part of the qterminator install (it is not packaged
by any distro). Build it from the `qtermwidget-pyqt/` directory in the
qterminator repo:

```bash
cd qterminator/qtermwidget-pyqt && pip install .
```

See `qterminator/README.md` for full build prerequisites (qtermwidget-devel,
sip, pyqt-builder).

**qfileman tests** require `tomli_w` (declared in `qfileman/pyproject.toml`; not
packaged by most distros):

```bash
pip install tomli_w
```

**qdwin vendored-libweston symbols test** requires the `libevdev` and `pango`
(incl. `pangocairo`) development packages:

```bash
# openSUSE Tumbleweed
sudo zypper install libevdev-devel pango-devel

# Ubuntu
sudo apt install libevdev-dev libpango1.0-dev
```

## Fast triage

```bash
qdistro/ci/bin/qci list-runs
qdistro/ci/bin/qci triage --latest
qdistro/ci/bin/qci report --latest
```

Start from the report, then inspect the linked logs, journals, screenshots, and
the preserved VM name if the failure kept one alive.
