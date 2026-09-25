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
| `preflight` | Verify the in-tree component dirs (and warn about stale pre-monorepo sibling checkouts next to the repo), libvirt session, VM tools, prebaked image, and common host tools. |
| `lint` | Run warn-only shellcheck/scenario-structure metrics plus blocking Bats syntax and maintained-document local-link/anchor validation. `QCI_FLAKE_STRICT=1` also makes scenario flake findings fatal. |
| `selftest` | Self-test the qci runner itself (no VM): run the host-only `tests/integration/qci/*.bats` suite that locks down the gate-runner contract — exit-class table, usage/unknown dispatch, headless gate manifest/results.tsv, and the affected/replay/offline plumbing. Runs first in `host`. |
| `host` | Run host tests/builds across all in-tree components: Python pytest repos, WebExtension npm tests/builds, and qdwin/qdshell meson/QML checks. (The qdistro-site website is NOT built here — it ships via a separate website pipeline.) |
| `vm-smoke` | Create or reuse a VM and verify the qdwin/qdshell session, Wayland socket, and core user services. |
| `bats` | Run every `tests/integration/vm/*.bats` file (and each component's `<component>/tests/integration/vm/*.bats`). Each file gets a fresh disposable VM, and files run **in parallel** (see [Parallelism & per-run golden](#parallelism--per-run-golden-image)). |
| `gui` | Run executable qdwin GUI smokes, qdshell vision pytest when configured, and markdown scenario assignments for qdwin, qdlocker, qdistro permissions GUI, and qdwin-noctalia. Normal disposable runs provision both GUI profiles: admin/non-qdwin scenarios use the admin compositor harness, while qdwin-dependent rows use `QDISTRO_VM_GUI_SESSION=qdwin`. The agent scenarios run **in parallel** (one disposable VM each). |
| `gui-admin` | Run the GUI gate in admin/non-qdwin mode: qdwin/qdshell, qdlocker, qdwin-noctalia, and tier-4/5 scenarios are recorded as intentional skips while qdistro admin/broker GUI scenarios still run. |
| `full` | Run `preflight`, `host`, `release-manifest`, `bootstrap-release-profile`, `image`, `vm-smoke`, `bats`, and `gui`. |
| `snapshot-daily` | Build a `qdistro-daily-YYYY-MM-DD` VM from current source state. |
| `cleanup` | Remove stale `qci-*` disposable VMs/overlays. Never touches `qdistro-daily*`. |

`QCI_RELEASE=1` strengthens `full`: every `skip` or `blocked` row from a
release-relevant gate is fatal. Release evidence is green only when the
required VM, Bats, GUI, manifest, bootstrap-contract, and tester-image
rows actually ran.

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

## Image release identity

The image gate in `full` checks the image's five clean `SOURCE` commits
against `release-manifest/manifest.snapshot` before boot qualification. It
also checks the version and Tumbleweed snapshot against `image/config.xml`,
and the profile against `QDISTRO_PROFILE` (default `release`). A pinned dev
tester remains supported with `QDISTRO_PROFILE=dev`; dirty source stamps do
not qualify as release evidence. Standalone `QCI_RELEASE=1 qci image` captures
the configured release manifest for the same check. The run records the
expected manifest/profile, selected artifact digest and expected/observed
identities in `host/image-release-identity.log`.

Cached raw images are compared in full against fresh decompression before
reuse. Verification therefore needs room for another uncompressed image,
even when a cached raw already exists.

## Parallelism & per-run golden image

The `bats` and `gui` gates run their disposable VMs **concurrently** in a bounded
pool. Each VM is ~4 GiB RAM + `QDWIN_VM_VCPUS` (default 4) vCPUs, so RAM is the
binding constraint; CPU is intentionally overprovisioned.

- **bats concurrency** auto-selects a tier from host RAM + logical CPUs:
  minimal `≤32 GiB → 4`, medium `≥56 GiB & ≥10 cores → 10`, high
  `≥90 GiB & ≥12 cores → 16`. The value is clamped by **current** `MemAvailable`
  (`(avail−6)/5`, ~5 GiB/VM) so a busy host can't be overcommitted.
- **gui concurrency** defaults to **1** (serial). GUI VMs are heavier (nested KVM
  + compositor) and each spawns its own agent process; running many full GUI stacks
  at once has repeatedly produced black screenshots, missed input/focus events, and
  agent timeouts that do not reproduce in isolation. `QCI_GUI_JOBS` is an explicit
  opt-in for throughput experiments and is still RAM-clamped.

**Per-run golden image:** the expensive part of provisioning is building
qdwin/qdshell from current source (`fresh-vm-bootstrap.sh`, ~150–310 s per VM).
Instead of paying that on every VM, the `bats` gate builds the compositor **once
per run** into a golden qcow2 (`qci-golden-bats-*.qcow2`), then every worker
clones that golden and **skips the build** (per-VM provisioning drops to ~10 s).
The golden is built from *current* source each run (still fresh), cleaned up at
run end, and preserved only if a failed worker that references it is preserved.
The **gui** gate uses the same per-run-golden mechanism (admin + qdwin profiles;
`spin-test-vm-gui.sh`).

### Environment knobs

| Variable | Default | Effect |
| --- | --- | --- |
| `QCI_JOBS` | auto-tier | Override bats pool concurrency (still RAM-clamped). |
| `QCI_GUI_JOBS` | 1 | Override gui pool concurrency (default serial; still RAM-clamped). |
| `QCI_GUI_SKIP_QDWIN` | 0 | `1` runs only the admin/non-qdwin GUI lane for `qci gui`; `qci gui-admin` sets this automatically. |
| `QCI_XWAYLAND_E2E` | 0 | `1` enables the opt-in qterminal/Textual XWayland desktop-integration scenarios; they otherwise skip in normal GUI/full runs. |
| `QCI_AGENT_TIMEOUT` | 0 | Host-side backstop deadline (s) on each agent scenario, wrapping `QCI_AGENT_CMD` in `timeout -k 15`. `0` = unbounded (the operator command owns the budget). When both are set the smaller wins; on expiry the agent is killed and the scenario fails closed (rc=124, no verdict). |
| `QCI_GUI_RETRY` | 0 | Classified GUI retry. `0`/unset = **report-only**: classify each failure and log to `flake.tsv` what *would* retry, but never re-run. `1`/`classified` = retry **exactly once on a fresh VM**, and only for tight retriable infra/tooling signatures such as `transport-timeout` (qemu-agent/vm-exec wedge), `agent-api-unreachable` (exact external provider connection or selected-model-capacity failure), and `agent-tooling` (agent command-construction failure). `status=FAIL`/`ERROR`, generic `UNKNOWN`, `no-verdict`, and `agent-timeout` (slow agent — possible product hang) are **never** auto-retried. A retried pass always emits a `flake.tsv` row + a note on the result row, so a flake is never silently green. |
| `QCI_NO_GOLDEN` | 0 | `1` disables the per-run golden; every worker runs the full bootstrap. |
| `QDISTRO_VM_BASE` | auto | `auto`: clone qci workers from the imported kiwi image (`qdistro-kiwi-base.qcow2`, tester or ci profile) if present, else `baseweed-baked`. `kiwi` requires the import (`scripts/vm/import-kiwi-base.sh`). `baked` always uses baseweed-baked. `build-in-vm.sh` always clones baked. |
| `QDWIN_VM_VCPUS` | 4 | vCPUs per disposable VM. |
| `QCI_DELETE_FAILED_VM` | 0 | `1` deletes failed VMs instead of preserving them. |
| `QDISTRO_VM_EXEC_TIMEOUT` | 1800 | Overall deadline (s) for a single `vm-exec` in-guest command. On expiry `vm-exec` attempts an identity-checked TERM, then KILL, of the discovered and pinned guest process tree, and exits 124. It reports its own verification limits: a descendant whose identity it cannot pin is named (`unpinnable-descendants:`) and deliberately **not** signalled, and it cannot guarantee it discovers a reparented process, so reaping is attempted and reported, not guaranteed. The deadline is also checked against elapsed time BETWEEN steps, not enforced as wall clock, so a true wall-clock cap must come from outside — use `timeout -k 30 <n> vm-exec ...`, where `-k` makes the cap an undeniable KILL-at-cap+grace for **vm-exec itself**, which a plain `timeout` does not give you against a TERM-resistant process. It does **not** reach descendants: `timeout` waits only for its direct child, so if vm-exec exits on the TERM the later group KILL is never sent. An outer cap bounds how long you wait; it does not bound cleanup, and a short grace can cut vm-exec's own cleanup verification short. The counter is also clamped across host suspend, so it measures elapsed time as the host saw it, not as the guest experienced it. `0` = unbounded. |
| `QDISTRO_VM_AGENT_RPC_TIMEOUT` | 30 | Host-side cap (s) per `virsh qemu-agent-command` RPC, bounding any ONE wedged agent call. It does not bound the total: the deadline above is checked between steps, so many capped calls can still overrun it. `0` = unbounded. |
| `QDISTRO_VM_EXEC_ORPHAN_REAP` | 1 | `vm-exec` orphan registry. Each call whose guest command is pinned records it (guest pid, start time, guest boot id, domain uuid); the record is removed once the command is known finished. Before launching, `vm-exec` resolves records left by a **SIGKILLed** `vm-exec` (identity- and boot-checked kill in the guest). If an orphan cannot be confirmed gone, or the per-VM lock is not obtained within `QDISTRO_VM_EXEC_REAP_LOCK_WAIT` (default 4 x (`QDISTRO_VM_KILL_VERIFY_TIMEOUT`+grace+1)+10 s), the launch is **refused with exit 75** (retryable, nothing started). Records are also KEPT when a call exits while its command may still run -- a poll error such as `Guest agent not responding`, qga losing its bookkeeping (`PID ... does not exist`), or an unverified signal cleanup -- and the next call on that VM resolves them (normally as `already-gone`) once the agent answers. `0` disables recording and reaping. |
| `QDISTRO_VM_EXEC_STATE_DIR` | `$XDG_RUNTIME_DIR/qdistro-vm-exec-<uid>` | Where the orphan registry lives (one subdirectory per VM name). When `XDG_RUNTIME_DIR` is unset it falls back to `/tmp/qdistro-vm-exec-<uid>`. Records are only seen by `vm-exec` calls that resolve to the SAME directory, so a caller with `XDG_RUNTIME_DIR` set and one without it do not see each other's orphans. Set this explicitly when mixing such environments. Per-VM directories are never removed automatically; they are tiny, and stale ones may be deleted by hand when no `vm-exec` is running. A malformed or unknown-format record refuses launches (exit 75) until reconciled by hand: if no such driver runs in the guest, `rm` the file the error names. |
| `QD_VM_START_MAX_WAIT` | 300 | Backstop cap (s) on guest-agent readiness in `vm-start-and-wait` (raised from 120 for parallel boot contention). |
| `QCI_HOST_STEP_TIMEOUT` | 600 | Per-step wall budget (s) for the `host` gate. It does **not** cover the `qdistro-pytest` step — see the next row. Raising this alone does not give pytest more time. |
| `QCI_QDISTRO_PYTEST_TIMEOUT` | 1800 | Wall budget (s) for the `qdistro-pytest` host step **only**, deliberately independent of `QCI_HOST_STEP_TIMEOUT`. The suite's honest cost is ~550s across ten batches, so the shared 600s step budget left no headroom and one slow test killed the gate; 1800s is ~3.3x the honest cost, which keeps the step a wedge detector without being sensitive to normal variance. Set **both** knobs to slow down every host step. |
| `QCI_EXTRA_BATS_ROOTS` | *(unset)* | Colon-separated extra repo roots to discover `tests/integration/vm/*.bats` under. Discovery otherwise covers only the declared `PROJECTS` checkouts, so an out-of-tree suite is invisible unless opted in here. Non-existent roots are ignored; the file list is de-duplicated. |

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
prompt-file path and the result is run via `bash -lc`, so the template can pass
the prompt as a path, on stdin, or inlined with `$(cat {prompt})`, whichever the
runner takes. The supported runner is Codex with `gpt-5.6-luna`, which reads the
prompt on stdin:

```bash
QCI_AGENT_CMD='codex --yolo exec -m gpt-5.6-luna --skip-git-repo-check - < {prompt}' \
QCI_AGENT_MODEL=gpt-5.6-luna \
  qdistro/ci/bin/qci gui
```

`--yolo` is required because the agent must run `vm-exec`/`virsh` and write its
`status.txt` without interactive approval. Do NOT add `--ephemeral`: the gate
reads each attempt's codex rollout (`$CODEX_HOME/sessions`, default
`~/.codex/sessions`) to see which attested frames the driver actually opened
(`ci/lib/gui_rollout_views.py`, sidecar `gui/<slug>.views.txt`). A
`qci:visual: required` PASS or FAIL whose driver opened no attested frame is
recorded ERROR (`agent-unviewed-verdict`); with `--ephemeral` there is no
rollout and every attempt is `unobservable:ephemeral` (no verdict changes). If
the template sets its own `CODEX_HOME=...`, also export `QCI_GUI_CODEX_HOME`
with the same value, or the attempts are `unobservable:codex-home`. Retention:
the rollouts are codex's own files and keep every viewed frame as base64, so
`~/.codex/sessions` grows by roughly the encoded size of every frame each
attempt opened (a 1280x800 admin-app frame is about 50 KB of base64; a two-frame
grading session measured 148 KB); prune it as you would any codex history. Each attempt gets its own working
directory regardless of the template: `run_agent_command` creates one with
`mktemp -d` and `cd`s into it before running the agent. Set `QCI_AGENT_MODEL` whenever a wrapper selects the
model outside the visible template, or the manifest records `unknown`.

**The runner must be vision-capable.** Visual scenarios are graded by opening
the harvested PNGs and looking at them. A driver that cannot view an image has
no verdict about pixels and must record `ERROR`, never `PASS` or `FAIL` -- see
`doc/dev.md` "Visual evidence: vision, not OCR".

The executable qdwin smokes run before the markdown assignments:

- `qdwin/tests/gui/agent-mvp-session-smoke.sh`
- `qdwin/tests/gui/agent-protocol-audit.sh`
- `qdwin/tests/gui/agent-cursor-clickthrough-smoke.sh`
- `qdwin/tests/gui/agent-click-smoke.sh`
- `qdwin/tests/gui/agent-shell-capture-smoke.sh` (skips with a rebake hint
  when the golden's compositor unit lacks `QDWIN_ENABLE_SHELL_CAPTURE=1`)

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

The `host` gate runs tests across all in-tree components. Several components need
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

**qdterm tests** (the `qterminator` package) require the `QTermWidget` Python
binding, which is built from source as part of the qterminator install (it is
not packaged by any distro). Build it from the `qtermwidget-pyqt/` directory
of the in-tree `qdterm/` component:

```bash
cd qdterm/qtermwidget-pyqt && pip install .
```

See `qdterm/README.md` for full build prerequisites (qtermwidget-devel,
sip, pyqt-builder).

**qdfileman tests** (the `qfileman` package) require `tomli_w` (declared in `qdfileman/pyproject.toml`; not
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
